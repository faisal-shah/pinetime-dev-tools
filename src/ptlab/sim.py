from __future__ import annotations

import argparse
import os
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orjson

from ptlab.allocation import allocate_tcp_port, display_is_active, xvfb_dynamic_command
from ptlab.processes import ProcessRef, process_is_alive, terminate_pids, terminate_process_groups
from ptlab.workspace import Workspace, WorkspaceError, discover_workspace


WINDOW_NAME = "TFT Simulator"
SCREEN = 240
HOLD = 0.15
EVENTS = {
    "ring": "r",
    "unring": "R",
    "buzz": "m",
    "buzz-long": "M",
    "notify": "n",
    "clear-notify": "N",
    "ble-connect": "b",
    "ble-disconnect": "B",
    "battery-up": "v",
    "battery-down": "V",
    "charging": "c",
    "not-charging": "C",
    "brightness-up": "l",
    "brightness-down": "L",
    "steps-up": "s",
    "steps-down": "S",
    "heartrate": "h",
    "heartrate-stop": "H",
    "weather": "w",
    "clear-weather": "W",
}


class SimError(RuntimeError):
    pass


@dataclass(frozen=True)
class SimLayout:
    root: Path
    run: Path
    flash: Path
    shots: Path
    logs: Path
    state: Path
    stdout: Path
    stderr: Path

    @classmethod
    def modern(cls, devtools: Path) -> SimLayout:
        root = devtools / ".ptlab" / "sim"
        return cls(
            root=root,
            run=root / "run",
            flash=root / "flash",
            shots=root / "shots",
            logs=root / "logs",
            state=root / "state.json",
            stdout=root / "logs" / "sim.stdout.log",
            stderr=root / "logs" / "sim.stderr.log",
        )

    @classmethod
    def legacy(cls, devtools: Path) -> SimLayout:
        run = devtools / "run"
        return cls(
            root=devtools,
            run=run,
            flash=run,
            shots=devtools / "shots",
            logs=run,
            state=run / "state.json",
            stdout=run / "sim.log",
            stderr=run / "sim.log",
        )

    def create(self) -> None:
        for path in {self.root, self.run, self.flash, self.shots, self.logs}:
            path.mkdir(parents=True, exist_ok=True)


def _dump_json(path: Path, value: Any) -> None:
    path.write_bytes(orjson.dumps(value, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS) + b"\n")


def _load_json(path: Path) -> dict[str, Any]:
    return orjson.loads(path.read_bytes())


def _process_refs(state: dict[str, Any]) -> list[ProcessRef]:
    refs = []
    for name in ("sim", "xvfb"):
        value = state.get(name)
        if isinstance(value, dict) and value.get("pid") and value.get("pgid"):
            refs.append(ProcessRef(int(value["pid"]), int(value["pgid"]), name))
    return refs


def _legacy_pids(state: dict[str, Any]) -> list[int]:
    return [
        int(state[key])
        for key in ("sim_pid", "xvfb_pid")
        if state.get(key)
    ]


def _state_pid(state: dict[str, Any], name: str) -> int:
    process = state.get(name)
    if isinstance(process, dict) and process.get("pid"):
        return int(process["pid"])
    return int(state.get(f"{name}_pid", 0))


def existing_state_is_healthy(
    state: dict[str, Any],
    *,
    alive: Callable[[int], bool] = process_is_alive,
    display_responsive: Callable[[int], bool] = display_is_active,
) -> bool:
    sim_pid = _state_pid(state, "sim")
    xvfb_pid = _state_pid(state, "xvfb")
    if state.get("headless"):
        return bool(sim_pid and alive(sim_pid))
    display = state.get("display")
    if not sim_pid or not xvfb_pid or not isinstance(display, str) or not display.startswith(":"):
        return False
    try:
        display_number = int(display[1:].split(".", 1)[0])
    except ValueError:
        return False
    return alive(sim_pid) and alive(xvfb_pid) and display_responsive(display_number)


def tcp_listener_accepting(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_tcp_listener(
    host: str,
    port: int,
    *,
    process_alive: Callable[[], bool],
    timeout: float = 5.0,
    probe: Callable[[str, int, float], bool] = tcp_listener_accepting,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if not process_alive():
            raise SimError(f"simulator exited before bridge port {port} became ready")
        if probe(host, port, min(0.2, max(0.01, deadline - monotonic()))):
            if not process_alive():
                raise SimError(f"simulator exited while bridge port {port} was starting")
            return
        sleep(min(0.05, max(0.0, deadline - monotonic())))
    if not process_alive():
        raise SimError(f"simulator exited before bridge port {port} became ready")
    raise SimError(f"bridge port {port} did not accept connections within {timeout:g} seconds")


def build_sim_state(
    *,
    display: str | None,
    bridge_port: int,
    window: str | None,
    zoom: int,
    sim_pid: int,
    xvfb_pid: int | None,
    layout: SimLayout,
    ble_control_port: int | None = None,
    headless: bool = False,
) -> dict[str, Any]:
    state = {
        "display": display,
        "bridge_port": bridge_port,
        "gatt_port": bridge_port,
        "ble_control_port": ble_control_port,
        "headless": headless,
        "window": window,
        "zoom": zoom,
        "sim_pid": sim_pid,
        "xvfb_pid": xvfb_pid,
        "sim": {"pid": sim_pid, "pgid": sim_pid},
        "paths": {
            "run": str(layout.run),
            "flash": str(layout.flash),
            "shots": str(layout.shots),
            "stdout": str(layout.stdout),
            "stderr": str(layout.stderr),
        },
    }
    if xvfb_pid is not None:
        state["xvfb"] = {"pid": xvfb_pid, "pgid": xvfb_pid}
    return state


class Simulator:
    def __init__(self, workspace: Workspace, layout: SimLayout) -> None:
        self.workspace = workspace
        self.layout = layout
        self.binary = workspace.infinisim / "build" / "infinisim"
        self.littlefs_do = workspace.infinisim / "build" / "littlefs-do"
        self.resources = workspace.infinisim / "build" / "resources"

    def load_state(self) -> dict[str, Any]:
        if not self.layout.state.exists():
            raise SimError("simulator is not running (no state file)")
        return _load_json(self.layout.state)

    def xdo(self, state: dict[str, Any], *args: object, check: bool = True) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, DISPLAY=str(state["display"]))
        return subprocess.run(
            ["xdotool", *map(str, args)],
            env=env,
            check=check,
            capture_output=True,
            text=True,
        )

    def _wait_for_x(self, display: str, xvfb: subprocess.Popen[bytes] | subprocess.Popen[str]) -> None:
        env = dict(os.environ, DISPLAY=display)
        for _ in range(100):
            if xvfb.poll() is not None:
                raise SimError(f"Xvfb exited before display {display} became ready")
            result = subprocess.run(
                ["xdotool", "getdisplaygeometry"],
                env=env,
                capture_output=True,
                check=False,
            )
            if result.returncode == 0:
                return
            time.sleep(0.05)
        raise SimError(f"Xvfb display {display} failed to come up")

    def _start_xvfb(self, canvas: int, display: str | None) -> tuple[subprocess.Popen[Any], str]:
        self.layout.logs.mkdir(parents=True, exist_ok=True)
        stderr = self.layout.logs / "xvfb.stderr.log"
        error_stream = stderr.open("ab")
        try:
            if display:
                process = subprocess.Popen(
                    ["Xvfb", display, "-screen", "0", f"{canvas}x{canvas}x24", "-nolisten", "tcp"],
                    stdout=subprocess.DEVNULL,
                    stderr=error_stream,
                    start_new_session=True,
                )
                chosen = display
            else:
                process = subprocess.Popen(
                    xvfb_dynamic_command(canvas),
                    stdout=subprocess.PIPE,
                    stderr=error_stream,
                    text=True,
                    start_new_session=True,
                )
                assert process.stdout is not None
                selector = selectors.DefaultSelector()
                selector.register(process.stdout, selectors.EVENT_READ)
                if not selector.select(timeout=5.0):
                    raise SimError("Xvfb did not allocate a display within 5 seconds")
                line = process.stdout.readline().strip()
                process.stdout.close()
                if not line.isdigit():
                    raise SimError(f"Xvfb returned an invalid display number: {line!r}")
                chosen = f":{line}"
        except Exception:
            if "process" in locals() and process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            raise
        finally:
            error_stream.close()
        try:
            self._wait_for_x(chosen, process)
        except Exception:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            raise
        return process, chosen

    def _seed_flash(self) -> None:
        flash = self.layout.flash / "spiNorFlash.raw"
        if flash.exists() or not self.littlefs_do.exists():
            return
        resources = sorted(self.resources.glob("infinitime-resources-*.zip"))
        if not resources:
            return
        subprocess.run(
            [str(self.littlefs_do), "res", "load", str(resources[-1])],
            cwd=self.layout.flash,
            capture_output=True,
            check=False,
        )

    def _link_flash(self) -> None:
        if self.layout.flash == self.layout.run:
            return
        source = self.layout.flash / "spiNorFlash.raw"
        target = self.layout.run / "spiNorFlash.raw"
        if target.is_symlink() and target.resolve(strict=False) == source:
            return
        if target.exists() or target.is_symlink():
            target.unlink()
        target.symlink_to(source)

    def start(
        self,
        *,
        display: str | None = None,
        zoom: int = 1,
        bridge_port: int | None = None,
        ble_control_port: int | None = None,
        headless: bool = False,
    ) -> dict[str, Any]:
        if self.layout.state.exists():
            state = _load_json(self.layout.state)
            if existing_state_is_healthy(state):
                return state
            self.stop(quiet=True)

        if not self.binary.exists():
            raise SimError(
                f"{self.binary} not built. Run: cmake --build {self.workspace.infinisim / 'build'} -j$(nproc)"
            )
        if zoom < 1:
            raise SimError("zoom must be at least 1")
        if bridge_port is None:
            bridge_port = allocate_tcp_port()
        if not 1 <= bridge_port <= 65535:
            raise SimError("bridge port must be between 1 and 65535")
        if ble_control_port is None:
            ble_control_port = allocate_tcp_port()
            while ble_control_port == bridge_port:
                ble_control_port = allocate_tcp_port()
        if not 1 <= ble_control_port <= 65535:
            raise SimError("BLE-control port must be between 1 and 65535")
        if ble_control_port == bridge_port:
            raise SimError("bridge and BLE-control ports must differ")
        if headless and display is not None:
            raise SimError("headless simulator cannot use an X display")

        self.layout.create()
        xvfb: subprocess.Popen[Any] | None = None
        sim: subprocess.Popen[Any] | None = None
        try:
            chosen_display: str | None = None
            if not headless:
                canvas = SCREEN * zoom + 200
                xvfb, chosen_display = self._start_xvfb(canvas, display)
            self._seed_flash()
            self._link_flash()

            env = dict(os.environ)
            if headless:
                env["SDL_VIDEODRIVER"] = "dummy"
                env.pop("DISPLAY", None)
            else:
                env["DISPLAY"] = str(chosen_display)
            command = [
                "stdbuf",
                "-oL",
                str(self.binary),
                "--hide-status",
                "--gatt-bridge",
                str(bridge_port),
                "--ble-control",
                str(ble_control_port),
            ]
            stdout = self.layout.stdout.open("ab")
            stderr = stdout if self.layout.stderr == self.layout.stdout else self.layout.stderr.open("ab")
            try:
                sim = subprocess.Popen(
                    command,
                    cwd=self.layout.run,
                    env=env,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
            finally:
                stdout.close()
                if stderr is not stdout:
                    stderr.close()

            window: str | None = None
            if not headless:
                for _ in range(200):
                    result = subprocess.run(
                        ["xdotool", "search", "--name", WINDOW_NAME],
                        env=env,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    windows = result.stdout.split()
                    if windows:
                        window = windows[-1]
                        break
                    if sim.poll() is not None:
                        raise SimError(f"simulator exited immediately; see {self.layout.stderr}")
                    time.sleep(0.05)
                if window is None:
                    raise SimError("simulator window never appeared")

            state = build_sim_state(
                display=chosen_display,
                bridge_port=bridge_port,
                ble_control_port=ble_control_port,
                window=window,
                zoom=zoom,
                sim_pid=sim.pid,
                xvfb_pid=xvfb.pid if xvfb is not None else None,
                layout=self.layout,
                headless=headless,
            )
            if not headless:
                self.xdo(state, "windowfocus", "--sync", window)
                time.sleep(1.0)
            wait_for_tcp_listener(
                "127.0.0.1",
                ble_control_port,
                process_alive=lambda: sim.poll() is None,
            )
            # The readiness probe briefly occupies the control server's single
            # client slot. Give the non-blocking main loop one turn to observe
            # EOF before returning the endpoint to the scenario.
            time.sleep(0.05)
            _dump_json(self.layout.state, state)
            return state
        except Exception:
            refs = []
            if sim is not None:
                refs.append(ProcessRef(sim.pid, sim.pid, "sim"))
            if xvfb is not None:
                refs.append(ProcessRef(xvfb.pid, xvfb.pid, "xvfb"))
            terminate_process_groups(refs, timeout=1.0)
            self.layout.state.unlink(missing_ok=True)
            raise

    def stop(self, *, quiet: bool = False) -> bool:
        if not self.layout.state.exists():
            return False
        state = _load_json(self.layout.state)
        refs = _process_refs(state)
        if refs:
            terminate_process_groups(refs)
        else:
            terminate_pids(_legacy_pids(state))
        self.layout.state.unlink(missing_ok=True)
        return True

    def kill(self) -> bool:
        if not self.layout.state.exists():
            return False
        state = _load_json(self.layout.state)
        refs = _process_refs(state)
        if refs:
            targets = ((ref.pid, ref.pgid, True) for ref in refs)
        else:
            targets = ((pid, pid, False) for pid in _legacy_pids(state))
        for pid, process_group, grouped in targets:
            if process_is_alive(pid):
                try:
                    if grouped:
                        os.killpg(process_group, signal.SIGKILL)
                    else:
                        os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.layout.state.unlink(missing_ok=True)
        return True

    def restart(self, **start_options: Any) -> dict[str, Any]:
        self.stop(quiet=True)
        return self.start(**start_options)

    def rebuild(self, **start_options: Any) -> dict[str, Any]:
        build = self.workspace.infinisim / "build"
        result = subprocess.run(
            ["cmake", "--build", str(build), f"-j{os.cpu_count()}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            lines = result.stdout.splitlines() + result.stderr.splitlines()
            errors = [line for line in lines if "error" in line.lower()]
            raise SimError("build failed\n" + "\n".join((errors or lines)[-40:]))
        return self.restart(**start_options)

    def status(self) -> dict[str, Any] | None:
        if not self.layout.state.exists():
            return None
        state = _load_json(self.layout.state)
        sim_ref = next((ref for ref in _process_refs(state) if ref.name == "sim"), None)
        sim_pid = sim_ref.pid if sim_ref else int(state.get("sim_pid", 0))
        state["alive"] = bool(sim_pid and process_is_alive(sim_pid))
        return state

    def logs(self, lines: int) -> str:
        if not self.layout.stdout.exists():
            return ""
        return "\n".join(self.layout.stdout.read_text(errors="replace").splitlines()[-lines:])

    def _window_origin(self, state: dict[str, Any]) -> tuple[int, int]:
        output = self.xdo(state, "getwindowgeometry", "--shell", state["window"]).stdout
        geometry = dict(line.split("=", 1) for line in output.strip().splitlines())
        return int(geometry["X"]), int(geometry["Y"])

    def _to_screen(self, state: dict[str, Any], x: int, y: int) -> tuple[int, int]:
        origin_x, origin_y = self._window_origin(state)
        zoom = int(state["zoom"])
        return origin_x + x * zoom + zoom // 2, origin_y + y * zoom + zoom // 2

    def _press_key(self, state: dict[str, Any], key: str) -> None:
        self.xdo(state, "windowfocus", "--sync", state["window"])
        self.xdo(state, "keydown", key)
        time.sleep(HOLD)
        self.xdo(state, "keyup", key)

    def _press_mouse(self, state: dict[str, Any], button: str) -> None:
        self.xdo(state, "mousedown", button)
        time.sleep(HOLD)
        self.xdo(state, "mouseup", button)

    def _screen_is_off(self) -> bool:
        if not self.layout.stdout.exists():
            return False
        off = False
        for line in self.layout.stdout.read_text(errors="replace").splitlines():
            if "[LCD] Sleep" in line:
                off = True
            elif "[LCD] Wakeup" in line:
                off = False
        return off

    def awake(self, settle: float) -> bool:
        state = self.load_state()
        if not self._screen_is_off():
            return False
        x, y = self._to_screen(state, 120, 120)
        self.xdo(state, "mousemove", x, y)
        self._press_mouse(state, "3")
        time.sleep(settle)
        return True

    def tap(self, x: int, y: int, settle: float) -> None:
        state = self.load_state()
        screen_x, screen_y = self._to_screen(state, x, y)
        self.xdo(state, "mousemove", screen_x, screen_y)
        self._press_mouse(state, "1")
        time.sleep(settle)

    def drag(self, x1: int, y1: int, x2: int, y2: int, steps: int, settle: float) -> None:
        state = self.load_state()
        start_x, start_y = self._to_screen(state, x1, y1)
        end_x, end_y = self._to_screen(state, x2, y2)
        self.xdo(state, "mousemove", start_x, start_y)
        self.xdo(state, "mousedown", "1")
        time.sleep(0.05)
        for index in range(1, steps + 1):
            fraction = index / steps
            self.xdo(
                state,
                "mousemove",
                int(start_x + (end_x - start_x) * fraction),
                int(start_y + (end_y - start_y) * fraction),
            )
            time.sleep(0.04)
        self.xdo(state, "mouseup", "1")
        time.sleep(settle)

    def swipe(self, direction: str, settle: float) -> None:
        state = self.load_state()
        self._press_key(state, direction.capitalize())
        time.sleep(settle)

    def button(self, hold: int | None, settle: float) -> None:
        state = self.load_state()
        x, y = self._to_screen(state, 120, 120)
        self.xdo(state, "mousemove", x, y)
        if hold:
            self.xdo(state, "mousedown", "3")
            time.sleep(hold / 1000)
            self.xdo(state, "mouseup", "3")
        else:
            self._press_mouse(state, "3")
        time.sleep(settle)

    def key(self, key: str, settle: float) -> str:
        state = self.load_state()
        translated = EVENTS.get(key, key)
        for character in translated:
            keysym = f"shift+{character.lower()}" if character.isupper() else character
            self._press_key(state, keysym)
            time.sleep(0.05)
        time.sleep(settle)
        return translated

    def shot(self, name: str | None) -> Path:
        state = self.load_state()
        self.layout.shots.mkdir(parents=True, exist_ok=True)
        before = set(self.layout.run.glob("InfiniSim_*.png"))
        mark = time.time()
        self._press_key(state, "i")
        shot = None
        for _ in range(100):
            candidates = [
                path
                for path in self.layout.run.glob("InfiniSim_*.png")
                if path not in before or path.stat().st_mtime >= mark
            ]
            if candidates:
                shot = max(candidates, key=lambda path: path.stat().st_mtime)
                if shot.stat().st_size > 0:
                    size = -1
                    while size != shot.stat().st_size:
                        size = shot.stat().st_size
                        time.sleep(0.03)
                    break
            time.sleep(0.05)
        if shot is None:
            raise SimError("no screenshot appeared; check simulator logs and window focus")
        filename = name or time.strftime("shot_%H%M%S")
        destination = self.layout.shots / (filename if filename.endswith(".png") else f"{filename}.png")
        shutil.move(shot, destination)
        return destination


def _add_start_arguments(parser: argparse.ArgumentParser, *, legacy: bool) -> None:
    parser.add_argument("--display", help="X display to use (default: dynamically allocated)")
    parser.add_argument("--zoom", type=int, default=1)
    parser.add_argument(
        "--bridge-port",
        type=int,
        default=18632 if legacy else None,
        help="TCP GATT bridge port (default: 18632 for simctl, dynamic for ptlab)",
    )


def build_legacy_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Drive InfiniSim headlessly")
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--settle", type=float, default=0.45)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "restart", "rebuild"):
        _add_start_arguments(commands.add_parser(name), legacy=True)
    commands.add_parser("stop")
    commands.add_parser("kill")
    commands.add_parser("status")
    logs = commands.add_parser("logs")
    logs.add_argument("-n", "--lines", type=int, default=40)
    shot = commands.add_parser("shot")
    shot.add_argument("name", nargs="?")
    commands.add_parser("awake")
    tap = commands.add_parser("tap")
    tap.add_argument("x", type=int)
    tap.add_argument("y", type=int)
    drag = commands.add_parser("drag")
    drag.add_argument("x1", type=int)
    drag.add_argument("y1", type=int)
    drag.add_argument("x2", type=int)
    drag.add_argument("y2", type=int)
    drag.add_argument("--steps", type=int, default=12)
    swipe = commands.add_parser("swipe")
    swipe.add_argument("direction", choices=["up", "down", "left", "right"])
    button = commands.add_parser("button")
    button.add_argument("--hold", type=int, metavar="MS")
    key = commands.add_parser("key")
    key.add_argument("key")
    return parser


def _print_state(state: dict[str, Any], layout: SimLayout) -> None:
    sim_ref = next((ref for ref in _process_refs(state) if ref.name == "sim"), None)
    pid = sim_ref.pid if sim_ref else state.get("sim_pid", "unknown")
    alive = state.get("alive", bool(isinstance(pid, int) and process_is_alive(pid)))
    print(
        f"display: {state.get('display') or 'SDL dummy'}\n"
        f"GATT port: {state.get('gatt_port', state.get('bridge_port', 'unknown'))}\n"
        f"BLE-control port: {state.get('ble_control_port', 'unknown')}\n"
        f"sim pid: {pid} ({'alive' if alive else 'DEAD'})\n"
        f"window:  {state.get('window') or 'none'}\n"
        f"zoom:    {state['zoom']}x\n"
        f"stdout:  {layout.stdout}\n"
        f"stderr:  {layout.stderr}"
    )


def execute_legacy(args: argparse.Namespace, simulator: Simulator) -> int:
    command = args.command
    start_options = {
        "display": getattr(args, "display", None),
        "zoom": getattr(args, "zoom", 1),
        "bridge_port": getattr(args, "bridge_port", 18632),
    }
    if command == "start":
        state = simulator.start(**start_options)
        print(f"started on {state['display']} (sim pid {state['sim']['pid']}, window {state['window']})")
    elif command == "stop":
        print("stopped" if simulator.stop() else "not running")
    elif command == "kill":
        print("killed simulator (power loss); flash image left as-is" if simulator.kill() else "not running")
    elif command == "restart":
        state = simulator.restart(**start_options)
        print(f"restarted on {state['display']} (sim pid {state['sim']['pid']})")
    elif command == "rebuild":
        state = simulator.rebuild(**start_options)
        print(f"build ok; restarted on {state['display']} (sim pid {state['sim']['pid']})")
    elif command == "status":
        state = simulator.status()
        if state is None:
            print("not running")
        else:
            _print_state(state, simulator.layout)
    elif command == "logs":
        print(simulator.logs(args.lines))
    elif command == "shot":
        print(simulator.shot(args.name))
    elif command == "awake":
        print("woke screen" if simulator.awake(args.settle) else "already awake")
    elif command == "tap":
        simulator.tap(args.x, args.y, args.settle)
        print(f"tap ({args.x},{args.y})")
    elif command == "drag":
        simulator.drag(args.x1, args.y1, args.x2, args.y2, args.steps, args.settle)
        print(f"drag ({args.x1},{args.y1}) -> ({args.x2},{args.y2})")
    elif command == "swipe":
        simulator.swipe(args.direction, args.settle)
        print(f"swipe {args.direction}")
    elif command == "button":
        simulator.button(args.hold, args.settle)
        suffix = f" (held {args.hold}ms)" if args.hold else ""
        print(f"button{suffix}")
    elif command == "key":
        translated = simulator.key(args.key, args.settle)
        print(f"key {args.key!r} -> {translated!r}")
    return 0


def legacy_main(argv: list[str] | None = None) -> int:
    args = build_legacy_parser().parse_args(argv)
    try:
        workspace = discover_workspace(args.workspace_root)
        simulator = Simulator(workspace, SimLayout.legacy(workspace.devtools))
        return execute_legacy(args, simulator)
    except (WorkspaceError, SimError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
