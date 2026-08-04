from __future__ import annotations

import os
import struct
import time
from pathlib import Path

from ptlab.gatt import BUSY_STATUS, GattProtocolError, resolve_characteristic
from ptlab.scenario import ScenarioContext, ScenarioDefinition
from ptlab.wire import (
    SCHEDULE_RECORD_VERSION,
    TASK_RECORD_VERSION,
    begin_sync,
    commit_sync,
    encode_schedule_record,
    encode_task_record,
    parse_companion_status,
    parse_digest,
    pull_records,
    push_records,
    record_message,
)


ATT_INSUFFICIENT_AUTHENTICATION = 0x05
SETTLE_MS = 250


def _peer(value: int, security: str = "AUTHENTICATED", *, replace: bool = False) -> str:
    address = bytes((value, value + 1, 2, 3, 4, 5)).hex()
    return f"NEXT_PEER 1 {address} {security}" + (" REPLACE" if replace else "")


def _peer_id(value: int) -> str:
    return "1:" + bytes((value, value + 1, 2, 3, 4, 5)).hex()


def _connect_and_disconnect(context: ScenarioContext, value: int, *, replace: bool = False) -> None:
    control = context.open_control()
    control.command(_peer(value, replace=replace))
    control.command("CONNECT")
    control.command("DISCONNECT")
    control.command(f"ADVANCE {SETTLE_MS}")


def _wait_digest(
    context: ScenarioContext,
    client,
    characteristic: str,
    *,
    count: int,
    version: int,
) -> None:
    deadline = time.monotonic() + 3.0
    last = None
    while time.monotonic() < deadline:
        response = client.read(characteristic)
        if response.status == 0:
            last = parse_digest(response.payload)
            if last.count == count and last.version == version:
                return
        time.sleep(0.025)
    context.require(
        False,
        f"{characteristic} commit becomes live",
        actual=last,
        expected={"count": count, "version": version},
    )


def _host_test(context: ScenarioContext, executable: Path, name: str) -> None:
    result = context.run_command([str(executable)], cwd=context.paths.run, timeout=30)
    summary = (result.stdout + result.stderr).strip().splitlines()
    context.check(
        result.returncode == 0 and any("0 failures" in line for line in summary),
        name,
        actual={"returncode": result.returncode, "summary": summary[-1] if summary else ""},
        expected="returncode 0 and 0 failures",
    )


def bond_lifecycle(context: ScenarioContext) -> None:
    control = context.open_control()
    initial = control.query()
    context.check(initial["boot"] == "initialized_empty", "fresh store initializes empty", actual=initial["boot"], expected="initialized_empty")
    context.check(initial["retained_peers"] == 0, "fresh store has no retained peers", actual=initial["retained_peers"], expected=0)

    for value in range(1, 6):
        _connect_and_disconnect(context, value)
    five = control.query()
    context.check(five["retained_peers"] == 5, "five peers are retained", actual=five["retained_peers"], expected=5)
    context.check(set(five["retained"]) == {_peer_id(value) for value in range(1, 6)}, "retained identities match five virtual fixtures")

    control.command("REBOOT")
    restored = control.query()
    context.check(restored["boot"] == "restored", "reboot restores bond snapshot", actual=restored["boot"], expected="restored")
    context.check(restored["retained_peers"] == 5, "reboot restores all five peers", actual=restored["retained_peers"], expected=5)

    _connect_and_disconnect(context, 1)
    _connect_and_disconnect(context, 6)
    lru = control.query()
    context.check(lru["evictions"] == 1, "sixth peer performs one LRU eviction", actual=lru["evictions"], expected=1)
    context.check(_peer_id(1) in lru["retained"], "touched older peer survives eviction")
    context.check(_peer_id(6) in lru["retained"], "new current-MRU peer survives eviction")
    context.check(_peer_id(2) not in lru["retained"], "untouched oldest peer is evicted")

    _connect_and_disconnect(context, 6)
    _connect_and_disconnect(context, 1, replace=True)
    replaced = control.query()
    context.check(replaced["retained_peers"] == 5, "repeat replacement preserves capacity", actual=replaced["retained_peers"], expected=5)
    context.check(replaced["evictions"] == 1, "repeat replacement does not add an eviction", actual=replaced["evictions"], expected=1)

    control.command(_peer(1))
    control.command("CONNECT")
    control.command("CCCD 0x0100 0x0081")
    control.command("DISCONNECT")
    control.command(f"ADVANCE {SETTLE_MS}")
    cccd = control.query()
    context.check(cccd["cccds"] == 1, "CCCD-only mutation is persisted", actual=cccd["cccds"], expected=1)
    control.command("REBOOT")
    context.check(control.query()["cccds"] == 1, "CCCD-only state restores after reboot")

    control.command("RESET")
    _connect_and_disconnect(context, 9)
    writes_before = control.query()["flash_writes"]
    for _ in range(3):
        control.command(_peer(9))
        with context.gatt() as gatt:
            battery = gatt.read("battery")
            context.check(battery.status == 0 and len(battery.payload) == 1, "battery-like reconnect remains public")
        control.command(f"ADVANCE {SETTLE_MS}")
    writes_after = control.query()["flash_writes"]
    context.check(
        writes_after == writes_before,
        "repeated sole/current-MRU battery-like connections do not write flash",
        actual=writes_after,
        expected=writes_before,
    )

    control.command("RESET")
    control.command(_peer(20, "UNAUTHENTICATED"))
    with context.gatt() as gatt:
        status_response = gatt.read("companion_status")
        status = parse_companion_status(status_response.payload)
    reset = control.query()
    context.check(status.reset_epoch > 0, "explicit reset advances to a nonzero reset epoch", actual=status.reset_epoch)
    context.check(reset["retained_peers"] == 0, "explicit reset leaves an empty registry", actual=reset["retained_peers"], expected=0)
    control.command("REBOOT")
    empty_restore = control.query()
    context.check(empty_restore["boot"] == "restored" and empty_restore["retained_peers"] == 0, "empty reset snapshot restores across reboot")
    _host_test(
        context,
        context.workspace.infinisim / "build" / "infinisim-ble-adapters-test",
        "portable adapter regression covers exact forget-all epoch and codec/LRU invariants",
    )


def radio_policy(context: ScenarioContext) -> None:
    control = context.open_control()
    initial = control.query()
    context.check(initial["radio_actual"] == "Fast", "radio begins in fast connectable mode", actual=initial["radio_actual"], expected="Fast")

    starts_before = initial["gap_starts"]
    stops_before = initial["gap_stops"]
    control.command("ADVANCE 30000")
    slow = control.query()
    context.check(slow["radio_actual"] == "Slow", "virtual fast timeout transitions to slow advertising", actual=slow["radio_actual"], expected="Slow")
    context.check(slow["gap_stops"] - stops_before == 1, "fast-to-slow issues one stop", actual=slow["gap_stops"] - stops_before, expected=1)
    context.check(slow["gap_starts"] - starts_before == 1, "fast-to-slow issues one replacement start", actual=slow["gap_starts"] - starts_before, expected=1)

    control.command("RESET")
    control.command("GAP_RESULT START -23 FAILED")
    control.command("ADVANCE 30000")
    failed = control.query()
    context.check(failed["radio_actual"] == "Off", "injected start failure leaves radio off during backoff", actual=failed["radio_actual"], expected="Off")
    attempts = failed["gap_starts"]
    control.command("ADVANCE 249")
    context.check(control.query()["gap_starts"] == attempts, "first retry is bounded before 250 ms")
    control.command("ADVANCE 1")
    retried = control.query()
    context.check(retried["radio_actual"] == "Fast", "250 ms backoff retries into fast mode", actual=retried["radio_actual"], expected="Fast")

    control.command("RESET")
    for _ in range(4):
        control.command("GAP_RESULT START -114 ADVERTISING_ACTIVE")
    control.command("ADVANCE 30000")
    delays = (250, 1000, 4000, 60000)
    previous_starts = control.query()["gap_starts"]
    for delay in delays:
        control.command(f"ADVANCE {delay - 1}")
        context.check(control.query()["gap_starts"] == previous_starts, f"EALREADY retry waits {delay} ms")
        control.command("ADVANCE 1")
        current = control.query()["gap_starts"]
        context.check(current == previous_starts + 1, f"EALREADY retry fires once at {delay} ms")
        previous_starts = current
    control.command("ADVANCE 60000")
    recovered = control.query()
    context.check(
        recovered["radio_actual"] in {"Fast", "Slow"},
        "bounded EALREADY recovery returns to connectable advertising",
        actual=recovered["radio_actual"],
        expected="Fast or Slow",
    )

    writes_before = recovered["flash_writes"]
    control.command(_peer(30))
    with context.gatt() as gatt:
        key = gatt.write("beacon_key", bytes(range(28)))
        context.require(key.status == 0, "beacon key provisioning accepted", actual=key.status, expected=0)
        enable = gatt.write("beacon_control", b"\x01")
        context.require(enable.status == 0, "beacon transition request accepted", actual=enable.status, expected=0)
        time.sleep(0.2)
        try:
            gatt.read("battery")
        except GattProtocolError:
            pass
    beacon = control.query()
    context.check(beacon["link"] == 0, "beacon transition terminates the connected link", actual=beacon["link"], expected=0)
    context.check(beacon["radio_actual"] == "Beacon", "connected terminate completes into beacon mode", actual=beacon["radio_actual"], expected="Beacon")
    context.check(beacon["gap_terminates"] >= 1, "connected beacon transition records one termination")
    context.check(beacon["flash_writes"] == writes_before, "radio and firmware key transitions do not masquerade as bond-store writes", actual=beacon["flash_writes"], expected=writes_before)
    control.command(f"ADVANCE {SETTLE_MS}")
    settled = control.query()
    context.check(settled["flash_writes"] == writes_before + 1, "disconnect settles exactly one virtual bond-store write proxy", actual=settled["flash_writes"], expected=writes_before + 1)
    context.check(settled["persistence_wakelock_ms"] >= 0, "persistence power proxy is reported without electrical-current claims")
    _host_test(
        context,
        context.workspace.infinitime / "build-host-tests" / "ble_radio_state_machine_test",
        "portable radio regression covers off/on, passive health, same-step ordering, and proxy cadence",
    )


def link_auth(context: ScenarioContext) -> None:
    control = context.open_control()
    control.command(_peer(40, "UNAUTHENTICATED"))
    incumbent = context.gatt()
    try:
        with context.gatt() as second:
            busy = second.read("battery")
            context.check(busy.status == BUSY_STATUS, "second active client receives busy response", actual=busy.status, expected=BUSY_STATUS)
        protected = incumbent.write("schedule_sync", begin_sync(0, 1))
        context.check(
            protected.status == ATT_INSUFFICIENT_AUTHENTICATION,
            "unauthenticated schedule write returns ATT authentication error",
            actual=protected.status,
            expected=ATT_INSUFFICIENT_AUTHENTICATION,
        )
        public = incumbent.read("companion_status")
        context.check(public.status == 0 and len(public.payload) == 20, "public companion status works without authentication")
        verify = incumbent.read("companion_verify")
        context.check(
            verify.status == ATT_INSUFFICIENT_AUTHENTICATION,
            "unauthenticated companion verify is denied",
            actual=verify.status,
            expected=ATT_INSUFFICIENT_AUTHENTICATION,
        )
        control.command("FORCE_DISCONNECT")
        try:
            incumbent.read("battery")
            forced_closed = False
        except GattProtocolError:
            forced_closed = True
        context.check(forced_closed, "explicit force disconnect releases the incumbent")
    finally:
        incumbent.close()

    control.command(_peer(41))
    with context.gatt() as authenticated:
        verify = authenticated.read("companion_verify")
        context.check(verify.status == 0 and len(verify.payload) == 20, "authenticated companion verify works")

        schedule_begin = authenticated.write("schedule_sync", begin_sync(2, 100))
        schedule_record = authenticated.write(
            "schedule_sync",
            record_message(
                0,
                encode_schedule_record(identifier=1, title="staged schedule"),
                record_version=SCHEDULE_RECORD_VERSION,
            ),
        )
        task_begin = authenticated.write("tasks_sync", begin_sync(2, 200))
        task_record = authenticated.write(
            "tasks_sync",
            record_message(
                0,
                encode_task_record(identifier=1, order=0, title="staged task"),
                record_version=TASK_RECORD_VERSION,
            ),
        )
        context.require(all(response.status == 0 for response in (schedule_begin, schedule_record, task_begin, task_record)), "schedule and task staging begins")

    control.command(_peer(41))
    with context.gatt() as reconnected:
        schedule_commit = reconnected.write("schedule_sync", commit_sync(2))
        task_commit = reconnected.write("tasks_sync", commit_sync(2))
        context.check(schedule_commit.status != 0, "disconnect cleanup aborts schedule staging")
        context.check(task_commit.status != 0, "disconnect cleanup aborts task staging")

    control.command(_peer(42))
    control.command("FORCE_CONNECT")
    forced = control.query()
    context.check(forced["link"] == 1, "explicit force connect attaches the selected virtual peer")
    control.command("FORCE_DISCONNECT")
    context.check(control.query()["link"] == 0, "explicit force disconnect clears the virtual link")


def persistence_faults(context: ScenarioContext) -> None:
    control = context.open_control()
    live = context.paths.run / "infinisim-ble-bonds.bin"
    staged = context.paths.run / "infinisim-ble-bonds.bin.next"

    control.command("RESET")
    _connect_and_disconnect(context, 1)
    old_live = live.read_bytes()
    control.command("STORE_FAILURE WRITE")
    _connect_and_disconnect(context, 2)
    write_failure = control.query()
    context.check(write_failure["write_failures"] >= 1, "injected write failure is counted")
    context.check(live.read_bytes() == old_live, "write failure preserves the old complete live store")
    control.command("STORE_FAILURE NONE")
    control.command("REBOOT")
    restored = control.query()
    context.check(_peer_id(1) in restored["retained"] and _peer_id(2) not in restored["retained"], "write-failure reboot restores old state")

    control.command("STORE_FAILURE READ")
    control.command("REBOOT")
    read_failure = control.query()
    context.check(read_failure["boot"] == "restore_failed", "injected read failure reports restore_failed", actual=read_failure["boot"], expected="restore_failed")
    context.check(read_failure["persistence_writes_enabled"] == 0, "read failure disables persistence writes")
    control.command("STORE_FAILURE NONE")

    for cut in ("BEFORE_REPLACE", "AFTER_PARTIAL_STAGED_WRITE", "AFTER_STAGED_WRITE"):
        control.command("RESET")
        _connect_and_disconnect(context, 10)
        before = live.read_bytes()
        control.command(f"POWER_CUT {cut}")
        _connect_and_disconnect(context, 11)
        cut_state = control.query()
        context.check(cut_state["write_failures"] >= 1, f"{cut} records a failed atomic replacement")
        context.check(live.read_bytes() == before, f"{cut} leaves the old live store byte-exact")
        context.check(len(live.read_bytes()) > 20, f"{cut} leaves a complete nonempty live file")
        staged_candidate: bytes | None = None
        if cut == "BEFORE_REPLACE":
            context.check(not staged.exists(), "before-replace cut does not invent a torn staged file")
        elif cut == "AFTER_PARTIAL_STAGED_WRITE":
            context.check(staged.exists() and 0 < staged.stat().st_size < live.stat().st_size, "partial staged cut is confined to .next")
        else:
            staged_candidate = staged.read_bytes() if staged.exists() else None
            context.check(staged_candidate is not None and len(staged_candidate) > 20, "full staged cut leaves a complete-size .next candidate")
        control.command("POWER_CUT NONE")
        control.command("REBOOT")
        after = control.query()
        context.check(_peer_id(10) in after["retained"] and _peer_id(11) not in after["retained"], f"{cut} reboot restores the old complete snapshot")
        if staged_candidate is not None:
            live.write_bytes(staged_candidate)
            control.command("REBOOT")
            promoted = control.query()
            context.check(
                promoted["boot"] == "restored" and _peer_id(11) in promoted["retained"],
                "full staged candidate decodes as the complete new snapshot when promoted",
            )

    control.command("RESET")
    _connect_and_disconnect(context, 20)
    corrupt = bytearray(live.read_bytes())
    corrupt[20] ^= 0x80
    live.write_bytes(corrupt)
    control.command("REBOOT")
    invalid = control.query()
    context.check(invalid["boot"] == "invalid", "invalid decoder input fails closed", actual=invalid["boot"], expected="invalid")
    context.check(invalid["persistence_writes_enabled"] == 0, "invalid decoder disables writes")
    evidence = live.read_bytes()
    _connect_and_disconnect(context, 21)
    context.check(live.read_bytes() == evidence, "fail-closed mode preserves invalid evidence")
    control.command("RESET")
    recovered = control.query()
    context.check(recovered["boot"] == "initialized_empty", "explicit reset recovers from invalid evidence", actual=recovered["boot"], expected="initialized_empty")
    context.check(recovered["persistence_writes_enabled"] == 1 and recovered["retained_peers"] == 0, "explicit reset commits an empty writable store")
    control.command("REBOOT")
    context.check(control.query()["boot"] == "restored", "reset recovery restores normally")
    _host_test(
        context,
        context.workspace.infinisim / "build" / "infinisim-ble-adapters-test",
        "portable adapter regression validates complete old/new snapshots and explicit forget-all recovery",
    )


def family_handoff(context: ScenarioContext) -> None:
    control = context.open_control()
    schedule_a = encode_schedule_record(identifier=101, title="A schedule")
    task_a = encode_task_record(identifier=201, order=0, title="A task")

    control.command(_peer(50))
    client_a = context.gatt()
    push_records(
        client_a,
        sync_characteristic="schedule_sync",
        records=[schedule_a],
        version=1001,
        record_version=SCHEDULE_RECORD_VERSION,
    )
    push_records(
        client_a,
        sync_characteristic="tasks_sync",
        records=[task_a],
        version=2001,
        record_version=TASK_RECORD_VERSION,
    )
    _wait_digest(context, client_a, "schedule_digest", count=1, version=1001)
    _wait_digest(context, client_a, "tasks_digest", count=1, version=2001)

    control.command(_peer(51))
    with context.gatt() as blocked_b:
        busy = blocked_b.read("battery")
        context.check(busy.status == BUSY_STATUS, "family B cannot replace incumbent A implicitly", actual=busy.status, expected=BUSY_STATUS)
    client_a.close()

    control.command(_peer(51))
    with context.gatt() as client_b:
        schedule_digest, schedules = pull_records(
            client_b,
            digest_characteristic="schedule_digest",
            read_characteristic="event_read",
        )
        task_digest, tasks = pull_records(
            client_b,
            digest_characteristic="tasks_digest",
            read_characteristic="task_read",
        )
        context.check(schedule_digest.version == 1001 and schedules == [schedule_a], "family B adopts watch-authoritative schedule A")
        context.check(task_digest.version == 2001 and tasks == [task_a], "family B adopts watch-authoritative task A")
        schedule_b = encode_schedule_record(identifier=102, title="B schedule")
        task_b = encode_task_record(identifier=202, order=1, title="B task")
        push_records(
            client_b,
            sync_characteristic="schedule_sync",
            records=[*schedules, schedule_b],
            version=1002,
            record_version=SCHEDULE_RECORD_VERSION,
        )
        push_records(
            client_b,
            sync_characteristic="tasks_sync",
            records=[*tasks, task_b],
            version=2002,
            record_version=TASK_RECORD_VERSION,
        )
        _wait_digest(context, client_b, "schedule_digest", count=2, version=1002)
        _wait_digest(context, client_b, "tasks_digest", count=2, version=2002)

    control.command(_peer(50))
    with context.gatt() as client_a_again:
        schedule_digest, schedules = pull_records(
            client_a_again,
            digest_characteristic="schedule_digest",
            read_characteristic="event_read",
        )
        task_digest, tasks = pull_records(
            client_a_again,
            digest_characteristic="tasks_digest",
            read_characteristic="task_read",
        )
        context.check(schedule_digest.version == 1002 and {struct.unpack_from("<H", record)[0] for record in schedules} == {101, 102}, "family A sees B's merged schedule after release")
        context.check(task_digest.version == 2002 and {struct.unpack_from("<H", record)[0] for record in tasks} == {201, 202}, "family A sees B's merged task list after release")


def _external_regression(context: ScenarioContext, script: str, *, timeout: float) -> None:
    env = dict(
        os.environ,
        BRIDGE_HOST="127.0.0.1",
        BRIDGE_PORT=str(context.gatt_port),
        SIM_LOG=str(context.paths.stdout),
    )
    result = context.run_command(
        ["node", script],
        cwd=context.workspace.devtools,
        env=env,
        timeout=timeout,
    )
    parsed = 0
    for line in (result.stdout + "\n" + result.stderr).splitlines():
        stripped = line.strip()
        if stripped.startswith("ok:"):
            parsed += 1
            context.check(True, stripped[3:].strip())
        elif stripped.startswith("FAIL:"):
            parsed += 1
            context.check(False, stripped[5:].strip(), detail=stripped)
    context.check(parsed > 0, f"{script} emitted machine-parsable checks", actual=parsed)
    context.check(result.returncode == 0, f"{script} exits successfully", actual=result.returncode, expected=0)


def protocol_regression(context: ScenarioContext) -> None:
    _external_regression(context, "bridge-test.mjs", timeout=90)


def dfu_regression(context: ScenarioContext) -> None:
    def crc16(data: bytes) -> int:
        crc = 0xFFFF
        for byte in data:
            crc = ((crc >> 8) & 0xFF) | ((crc << 8) & 0xFFFF)
            crc ^= byte
            crc ^= (crc & 0xFF) >> 4
            crc ^= (crc << 12) & 0xFFFF
            crc ^= ((crc & 0xFF) << 5) & 0xFFFF
            crc &= 0xFFFF
        return crc

    image = bytes((index * 7) & 0xFF for index in range(460))
    image_crc = crc16(image)
    init_packet = struct.pack("<HHIHH", 0x0102, 0x0304, 1, 1, 0xFFFF) + struct.pack("<H", image_crc)
    sizes = b"\0" * 8 + struct.pack("<I", len(image))

    with context.gatt(timeout=10.0) as gatt:
        started = gatt.write("dfu_control", b"\x01\x04")
        context.check(started.status == 0, "DFU StartDFU control write is accepted")
        gatt.write_without_response("dfu_packet", sizes)
        notification = gatt.next_notification()
        context.check(
            notification.characteristic == resolve_characteristic("dfu_control")[1]
            and notification.payload == b"\x10\x01\x01",
            "DFU image-size phase returns the firmware start acknowledgement",
            actual=notification.payload.hex(),
            expected="100101",
        )
        begin = gatt.write("dfu_control", b"\x02\x00")
        context.check(begin.status == 0, "DFU init-begin control write is accepted")
        gatt.write_without_response("dfu_packet", init_packet)
        complete = gatt.write("dfu_control", b"\x02\x01")
        context.check(complete.status == 0, "DFU init-complete control write is accepted")

    deadline = time.monotonic() + 3
    log = ""
    while time.monotonic() < deadline:
        log = context.paths.stdout.read_text(errors="replace")
        if f"CRC = {image_crc}" in log:
            break
        time.sleep(0.05)
    context.check(f"CRC = {image_crc}" in log, "DFU init parser records the transmitted CRC")


def raw_flash_power_loss(context: ScenarioContext) -> None:
    control = context.open_control()
    control.command(_peer(60))
    gatt = context.gatt()
    seed = [
        encode_schedule_record(identifier=index + 1, title=f"Seed {index}")
        for index in range(3)
    ]
    push_records(
        gatt,
        sync_characteristic="schedule_sync",
        records=seed,
        version=31337,
        record_version=SCHEDULE_RECORD_VERSION,
    )
    _wait_digest(context, gatt, "schedule_digest", count=3, version=31337)
    begin = gatt.write("schedule_sync", begin_sync(64, 777777))
    context.require(begin.status == 0, "raw power-loss staging begins")
    for index in range(32):
        response = gatt.write(
            "schedule_sync",
            record_message(
                index,
                encode_schedule_record(identifier=100 + index, title=f"Doomed {index}"),
                record_version=SCHEDULE_RECORD_VERSION,
            ),
        )
        context.require(response.status == 0, f"raw power-loss staged record {index}")

    context.close()
    killed = context.simulator.kill()
    context.check(killed, "simulator is killed mid-transaction")
    gatt.close()

    littlefs = context.workspace.infinisim / "build" / "littlefs-do"
    listing = context.run_command([str(littlefs), "ls", "/.system"], cwd=context.paths.flash)
    context.check("schedule.dat" in listing.stdout, "raw flash retains schedule.dat after power cut")
    context.check("schedule.stg" in listing.stdout, "raw flash contains only the interrupted staging file")

    state = context.simulator.start(
        bridge_port=context.gatt_port,
        ble_control_port=context.control_port,
        headless=True,
    )
    context.sim_state = state
    control = context.reconnect_control()
    control.command(_peer(60))
    with context.gatt() as rebooted:
        digest = rebooted.read("schedule_digest")
        parsed = parse_digest(digest.payload)
        context.check(parsed.count == 3 and parsed.version == 31337, "reboot restores pre-cut live schedule")
    context.simulator.stop()
    listing = context.run_command([str(littlefs), "ls", "/.system"], cwd=context.paths.flash)
    context.check("schedule.stg" not in listing.stdout, "boot removes interrupted schedule staging file")
    context.sim_state = context.simulator.start(
        bridge_port=context.gatt_port,
        ble_control_port=context.control_port,
        headless=True,
    )
    context.reconnect_control()


SCENARIOS = (
    ScenarioDefinition(
        "bond-lifecycle",
        "Virtual bond persistence, five-peer LRU, CCCD restore, reset epoch, and write cadence.",
        bond_lifecycle,
        timeout=90,
    ),
    ScenarioDefinition(
        "radio-policy",
        "Virtual radio fast/slow policy, bounded failures, connected beacon termination, and proxies.",
        radio_policy,
        timeout=90,
    ),
    ScenarioDefinition(
        "link-auth",
        "Single-client ownership, virtual authentication policy, force controls, and disconnect cleanup.",
        link_auth,
        timeout=60,
    ),
    ScenarioDefinition(
        "persistence-faults",
        "Read/write failures, named staged power cuts, decoder fail-closed behavior, and reset recovery.",
        persistence_faults,
        timeout=120,
    ),
    ScenarioDefinition(
        "family-handoff",
        "Independent A-to-B-to-A watch-authoritative schedule and task merges.",
        family_handoff,
        timeout=90,
    ),
    ScenarioDefinition(
        "protocol-regression",
        "Existing companion protocol regression through the isolated dynamic GATT bridge.",
        protocol_regression,
        tags=frozenset({"regression"}),
        timeout=120,
    ),
    ScenarioDefinition(
        "dfu",
        "Existing legacy DFU parser regression through the isolated dynamic GATT bridge.",
        dfu_regression,
        tags=frozenset({"regression"}),
        timeout=60,
    ),
    ScenarioDefinition(
        "raw-flash-power-loss",
        "Raw littlefs schedule staging power-loss and boot cleanup regression.",
        raw_flash_power_loss,
        tags=frozenset({"regression", "power-loss"}),
        timeout=120,
    ),
)

SCENARIO_BY_NAME = {scenario.name: scenario for scenario in SCENARIOS}
if len(SCENARIO_BY_NAME) != len(SCENARIOS):
    raise RuntimeError("scenario names must be unique")


def select_scenarios(suite: str, filters: list[str] | None = None) -> list[ScenarioDefinition]:
    if suite == "all":
        selected = list(SCENARIOS)
    else:
        try:
            selected = [SCENARIO_BY_NAME[suite]]
        except KeyError as error:
            raise ValueError(f"unknown suite {suite!r}") from error
    for expression in filters or []:
        needle = expression.casefold()
        selected = [
            scenario
            for scenario in selected
            if needle in scenario.name.casefold()
            or needle in scenario.description.casefold()
            or any(needle in tag.casefold() for tag in scenario.tags)
        ]
    return selected
