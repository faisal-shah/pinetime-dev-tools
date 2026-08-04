import signal

import pytest

from ptlab.processes import ProcessRef, terminate_pids, terminate_process_groups


def test_cleanup_targets_recorded_process_groups_and_escalates() -> None:
    signals = []
    times = iter((0.0, 0.0, 0.1, 0.2))

    terminate_process_groups(
        [ProcessRef(pid=101, pgid=201, name="sim")],
        timeout=0.1,
        alive=lambda _: True,
        send_group_signal=lambda pgid, sig: signals.append((pgid, sig)),
        monotonic=lambda: next(times),
        sleep=lambda _: None,
    )

    assert signals == [(201, signal.SIGTERM), (201, signal.SIGKILL)]


def test_cleanup_refuses_own_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ptlab.processes.os.getpgrp", lambda: 42)

    with pytest.raises(RuntimeError, match="refusing"):
        terminate_process_groups([ProcessRef(pid=12, pgid=42, name="bad")])


def test_legacy_cleanup_targets_exact_recorded_pids() -> None:
    signals = []
    live = {101, 102}

    def send_signal(pid, sig):
        signals.append((pid, sig))
        live.discard(pid)

    terminate_pids(
        [101, 102],
        alive=live.__contains__,
        send_signal=send_signal,
    )

    assert set(signals) == {(101, signal.SIGTERM), (102, signal.SIGTERM)}
