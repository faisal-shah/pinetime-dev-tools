from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import orjson
import pytest

from ptlab.hardware import (
    AcceptancePlan,
    AdbHardware,
    BleEndpoint,
    CharacteristicContract,
    HardwareError,
    parse_adb_devices,
    run_soak,
)
from ptlab.workspace import Workspace


def test_ble_endpoint_requires_exactly_one_identity() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        BleEndpoint()
    with pytest.raises(ValueError, match="exactly one"):
        BleEndpoint("AA:BB:CC:DD:EE:FF", "InfiniTime")
    assert BleEndpoint(name="InfiniTime").label == "InfiniTime"


def test_bleak_adapter_uses_current_bluez_argument_shape() -> None:
    contract = CharacteristicContract("battery", "revision", "status", "verify")
    from ptlab.hardware import BleakHardware

    assert BleakHardware(contract)._backend_kwargs(
        BleEndpoint(address="AA:BB:CC:DD:EE:FF", adapter="hci1")
    ) == {"bluez": {"adapter": "hci1"}}


def test_contract_loads_characteristics_from_manifest(tmp_path: Path) -> None:
    manifest = {
        "characteristics": [
            {"name": "battery", "characteristic_uuid": "battery"},
            {"name": "firmware_revision", "characteristic_uuid": "revision"},
            {"name": "companion_status", "characteristic_uuid": "status"},
            {"name": "companion_verify", "characteristic_uuid": "verify"},
        ]
    }
    protocol = tmp_path / "InfiniTime" / "protocol"
    protocol.mkdir(parents=True)
    (protocol / "companion.json").write_bytes(orjson.dumps(manifest))
    contract = CharacteristicContract.load(Workspace(tmp_path))
    assert contract.companion_verify == "verify"


def test_parse_adb_devices_ignores_header_and_blank_lines() -> None:
    assert parse_adb_devices(
        "List of devices attached\nphone-a\tdevice product:x\nphone-b\toffline\n\n"
    ) == {"phone-a": "device", "phone-b": "offline"}


def test_adb_hardware_uses_public_settings_and_launcher_commands() -> None:
    commands: list[list[str]] = []

    def runner(command, **_kwargs):
        commands.append(command)
        if command == ["adb", "devices"]:
            return subprocess.CompletedProcess(command, 0, "List of devices attached\nphone-a\tdevice\n", "")
        if command[-3:] == ["shell", "getprop", "ro.build.version.sdk"]:
            return subprocess.CompletedProcess(command, 0, "35\n", "")
        if command[-3:] == ["shell", "getprop", "ro.product.model"]:
            return subprocess.CompletedProcess(command, 0, "Test Phone\n", "")
        if command[-2:] == ["dumpsys", "bluetooth_manager"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "enabled: true\nAA:BB:CC:DD:EE:FF BONDED\nother private device\n",
                "",
            )
        return subprocess.CompletedProcess(command, 0, "ok\n", "")

    adb = AdbHardware("phone-a", runner=runner)
    adb.launch_app()
    adb.open_bluetooth_settings()
    snapshot = adb.snapshot("AA:BB:CC:DD:EE:FF")

    assert any("android.settings.BLUETOOTH_SETTINGS" in command for command in commands)
    assert any("monkey" in command for command in commands)
    assert snapshot["bluetooth_evidence"] == [
        "enabled: true",
        "AA:BB:CC:DD:EE:FF BONDED",
    ]


def test_adb_hardware_rejects_unready_device() -> None:
    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            "List of devices attached\nphone-a\toffline\n",
            "",
        )

    with pytest.raises(HardwareError, match="not ready"):
        AdbHardware("phone-a", runner=runner).verify()


def test_acceptance_plan_validates_multi_peer_sequence(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(
        orjson.dumps(
            {
                "schema_version": 1,
                "watch_label": "Kid A",
                "peers": [
                    {
                        "label": "parent-a",
                        "kind": "android",
                        "serial": "phone-a",
                    },
                    {
                        "label": "laptop",
                        "kind": "linux",
                        "address": "AA:BB:CC:DD:EE:FF",
                    },
                ],
                "sequence": ["parent-a", "laptop", "parent-a"],
            }
        )
    )
    plan = AcceptancePlan.load(plan_path)
    assert plan.sequence == ("parent-a", "laptop", "parent-a")

    bad = orjson.loads(plan_path.read_bytes())
    bad["sequence"] = ["parent-a", "missing", "parent-a"]
    plan_path.write_bytes(orjson.dumps(bad))
    with pytest.raises(ValueError, match="unknown peers"):
        AcceptancePlan.load(plan_path)


def test_acceptance_plan_rejects_duplicate_central_identity(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(
        orjson.dumps(
            {
                "schema_version": 1,
                "watch_label": "Kid A",
                "peers": [
                    {"label": "one", "kind": "android", "serial": "phone-a"},
                    {"label": "two", "kind": "android", "serial": "phone-a"},
                ],
                "sequence": ["one", "two", "one"],
            }
        )
    )
    with pytest.raises(ValueError, match="independent"):
        AcceptancePlan.load(plan_path)


def test_failed_soak_checkpoints_partial_evidence() -> None:
    class FakeHardware:
        def __init__(self) -> None:
            self.reads = 0

        async def read_battery(self, _endpoint):
            self.reads += 1
            if self.reads == 4:
                raise HardwareError("final baseline read failed")
            return 80 - self.reads

        async def observe_pair(self, _candidate, _baseline, **kwargs):
            kwargs["on_sample"](
                {"timestamp": "one", "visible": True},
                {"timestamp": "one", "visible": True},
            )
            return [], []

    checkpoints = []

    def checkpoint(value):
        checkpoints.append(orjson.loads(orjson.dumps(value)))

    with pytest.raises(HardwareError, match="final baseline"):
        asyncio.run(
            run_soak(
                FakeHardware(),
                BleEndpoint(address="AA:BB:CC:DD:EE:01"),
                BleEndpoint(address="AA:BB:CC:DD:EE:02"),
                duration=1,
                interval=1,
                scan_window=1,
                checkpoint=checkpoint,
            )
        )

    assert checkpoints[-1]["status"] == "failed"
    assert checkpoints[-1]["candidate"]["advertising_samples"] == [
        {"timestamp": "one", "visible": True}
    ]
