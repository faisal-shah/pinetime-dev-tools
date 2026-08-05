from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

from ptlab.hardware import (
    DEFAULT_MINIMUM_BATTERY_PERCENT,
    AcceptancePlan,
    AdbHardware,
    BleakHardware,
    BleEndpoint,
    CharacteristicContract,
    HardwareError,
    prepare_hardware_output,
    run_acceptance_plan,
    run_soak,
    save_hardware_result,
)
from ptlab.protocol import check_protocol
from ptlab.scenario import run_scenario
from ptlab.scenarios import select_scenarios
from ptlab.sim import SimError, SimLayout, Simulator, _print_state
from ptlab.workspace import WorkspaceError, discover_workspace


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ptlab", description="PineTime workspace orchestration")
    parser.add_argument(
        "--workspace-root",
        type=Path,
        help="directory containing the four PineTime sibling repositories",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    protocol = commands.add_parser("protocol", help="shared companion protocol operations")
    protocol_commands = protocol.add_subparsers(dest="protocol_command", required=True)
    protocol_commands.add_parser("check", help="verify every generated protocol target")

    sim = commands.add_parser("sim", help="manage the persistent simulator")
    sim_commands = sim.add_subparsers(dest="sim_command", required=True)
    start = sim_commands.add_parser("start", help="start InfiniSim on dynamic resources")
    start.add_argument("--display", help="explicit X display instead of dynamic allocation")
    start.add_argument("--bridge-port", type=int, help="explicit GATT bridge port instead of a free port")
    start.add_argument("--ble-control-port", type=int, help="explicit BLE-control port instead of a free port")
    start.add_argument("--headless", action="store_true", help="use SDL dummy video without X")
    start.add_argument("--zoom", type=int, default=1)
    sim_commands.add_parser("stop", help="stop only the simulator processes recorded by ptlab")
    sim_commands.add_parser("status", help="show simulator state and artifact paths")

    run = commands.add_parser("run", help="run isolated deterministic scenarios")
    run.add_argument("--target", choices=("sim",), default="sim")
    run.add_argument("--suite", required=True, help="scenario name or 'all'")
    run.add_argument(
        "--filter",
        action="append",
        default=[],
        help="retain scenarios whose name, description, or tag contains this text",
    )
    run.add_argument("--timeout", type=float, help="override each scenario timeout in seconds")
    run.add_argument(
        "--runs-dir",
        type=Path,
        help="run directory root (default: pinetime-dev-tools/.ptlab/runs)",
    )

    listing = commands.add_parser("list", help="list registered scenarios")
    listing.add_argument("--target", choices=("sim",), default="sim")
    listing.add_argument("--filter", action="append", default=[])

    hardware = commands.add_parser("hardware", help="physical watch acceptance and soak tools")
    hardware_commands = hardware.add_subparsers(dest="hardware_command", required=True)

    probe = hardware_commands.add_parser(
        "probe",
        help="verify real Linux GATT, authenticated pairing, and CCCD restore",
    )
    _add_ble_endpoint_arguments(probe)
    probe.add_argument("--timeout", type=float, default=30.0)
    probe.add_argument("--skip-cccd", action="store_true")
    probe.add_argument(
        "--allow-eviction",
        action="store_true",
        help="allow a full watch to evict its least-recently-used peer",
    )
    probe.add_argument(
        "--skip-auth-negative",
        action="store_true",
        help="do not require a fresh unbonded identity to prove verify is protected",
    )
    probe.add_argument("--output", type=Path)

    advertise = hardware_commands.add_parser(
        "advertise",
        help="passively sample long-idle advertising visibility",
    )
    _add_ble_endpoint_arguments(advertise)
    advertise.add_argument("--duration", type=float, required=True, help="total seconds")
    advertise.add_argument("--interval", type=float, default=60.0, help="seconds between scans")
    advertise.add_argument("--scan-window", type=float, default=5.0)
    advertise.add_argument("--output", type=Path)

    soak = hardware_commands.add_parser(
        "soak",
        help="side-by-side candidate/upstream battery and advertising observation",
    )
    soak.add_argument("--candidate-address", required=True)
    soak.add_argument("--baseline-address", required=True)
    soak.add_argument("--adapter")
    soak.add_argument("--duration", type=float, required=True, help="total seconds")
    soak.add_argument("--interval", type=float, default=3600.0)
    soak.add_argument("--scan-window", type=float, default=10.0)
    soak.add_argument("--timeout", type=float, default=30.0)
    soak.add_argument("--output", type=Path)

    android = hardware_commands.add_parser(
        "android",
        help="prepare or diagnose an Android companion through public ADB surfaces",
    )
    android.add_argument("--serial", required=True)
    android.add_argument(
        "--action",
        choices=("launch", "bluetooth-settings", "snapshot"),
        required=True,
    )
    android.add_argument("--watch-address")
    android.add_argument("--output", type=Path)

    accept = hardware_commands.add_parser(
        "accept",
        help="run an operator-assisted multi-central hardware plan",
    )
    accept.add_argument("--plan", type=Path, required=True)
    accept.add_argument(
        "--battery-percent",
        type=int,
        required=True,
        help="watch-reported estimate immediately before the run",
    )
    accept.add_argument(
        "--battery-mv",
        type=int,
        required=True,
        help="watch-reported battery voltage from Sys Info",
    )
    accept.add_argument(
        "--minimum-battery-percent",
        type=int,
        default=DEFAULT_MINIMUM_BATTERY_PERCENT,
    )
    accept.add_argument("--output", type=Path)
    return parser


def _add_ble_endpoint_arguments(parser: argparse.ArgumentParser) -> None:
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--address")
    identity.add_argument("--name")
    parser.add_argument("--adapter", help="BlueZ adapter such as hci0")


def _endpoint(args: argparse.Namespace) -> BleEndpoint:
    return BleEndpoint(args.address, args.name, args.adapter)


def _hardware_main(args: argparse.Namespace, workspace) -> int:
    def run(coroutine):
        try:
            return asyncio.run(coroutine)
        except Exception as error:
            if type(error).__module__.startswith("bleak"):
                raise HardwareError(str(error)) from error
            raise

    command = args.hardware_command
    if command == "android":
        adb = AdbHardware(args.serial)
        if args.action == "launch":
            adb.launch_app()
            print(f"launched {args.serial}")
            return 0
        if args.action == "bluetooth-settings":
            adb.open_bluetooth_settings()
            print(f"opened Bluetooth settings on {args.serial}")
            return 0
        result = adb.snapshot(args.watch_address)
        output = save_hardware_result(workspace, "android", result, output=args.output)
        print(output)
        return 0

    if command == "accept":
        output = prepare_hardware_output(workspace, "acceptance", args.output)
        result = run(
            run_acceptance_plan(
                workspace,
                AcceptancePlan.load(args.plan),
                reported_battery_percent=args.battery_percent,
                reported_battery_mv=args.battery_mv,
                minimum_battery_percent=args.minimum_battery_percent,
                checkpoint=lambda value: save_hardware_result(
                    workspace,
                    "acceptance",
                    value,
                    output=output,
                ),
            )
        )
        print(f"{result['status']}: {output}")
        return 0 if result["status"] == "passed" else 1

    contract = CharacteristicContract.load(workspace)
    hardware = BleakHardware(contract, timeout=getattr(args, "timeout", 30.0))
    if command == "probe":
        result = run(
            hardware.probe(
                _endpoint(args),
                verify_cccd=not args.skip_cccd,
                require_authentication_rejection=not args.skip_auth_negative,
                allow_eviction=args.allow_eviction,
            )
        )
        result["status"] = "passed"
        output = save_hardware_result(workspace, "probe", result, output=args.output)
        print(f"passed: {output}")
        return 0
    if command == "advertise":
        endpoint = _endpoint(args)
        output = prepare_hardware_output(workspace, "advertising", args.output)
        result = {
            "status": "running",
            "endpoint": {
                "address": endpoint.address,
                "name": endpoint.name,
                "adapter": endpoint.adapter,
            },
            "samples": [],
            "claim": "Advertising visibility only; no RF power or electrical-current claim.",
        }
        save_hardware_result(workspace, "advertising", result, output=output)

        def sampled(sample):
            result["samples"].append(sample)
            save_hardware_result(workspace, "advertising", result, output=output)

        try:
            samples = run(
                hardware.observe_advertising(
                    endpoint,
                    duration=args.duration,
                    interval=args.interval,
                    scan_window=args.scan_window,
                    on_sample=sampled,
                )
            )
        except Exception as error:
            result["status"] = "failed"
            result["error"] = f"{type(error).__name__}: {error}"
            save_hardware_result(workspace, "advertising", result, output=output)
            raise
        result["samples"] = samples
        result["status"] = "passed" if all(sample["visible"] for sample in samples) else "failed"
        save_hardware_result(workspace, "advertising", result, output=output)
        print(f"{result['status']}: {output}")
        return 0 if result["status"] == "passed" else 1

    output = prepare_hardware_output(workspace, "soak", args.output)
    result = run(
        run_soak(
            hardware,
            BleEndpoint(address=args.candidate_address, adapter=args.adapter),
            BleEndpoint(address=args.baseline_address, adapter=args.adapter),
            duration=args.duration,
            interval=args.interval,
            scan_window=args.scan_window,
            checkpoint=lambda value: save_hardware_result(
                workspace,
                "soak",
                value,
                output=output,
            ),
        )
    )
    print(output)
    return 0 if result["status"] == "passed" else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        workspace = discover_workspace(args.workspace_root)
        if args.command == "hardware":
            return _hardware_main(args, workspace)
        if args.command == "protocol":
            return check_protocol(workspace)

        if args.command == "list":
            selected = select_scenarios("all", args.filter)
            for scenario in selected:
                mode = "headless" if scenario.headless else "xvfb"
                print(
                    f"{scenario.name:24} {mode:8} {scenario.timeout:>5g}s "
                    f"[{','.join(sorted(scenario.tags))}] {scenario.description}"
                )
            return 0

        if args.command == "run":
            runs_root = args.runs_dir or workspace.devtools / ".ptlab" / "runs"
            selected = select_scenarios(args.suite, args.filter)
            if not selected:
                raise ValueError("scenario filters selected no suites")
            exit_code = 0
            for scenario in selected:
                code, paths, result = run_scenario(
                    workspace,
                    scenario,
                    runs_root=runs_root,
                    timeout=args.timeout,
                )
                exit_code = max(exit_code, code)
                print(
                    f"{scenario.name}: {result['status']} "
                    f"({result['check_count']} checks, {result['duration_seconds']:.3f}s) "
                    f"{paths.root}"
                )
            return exit_code

        simulator = Simulator(workspace, SimLayout.modern(workspace.devtools))
        if args.sim_command == "start":
            state = simulator.start(
                display=args.display,
                zoom=args.zoom,
                bridge_port=args.bridge_port,
                ble_control_port=args.ble_control_port,
                headless=args.headless,
            )
            print(
                f"started on {state['display'] or 'SDL dummy'} "
                f"(GATT {state['bridge_port']}, BLE control {state['ble_control_port']}, "
                f"sim pid {state['sim']['pid']})"
            )
        elif args.sim_command == "stop":
            print("stopped" if simulator.stop() else "not running")
        else:
            state = simulator.status()
            if state is None:
                print("not running")
            else:
                _print_state(state, simulator.layout)
        return 0
    except (
        WorkspaceError,
        SimError,
        HardwareError,
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
