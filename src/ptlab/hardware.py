from __future__ import annotations

import asyncio
import os
import subprocess
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson

from ptlab.wire import CompanionStatus, parse_companion_status
from ptlab.workspace import Workspace


CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"
ANDROID_PACKAGE = "dev.faisal.pinetimecompanion"


class HardwareError(RuntimeError):
    pass


@dataclass(frozen=True)
class BleEndpoint:
    address: str | None = None
    name: str | None = None
    adapter: str | None = None

    def __post_init__(self) -> None:
        if bool(self.address) == bool(self.name):
            raise ValueError("specify exactly one of BLE address or name")

    @property
    def label(self) -> str:
        return self.address or self.name or "unknown"


@dataclass(frozen=True)
class CharacteristicContract:
    battery: str
    firmware_revision: str
    companion_status: str
    companion_verify: str

    @classmethod
    def load(cls, workspace: Workspace) -> CharacteristicContract:
        manifest = orjson.loads(
            (workspace.infinitime / "protocol" / "companion.json").read_bytes()
        )
        characteristics = {
            item["name"]: item["characteristic_uuid"]
            for item in manifest["characteristics"]
        }
        try:
            return cls(
                battery=characteristics["battery"],
                firmware_revision=characteristics["firmware_revision"],
                companion_status=characteristics["companion_status"],
                companion_verify=characteristics["companion_verify"],
            )
        except KeyError as error:
            raise HardwareError(
                f"companion protocol is missing {error.args[0]!r}"
            ) from error


def _status_dict(status: CompanionStatus) -> dict[str, int]:
    return asdict(status)


def _validate_status_pair(
    public: CompanionStatus,
    verified: CompanionStatus,
) -> None:
    stable = ("protocol", "capacity", "eviction_policy", "reset_epoch")
    for field in stable:
        if getattr(public, field) != getattr(verified, field):
            raise HardwareError(
                f"authenticated status changed {field}: "
                f"{getattr(public, field)} -> {getattr(verified, field)}"
            )
    if verified.bonded_count < public.bonded_count:
        raise HardwareError("authenticated status regressed the bonded-peer count")
    if verified.eviction_count < public.eviction_count:
        raise HardwareError("authenticated status regressed the eviction count")


def _hardware_run_dir(root: Path, label: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = root / f"{stamp}-{label}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def write_hardware_result(path: Path, result: dict[str, Any]) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_bytes(
        orjson.dumps(result, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)
        + b"\n"
    )
    os.replace(temporary, path)


def _load_bleak() -> tuple[Any, Any]:
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError as error:
        raise HardwareError(
            "Bleak is not installed; run `uv sync --group hardware` and retry"
        ) from error
    return BleakClient, BleakScanner


class BleakHardware:
    def __init__(
        self,
        contract: CharacteristicContract,
        *,
        timeout: float = 30.0,
    ) -> None:
        if timeout <= 0:
            raise ValueError("BLE timeout must be positive")
        self.contract = contract
        self.timeout = timeout

    def _backend_kwargs(self, endpoint: BleEndpoint) -> dict[str, Any]:
        return {"bluez": {"adapter": endpoint.adapter}} if endpoint.adapter else {}

    async def _find(self, endpoint: BleEndpoint, *, timeout: float | None = None) -> Any:
        _, scanner = _load_bleak()
        wait = timeout or self.timeout
        kwargs = self._backend_kwargs(endpoint)
        if endpoint.address:
            device = await scanner.find_device_by_address(
                endpoint.address,
                timeout=wait,
                **kwargs,
            )
        else:
            device = await scanner.find_device_by_name(
                endpoint.name,
                timeout=wait,
                **kwargs,
            )
        if device is None:
            raise HardwareError(
                f"watch {endpoint.label!r} was not advertising within {wait:g} seconds"
            )
        return device

    @staticmethod
    def _is_authentication_rejection(error: Exception) -> bool:
        message = str(error).casefold()
        return any(
            marker in message
            for marker in (
                "insufficient authentication",
                "authentication required",
                "not authorized",
                "not permitted",
                "att error: 0x05",
                "att error 0x05",
            )
        )

    async def _read_public(
        self,
        endpoint: BleEndpoint,
        *,
        check_authentication_gate: bool,
    ) -> tuple[Any, bytes, int, str, bool | None]:
        client_type, _ = _load_bleak()
        device = await self._find(endpoint)
        async with client_type(
            device,
            timeout=self.timeout,
            **self._backend_kwargs(endpoint),
        ) as client:
            status = bytes(await client.read_gatt_char(self.contract.companion_status))
            battery = bytes(await client.read_gatt_char(self.contract.battery))
            revision = bytes(
                await client.read_gatt_char(self.contract.firmware_revision)
            ).decode("utf-8", errors="replace").rstrip("\0")
            authentication_rejected: bool | None = None
            if check_authentication_gate:
                try:
                    await client.read_gatt_char(self.contract.companion_verify)
                except Exception as error:
                    if not self._is_authentication_rejection(error):
                        raise HardwareError(
                            "pre-pair verify failed for a reason other than the expected "
                            f"ATT authentication rejection: {error}"
                        ) from error
                    authentication_rejected = True
                else:
                    authentication_rejected = False
        if len(battery) != 1:
            raise HardwareError(
                f"battery characteristic returned {len(battery)} bytes instead of 1"
            )
        return device, status, battery[0], revision, authentication_rejected

    @staticmethod
    def _cccd_descriptor(client: Any, characteristic_uuid: str) -> Any:
        characteristic = client.services.get_characteristic(characteristic_uuid)
        if characteristic is None:
            raise HardwareError("battery characteristic is absent after discovery")
        for descriptor in characteristic.descriptors:
            if str(descriptor.uuid).lower() == CCCD_UUID:
                return characteristic, descriptor
        raise HardwareError("battery characteristic has no CCCD descriptor")

    async def probe(
        self,
        endpoint: BleEndpoint,
        *,
        verify_cccd: bool = True,
        require_authentication_rejection: bool = True,
        allow_eviction: bool = False,
    ) -> dict[str, Any]:
        client_type, _ = _load_bleak()
        device, public_payload, battery, revision, authentication_rejected = (
            await self._read_public(
                endpoint,
                check_authentication_gate=require_authentication_rejection,
            )
        )
        if require_authentication_rejection and not authentication_rejected:
            raise HardwareError(
                "companion verify was readable before an unbonded authentication attempt; "
                "run this check from a fresh OS identity or pass --skip-auth-negative"
            )
        public = parse_companion_status(public_payload)
        if public.bonded_count >= public.capacity and not allow_eviction:
            raise HardwareError(
                f"watch reports {public.bonded_count}/{public.capacity} retained peers; "
                "pairing a new identity can evict the LRU peer. Retry with explicit "
                "--allow-eviction only after confirming that outcome."
            )
        notifications: list[str] = []

        async with client_type(
            device,
            timeout=self.timeout,
            pair=True,
            **self._backend_kwargs(endpoint),
        ) as client:
            verified_payload = bytes(
                await client.read_gatt_char(self.contract.companion_verify)
            )
            verified = parse_companion_status(verified_payload)
            _validate_status_pair(public, verified)
            if verify_cccd:
                battery_characteristic, _ = self._cccd_descriptor(
                    client,
                    self.contract.battery,
                )

                def notified(_sender: Any, value: bytearray) -> None:
                    notifications.append(bytes(value).hex())

                await client.start_notify(battery_characteristic, notified)
                await asyncio.sleep(0.5)

        cccd_value: str | None = None
        if verify_cccd:
            device = await self._find(endpoint)
            async with client_type(
                device,
                timeout=self.timeout,
                **self._backend_kwargs(endpoint),
            ) as client:
                _, descriptor = self._cccd_descriptor(
                    client,
                    self.contract.battery,
                )
                raw_cccd = bytes(await client.read_gatt_descriptor(descriptor.handle))
                cccd_value = raw_cccd.hex()
                if raw_cccd != b"\x01\x00":
                    raise HardwareError(
                        "battery CCCD did not restore as notify-enabled after reconnect "
                        f"(read {raw_cccd.hex() or '<empty>'})"
                    )

        return {
            "endpoint": asdict(endpoint),
            "firmware_revision": revision,
            "battery_percent": battery,
            "public_status": _status_dict(public),
            "verified_status": _status_dict(verified),
            "authentication_gate_negative_checked": require_authentication_rejection,
            "authentication_gate_rejected_unpaired_read": authentication_rejected,
            "authenticated_verify_read": True,
            "cccd_restore_checked": verify_cccd,
            "battery_cccd_after_reconnect": cccd_value,
            "notifications_during_subscription": notifications,
        }

    async def read_battery(self, endpoint: BleEndpoint) -> int:
        client_type, _ = _load_bleak()
        device = await self._find(endpoint)
        async with client_type(
            device,
            timeout=self.timeout,
            **self._backend_kwargs(endpoint),
        ) as client:
            payload = bytes(await client.read_gatt_char(self.contract.battery))
        if len(payload) != 1:
            raise HardwareError(
                f"battery characteristic returned {len(payload)} bytes instead of 1"
            )
        return payload[0]

    async def observe_advertising(
        self,
        endpoint: BleEndpoint,
        *,
        duration: float,
        interval: float,
        scan_window: float,
        on_sample: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        if duration <= 0 or interval <= 0 or scan_window <= 0:
            raise ValueError("advertising duration, interval, and scan window must be positive")
        if scan_window > interval:
            raise ValueError("scan window cannot exceed the observation interval")
        samples: list[dict[str, Any]] = []
        deadline = time.monotonic() + duration
        while True:
            started = time.monotonic()
            try:
                device = await self._find(endpoint, timeout=scan_window)
                sample = {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "visible": True,
                    "address": getattr(device, "address", None),
                    "name": getattr(device, "name", None),
                }
            except HardwareError:
                sample = {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "visible": False,
                    "address": endpoint.address,
                    "name": endpoint.name,
                }
            samples.append(sample)
            if on_sample is not None:
                on_sample(sample)
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(
                min(max(0.0, interval - (time.monotonic() - started)), deadline - time.monotonic())
            )
        return samples

    async def observe_pair(
        self,
        candidate: BleEndpoint,
        baseline: BleEndpoint,
        *,
        duration: float,
        interval: float,
        scan_window: float,
        on_sample: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if duration <= 0 or interval <= 0 or scan_window <= 0:
            raise ValueError("advertising duration, interval, and scan window must be positive")
        if scan_window > interval:
            raise ValueError("scan window cannot exceed the observation interval")
        if candidate.adapter != baseline.adapter:
            raise ValueError("candidate and baseline must use the same BlueZ adapter")
        _, scanner_type = _load_bleak()
        candidate_samples: list[dict[str, Any]] = []
        baseline_samples: list[dict[str, Any]] = []
        deadline = time.monotonic() + duration
        while True:
            started = time.monotonic()
            found: dict[str, Any] = {}

            def detected(device: Any, advertisement: Any) -> None:
                address = str(getattr(device, "address", "")).casefold()
                local_name = getattr(advertisement, "local_name", None)
                device_name = getattr(device, "name", None)
                for label, endpoint in (("candidate", candidate), ("baseline", baseline)):
                    if endpoint.address and address == endpoint.address.casefold():
                        found[label] = device
                    elif endpoint.name and endpoint.name in {local_name, device_name}:
                        found[label] = device

            async with scanner_type(
                detected,
                **self._backend_kwargs(candidate),
            ):
                await asyncio.sleep(scan_window)

            timestamp = datetime.now(UTC).isoformat()
            current: dict[str, dict[str, Any]] = {}
            for label, endpoint, destination in (
                ("candidate", candidate, candidate_samples),
                ("baseline", baseline, baseline_samples),
            ):
                device = found.get(label)
                sample = {
                    "timestamp": timestamp,
                    "visible": device is not None,
                    "address": getattr(device, "address", endpoint.address),
                    "name": getattr(device, "name", endpoint.name),
                }
                destination.append(sample)
                current[label] = sample
            if on_sample is not None:
                on_sample(current["candidate"], current["baseline"])
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(
                min(max(0.0, interval - (time.monotonic() - started)), deadline - time.monotonic())
            )
        return candidate_samples, baseline_samples


def _run_adb(
    serial: str,
    arguments: Sequence[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    return runner(
        ["adb", "-s", serial, *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def parse_adb_devices(output: str) -> dict[str, str]:
    devices: dict[str, str] = {}
    for line in output.splitlines()[1:]:
        columns = line.strip().split()
        if len(columns) >= 2:
            devices[columns[0]] = columns[1]
    return devices


class AdbHardware:
    def __init__(
        self,
        serial: str,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        if not serial.strip():
            raise ValueError("ADB serial cannot be empty")
        self.serial = serial
        self.runner = runner

    def _run(self, *arguments: str) -> str:
        result = _run_adb(self.serial, arguments, runner=self.runner)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise HardwareError(
                f"adb {self.serial} {' '.join(arguments)} failed: {detail}"
            )
        return result.stdout

    def verify(self) -> None:
        result = self.runner(
            ["adb", "devices"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise HardwareError("adb is unavailable")
        state = parse_adb_devices(result.stdout).get(self.serial)
        if state != "device":
            raise HardwareError(
                f"ADB device {self.serial!r} is not ready (state: {state or 'missing'})"
            )
        self._run("shell", "pm", "path", ANDROID_PACKAGE)

    def launch_app(self) -> None:
        self.verify()
        self._run(
            "shell",
            "monkey",
            "-p",
            ANDROID_PACKAGE,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        )

    def open_bluetooth_settings(self) -> None:
        self.verify()
        self._run(
            "shell",
            "am",
            "start",
            "-a",
            "android.settings.BLUETOOTH_SETTINGS",
        )

    def snapshot(self, watch_address: str | None = None) -> dict[str, Any]:
        self.verify()
        sdk = self._run("shell", "getprop", "ro.build.version.sdk").strip()
        model = self._run("shell", "getprop", "ro.product.model").strip()
        bluetooth = self._run("shell", "dumpsys", "bluetooth_manager")
        selected_lines = []
        address = watch_address.casefold() if watch_address else None
        for line in bluetooth.splitlines():
            folded = line.casefold()
            if (
                "enabled:" in folded
                or "state:" in folded
                or (address is not None and address in folded)
            ):
                selected_lines.append(line.strip())
        return {
            "serial": self.serial,
            "model": model,
            "android_sdk": sdk,
            "package": ANDROID_PACKAGE,
            "watch_address": watch_address,
            "bluetooth_evidence": selected_lines,
        }


@dataclass(frozen=True)
class AcceptancePeer:
    label: str
    kind: str
    address: str | None = None
    name: str | None = None
    adapter: str | None = None
    serial: str | None = None
    allow_eviction: bool = False


@dataclass(frozen=True)
class AcceptancePlan:
    watch_label: str
    peers: tuple[AcceptancePeer, ...]
    sequence: tuple[str, ...]
    capacity_check: dict[str, str] | None = None

    @classmethod
    def load(cls, path: Path) -> AcceptancePlan:
        raw = orjson.loads(path.read_bytes())
        if raw.get("schema_version") != 1:
            raise ValueError("hardware acceptance plan schema_version must be 1")
        watch_label = str(raw.get("watch_label", "")).strip()
        if not watch_label:
            raise ValueError("hardware acceptance plan needs watch_label")
        peers = tuple(AcceptancePeer(**item) for item in raw.get("peers", []))
        labels = [peer.label for peer in peers]
        if len(peers) < 2 or len(set(labels)) != len(labels):
            raise ValueError("hardware acceptance plan needs at least two uniquely named peers")
        for peer in peers:
            if peer.kind == "linux":
                BleEndpoint(peer.address, peer.name, peer.adapter)
            elif peer.kind == "android":
                if not peer.serial:
                    raise ValueError(f"Android peer {peer.label!r} needs an ADB serial")
            else:
                raise ValueError(f"unsupported peer kind {peer.kind!r}")
        identities = [
            ("android", peer.serial)
            if peer.kind == "android"
            else ("linux", peer.adapter or "default")
            for peer in peers
        ]
        if len(set(identities)) != len(identities):
            raise ValueError(
                "each acceptance peer must use an independent Android serial or BlueZ adapter"
            )
        sequence = tuple(raw.get("sequence", []))
        if len(sequence) < 3 or len(set(sequence)) < 2:
            raise ValueError("acceptance sequence must exercise at least two peers over three steps")
        unknown = set(sequence) - set(labels)
        if unknown:
            raise ValueError(
                f"acceptance sequence references unknown peers: {', '.join(sorted(unknown))}"
            )
        capacity = raw.get("capacity_check")
        if capacity is not None:
            required = {"survivor", "evicted", "new_peer"}
            if set(capacity) != required:
                raise ValueError(
                    "capacity_check must contain survivor, evicted, and new_peer"
                )
            if not set(capacity.values()) <= set(labels):
                raise ValueError("capacity_check references an unknown peer")
            if len(peers) < 6:
                raise ValueError("capacity_check needs at least six independent peers")
            if len(set(capacity.values())) != 3:
                raise ValueError("capacity_check must name three distinct peers")
        return cls(watch_label, peers, sequence, capacity)


def _operator_result(prompt: str) -> str:
    while True:
        answer = input(f"{prompt} [PASS/FAIL/SKIP]: ").strip().upper()
        if answer in {"PASS", "FAIL", "SKIP"}:
            return answer.lower()


async def run_acceptance_plan(
    workspace: Workspace,
    plan: AcceptancePlan,
    *,
    operator: Callable[[str], str] = _operator_result,
    checkpoint: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    contract = CharacteristicContract.load(workspace)
    bleak = BleakHardware(contract)
    peers = {peer.label: peer for peer in plan.peers}
    steps: list[dict[str, Any]] = []

    def result(status: str, capacity: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "watch_label": plan.watch_label,
            "status": status,
            "started_at": started_at,
            "updated_at": datetime.now(UTC).isoformat(),
            "steps": steps,
            "capacity_check": capacity,
            "fidelity": {
                "linux": "real BlueZ scan, GATT, authenticated verify, and CCCD readback",
                "android": "ADB launch/diagnostics plus explicit operator observation",
                "radio": "advertising visibility only; no RF timing or electrical-current claim",
            },
        }

    if checkpoint is not None:
        checkpoint(result("running"))
    first_linux_probe = True
    for index, label in enumerate(plan.sequence, start=1):
        peer = peers[label]
        try:
            if peer.kind == "linux":
                evidence = await bleak.probe(
                    BleEndpoint(peer.address, peer.name, peer.adapter),
                    require_authentication_rejection=first_linux_probe,
                    allow_eviction=peer.allow_eviction,
                )
                first_linux_probe = False
                status = "pass"
            else:
                adb = AdbHardware(peer.serial or "")
                adb.launch_app()
                evidence = adb.snapshot(peer.address)
                status = operator(
                    f"Step {index}: on {label}, pair only if Android requests it, "
                    f"read {plan.watch_label}'s battery, then release the connection"
                )
                if status not in {"pass", "fail", "skip"}:
                    raise ValueError(
                        f"operator returned unsupported result {status!r}"
                    )
        except (HardwareError, OSError, subprocess.SubprocessError, ValueError) as error:
            evidence = {"error": f"{type(error).__name__}: {error}"}
            status = "fail"
        except Exception as error:
            if not type(error).__module__.startswith("bleak"):
                raise
            evidence = {"error": f"{type(error).__name__}: {error}"}
            status = "fail"
        steps.append(
            {
                "index": index,
                "peer": label,
                "kind": peer.kind,
                "status": status,
                "evidence": evidence,
            }
        )
        if checkpoint is not None:
            checkpoint(result("running"))

    capacity: dict[str, Any] | None = None
    if plan.capacity_check is not None:
        instructions = (
            f"With five peers retained, touch {plan.capacity_check['survivor']} so it is MRU; "
            f"pair {plan.capacity_check['new_peer']} as the sixth peer; without repairing, "
            f"confirm {plan.capacity_check['survivor']} still verifies and "
            f"{plan.capacity_check['evicted']} no longer verifies"
        )
        capacity = {
            **plan.capacity_check,
            "status": operator(instructions),
            "instruction": instructions,
        }
        if capacity["status"] not in {"pass", "fail", "skip"}:
            raise ValueError(
                f"operator returned unsupported result {capacity['status']!r}"
            )
        if checkpoint is not None:
            checkpoint(result("running", capacity))

    statuses = [step["status"] for step in steps]
    if capacity is not None:
        statuses.append(capacity["status"])
    overall = (
        "failed"
        if "fail" in statuses
        else "incomplete"
        if "skip" in statuses
        else "passed"
    )
    final = result(overall, capacity)
    if checkpoint is not None:
        checkpoint(final)
    return final


async def run_soak(
    hardware: BleakHardware,
    candidate: BleEndpoint,
    baseline: BleEndpoint,
    *,
    duration: float,
    interval: float,
    scan_window: float,
    checkpoint: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if candidate == baseline:
        raise ValueError("candidate and baseline watches must differ")
    started = datetime.now(UTC).isoformat()
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "running",
        "started_at": started,
        "updated_at": datetime.now(UTC).isoformat(),
        "finished_at": None,
        "duration_seconds": duration,
        "sample_interval_seconds": interval,
        "scan_window_seconds": scan_window,
        "candidate": {
            "endpoint": asdict(candidate),
            "battery_start": None,
            "battery_end": None,
            "battery_delta": None,
            "advertising_samples": [],
        },
        "baseline": {
            "endpoint": asdict(baseline),
            "battery_start": None,
            "battery_end": None,
            "battery_delta": None,
            "advertising_samples": [],
        },
        "claim": (
            "Side-by-side software observation. Battery percentages are coarse watch "
            "telemetry; advertising samples prove visibility, not RF power or current."
        ),
    }
    if checkpoint is not None:
        checkpoint(result)
    try:
        result["candidate"]["battery_start"] = await hardware.read_battery(candidate)
        result["baseline"]["battery_start"] = await hardware.read_battery(baseline)
        result["updated_at"] = datetime.now(UTC).isoformat()
        if checkpoint is not None:
            checkpoint(result)

        def sampled(candidate_sample: dict[str, Any], baseline_sample: dict[str, Any]) -> None:
            result["candidate"]["advertising_samples"].append(candidate_sample)
            result["baseline"]["advertising_samples"].append(baseline_sample)
            result["updated_at"] = datetime.now(UTC).isoformat()
            if checkpoint is not None:
                checkpoint(result)

        await hardware.observe_pair(
            candidate,
            baseline,
            duration=duration,
            interval=interval,
            scan_window=scan_window,
            on_sample=sampled,
        )
        result["candidate"]["battery_end"] = await hardware.read_battery(candidate)
        result["baseline"]["battery_end"] = await hardware.read_battery(baseline)
        result["candidate"]["battery_delta"] = (
            result["candidate"]["battery_end"] - result["candidate"]["battery_start"]
        )
        result["baseline"]["battery_delta"] = (
            result["baseline"]["battery_end"] - result["baseline"]["battery_start"]
        )
        visibility_passed = all(
            sample["visible"]
            for watch in ("candidate", "baseline")
            for sample in result[watch]["advertising_samples"]
        )
        result["status"] = "passed" if visibility_passed else "failed"
        result["finished_at"] = datetime.now(UTC).isoformat()
        result["updated_at"] = result["finished_at"]
        if checkpoint is not None:
            checkpoint(result)
        return result
    except Exception as error:
        result["status"] = "failed"
        result["error"] = f"{type(error).__name__}: {error}"
        result["finished_at"] = datetime.now(UTC).isoformat()
        result["updated_at"] = result["finished_at"]
        if checkpoint is not None:
            checkpoint(result)
        raise


def default_hardware_root(workspace: Workspace) -> Path:
    return workspace.devtools / ".ptlab" / "hardware"


def save_hardware_result(
    workspace: Workspace,
    label: str,
    result: dict[str, Any],
    *,
    output: Path | None = None,
) -> Path:
    if output is None:
        output = _hardware_run_dir(default_hardware_root(workspace), label) / "result.json"
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
    write_hardware_result(output, result)
    return output


def prepare_hardware_output(
    workspace: Workspace,
    label: str,
    output: Path | None,
) -> Path:
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        return output
    return _hardware_run_dir(default_hardware_root(workspace), label) / "result.json"
