#!/usr/bin/env python3
"""Drive InfiniSim headlessly: start it on a virtual display, send touch/button
input, and capture screenshots of the 240x240 watch screen.

    ./simctl.py start
    ./simctl.py tap 120 120
    ./simctl.py swipe up
    ./simctl.py shot mainscreen
    ./simctl.py stop

Screenshots land in shots/ as PNGs. The simulator's own 'i' hotkey is used to
capture, so what you get is exactly the watch framebuffer, not a window grab.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INFINISIM = ROOT.parent / "InfiniSim"
BIN = INFINISIM / "build" / "infinisim"
LITTLEFS_DO = INFINISIM / "build" / "littlefs-do"
RESOURCES = INFINISIM / "build" / "resources"

RUN = ROOT / "run"          # simulator cwd: flash image, raw screenshots, logs
SHOTS = ROOT / "shots"      # named screenshots for review
STATE = RUN / "state.json"
LOG = RUN / "sim.log"

WINDOW_NAME = "TFT Simulator"   # lv_drivers/display/monitor.c
SCREEN = 240                    # watch is 240x240

# Simulator hotkeys (see InfiniSim README) -- exposed via `key` / these aliases.
EVENTS = {
    "ring": "r", "unring": "R",
    "buzz": "m", "buzz-long": "M",
    "notify": "n", "clear-notify": "N",
    "ble-connect": "b", "ble-disconnect": "B",
    "battery-up": "v", "battery-down": "V",
    "charging": "c", "not-charging": "C",
    "brightness-up": "l", "brightness-down": "L",
    "steps-up": "s", "steps-down": "S",
    "heartrate": "h", "heartrate-stop": "H",
    "weather": "w", "clear-weather": "W",
}


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def load_state():
    if not STATE.exists():
        die("simulator is not running (no state file). Run: ./simctl.py start")
    return json.loads(STATE.read_text())


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def xdo(state, *args, check=True):
    env = dict(os.environ, DISPLAY=state["display"])
    return subprocess.run(["xdotool", *map(str, args)], env=env,
                          check=check, capture_output=True, text=True)


# InfiniSim polls SDL_GetKeyboardState/SDL_GetMouseState once per LVGL refresh
# (LV_DISP_DEF_REFR_PERIOD, ~30ms) instead of consuming events, so an instant
# press-release lands between two polls and is lost. Hold every input instead.
HOLD = 0.15


def press_key(state, key):
    xdo(state, "windowfocus", "--sync", state["window"])
    xdo(state, "keydown", key)
    time.sleep(HOLD)
    xdo(state, "keyup", key)


def press_mouse(state, button):
    xdo(state, "mousedown", button)
    time.sleep(HOLD)
    xdo(state, "mouseup", button)


def free_display():
    for n in range(99, 130):
        if not Path(f"/tmp/.X{n}-lock").exists():
            return f":{n}"
    die("no free X display found")


def window_origin(state):
    """Absolute screen coords of the watch window's top-left pixel."""
    out = xdo(state, "getwindowgeometry", "--shell", state["window"]).stdout
    geo = dict(line.split("=", 1) for line in out.strip().splitlines())
    return int(geo["X"]), int(geo["Y"])


def to_screen(state, x, y):
    """Watch pixel (0-239) -> absolute X coords, honouring the zoom factor."""
    ox, oy = window_origin(state)
    z = state["zoom"]
    return ox + int(x) * z + z // 2, oy + int(y) * z + z // 2


# --------------------------------------------------------------------------
# lifecycle


def cmd_start(args):
    if STATE.exists():
        st = json.loads(STATE.read_text())
        if alive(st["sim_pid"]):
            print(f"already running on {st['display']} (pid {st['sim_pid']})")
            return
        cmd_stop(args, quiet=True)

    if not BIN.exists():
        die(f"{BIN} not built. Run: cmake --build {INFINISIM}/build -j$(nproc)")

    RUN.mkdir(parents=True, exist_ok=True)
    SHOTS.mkdir(parents=True, exist_ok=True)

    display = args.display or free_display()
    canvas = SCREEN * args.zoom + 200  # headroom so the window is never clipped

    xvfb = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", f"{canvas}x{canvas}x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    env = dict(os.environ, DISPLAY=display)
    for _ in range(100):  # wait for the X server to accept connections
        if subprocess.run(["xdotool", "getdisplaygeometry"], env=env,
                          capture_output=True).returncode == 0:
            break
        time.sleep(0.05)
    else:
        xvfb.kill()
        die("Xvfb failed to come up")

    seed_flash()

    log = open(LOG, "wb")
    # stdbuf keeps the sim's printf diagnostics line-buffered into sim.log
    cmd = ["stdbuf", "-oL", str(BIN), "--hide-status", "--gatt-bridge", str(args.bridge_port)]
    sim = subprocess.Popen(cmd, cwd=RUN, env=env, stdout=log, stderr=subprocess.STDOUT)

    window = None
    for _ in range(200):  # wait for the SDL window to be mapped
        r = subprocess.run(["xdotool", "search", "--name", WINDOW_NAME],
                           env=env, capture_output=True, text=True)
        ids = r.stdout.split()
        if ids:
            window = ids[-1]
            break
        if sim.poll() is not None:
            xvfb.kill()
            die(f"simulator exited immediately; see {LOG}")
        time.sleep(0.05)
    else:
        sim.kill()
        xvfb.kill()
        die("simulator window never appeared")

    state = {"display": display, "xvfb_pid": xvfb.pid, "sim_pid": sim.pid,
             "window": window, "zoom": args.zoom}
    STATE.write_text(json.dumps(state, indent=2))

    # The window must hold input focus or XTest keystrokes go nowhere: there is
    # no window manager on Xvfb to assign it for us.
    xdo(state, "windowfocus", "--sync", window)
    time.sleep(1.0)  # let InfiniTime boot into the watchface
    print(f"started on {display} (sim pid {sim.pid}, window {window}, zoom {args.zoom}x)")


def seed_flash():
    """Load resource.zip (fonts/images) into the emulated SPI flash, once."""
    flash = RUN / "spiNorFlash.raw"
    if flash.exists() or not LITTLEFS_DO.exists():
        return
    zips = sorted(RESOURCES.glob("infinitime-resources-*.zip"))
    if not zips:
        return
    subprocess.run([str(LITTLEFS_DO), "res", "load", str(zips[-1])],
                   cwd=RUN, capture_output=True)


def cmd_stop(args, quiet=False):
    if not STATE.exists():
        if not quiet:
            print("not running")
        return
    st = json.loads(STATE.read_text())
    for pid in (st.get("sim_pid"), st.get("xvfb_pid")):
        if pid and alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    time.sleep(0.3)
    for pid in (st.get("sim_pid"), st.get("xvfb_pid")):
        if pid and alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    STATE.unlink()
    if not quiet:
        print("stopped")


def cmd_restart(args):
    cmd_stop(args, quiet=True)
    cmd_start(args)


def cmd_rebuild(args):
    """Compile the InfiniTime fork into the simulator and restart it."""
    build = INFINISIM / "build"
    r = subprocess.run(["cmake", "--build", str(build), f"-j{os.cpu_count()}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        # only the errors matter; the upstream tree is noisy with warnings
        errs = [l for l in r.stdout.splitlines() + r.stderr.splitlines()
                if "error" in l.lower()]
        print("\n".join(errs[-40:] or r.stdout.splitlines()[-40:]), file=sys.stderr)
        die("build failed")
    print("build ok")
    cmd_restart(args)


def cmd_status(args):
    if not STATE.exists():
        print("not running")
        return
    st = json.loads(STATE.read_text())
    ok = alive(st["sim_pid"])
    print(f"display: {st['display']}\nsim pid: {st['sim_pid']} ({'alive' if ok else 'DEAD'})"
          f"\nwindow:  {st['window']}\nzoom:    {st['zoom']}x\nlog:     {LOG}")


def cmd_logs(args):
    if LOG.exists():
        text = LOG.read_text(errors="replace").splitlines()
        print("\n".join(text[-args.lines:]))


# --------------------------------------------------------------------------
# input


def settle(args):
    time.sleep(args.settle)


def screen_is_off():
    """The sim logs '[LCD] Sleep' / '[LCD] Wakeup'; last one wins. Fresh boot = on."""
    if not LOG.exists():
        return False
    state = False
    with open(LOG, "rb") as f:
        for line in f.read().decode(errors="replace").splitlines():
            if "[LCD] Sleep" in line:
                state = True
            elif "[LCD] Wakeup" in line:
                state = False
    return state


def cmd_awake(args):
    """Wake the screen only if it is actually off (button on a live watchface sleeps it!)."""
    st = load_state()
    if screen_is_off():
        x, y = to_screen(st, 120, 120)
        xdo(st, "mousemove", x, y)
        press_mouse(st, "3")
        settle(args)
        print("woke screen")
    else:
        print("already awake")


def cmd_tap(args):
    st = load_state()
    x, y = to_screen(st, args.x, args.y)
    xdo(st, "mousemove", x, y)
    press_mouse(st, "1")
    settle(args)
    print(f"tap ({args.x},{args.y})")


def cmd_drag(args):
    """Press, move in steps, release -- a real swipe, unlike the arrow-key shortcut."""
    st = load_state()
    x1, y1 = to_screen(st, args.x1, args.y1)
    x2, y2 = to_screen(st, args.x2, args.y2)
    xdo(st, "mousemove", x1, y1)
    xdo(st, "mousedown", "1")
    time.sleep(0.05)
    for i in range(1, args.steps + 1):
        t = i / args.steps
        xdo(st, "mousemove", int(x1 + (x2 - x1) * t), int(y1 + (y2 - y1) * t))
        time.sleep(0.04)
    xdo(st, "mouseup", "1")
    settle(args)
    print(f"drag ({args.x1},{args.y1}) -> ({args.x2},{args.y2})")


def cmd_swipe(args):
    """InfiniSim maps the arrow keys to swipe gestures."""
    st = load_state()
    press_key(st, args.direction.capitalize())
    settle(args)
    print(f"swipe {args.direction}")


def cmd_button(args):
    """Right mouse button == the PineTime's hardware side button."""
    st = load_state()
    x, y = to_screen(st, 120, 120)
    xdo(st, "mousemove", x, y)
    if args.hold:
        xdo(st, "mousedown", "3")
        time.sleep(args.hold / 1000)
        xdo(st, "mouseup", "3")
    else:
        press_mouse(st, "3")
    settle(args)
    print(f"button{f' (held {args.hold}ms)' if args.hold else ''}")


def cmd_key(args):
    st = load_state()
    k = EVENTS.get(args.key, args.key)
    for ch in k:
        # xdotool needs the keysym name for capitals, e.g. 'R' -> shift+r
        press_key(st, f"shift+{ch.lower()}" if ch.isupper() else ch)
        time.sleep(0.05)
    settle(args)
    print(f"key {args.key!r} -> {k!r}")


def cmd_shot(args):
    """Press 'i' and collect the PNG the simulator writes into its cwd."""
    st = load_state()
    SHOTS.mkdir(parents=True, exist_ok=True)
    before = set(RUN.glob("InfiniSim_*.png"))
    mark = time.time()

    press_key(st, "i")

    shot = None
    for _ in range(100):
        new = [p for p in RUN.glob("InfiniSim_*.png")
               if p not in before or p.stat().st_mtime >= mark]
        if new:
            shot = max(new, key=lambda p: p.stat().st_mtime)
            if shot.stat().st_size > 0:  # written fully?
                size = -1
                while size != shot.stat().st_size:
                    size = shot.stat().st_size
                    time.sleep(0.03)
                break
        time.sleep(0.05)
    else:
        die("no screenshot appeared -- is the window focused? check ./simctl.py logs")

    name = args.name or time.strftime("shot_%H%M%S")
    dest = SHOTS / (name if name.endswith(".png") else name + ".png")
    shutil.move(str(shot), dest)
    print(dest)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--settle", type=float, default=0.45,
                   help="seconds to wait after an action for the UI to redraw")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="boot the simulator on a virtual display")
    s.add_argument("--display", help="X display to use (default: first free)")
    s.add_argument("--zoom", type=int, default=1)
    s.add_argument("--bridge-port", type=int, default=18632, help="TCP GATT bridge port")
    s.set_defaults(func=cmd_start)

    sub.add_parser("stop").set_defaults(func=cmd_stop)
    s = sub.add_parser("restart", help="restart (use after rebuilding)")
    s.add_argument("--display")
    s.add_argument("--zoom", type=int, default=1)
    s.add_argument("--bridge-port", type=int, default=18632)
    s.set_defaults(func=cmd_restart)
    sub.add_parser("status").set_defaults(func=cmd_status)

    s = sub.add_parser("rebuild", help="compile the InfiniTime fork, then restart")
    s.add_argument("--display")
    s.add_argument("--zoom", type=int, default=1)
    s.add_argument("--bridge-port", type=int, default=18632)
    s.set_defaults(func=cmd_rebuild)

    s = sub.add_parser("logs")
    s.add_argument("-n", "--lines", type=int, default=40)
    s.set_defaults(func=cmd_logs)

    s = sub.add_parser("shot", help="capture the watch screen to shots/")
    s.add_argument("name", nargs="?")
    s.set_defaults(func=cmd_shot)

    sub.add_parser("awake", help="wake the screen only if it is off").set_defaults(func=cmd_awake)

    s = sub.add_parser("tap", help="tap at watch coords (0-239)")
    s.add_argument("x", type=int)
    s.add_argument("y", type=int)
    s.set_defaults(func=cmd_tap)

    s = sub.add_parser("drag", help="press-move-release swipe")
    s.add_argument("x1", type=int)
    s.add_argument("y1", type=int)
    s.add_argument("x2", type=int)
    s.add_argument("y2", type=int)
    s.add_argument("--steps", type=int, default=12)
    s.set_defaults(func=cmd_drag)

    s = sub.add_parser("swipe", help="gesture via arrow key")
    s.add_argument("direction", choices=["up", "down", "left", "right"])
    s.set_defaults(func=cmd_swipe)

    s = sub.add_parser("button", help="hardware side button (right click)")
    s.add_argument("--hold", type=int, metavar="MS", help="long press")
    s.set_defaults(func=cmd_button)

    s = sub.add_parser("key", help=f"hotkey or event alias ({', '.join(EVENTS)})")
    s.add_argument("key")
    s.set_defaults(func=cmd_key)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
