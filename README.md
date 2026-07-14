# PineTime dev environment

Closed-loop setup for developing InfiniTime apps against the simulator:
edit source → rebuild → drive the UI → look at the screen.

## Layout

| Path | What it is |
|---|---|
| `../InfiniTime` | **Your fork** — firmware source. This is where apps get written. |
| `../InfiniSim` | Your fork of the simulator. Builds the firmware source into a desktop binary. |
| `simctl.py` | Runs the sim headlessly and sends it input / takes screenshots. |
| `shots/` | Captured screenshots (240×240 PNGs of the watch screen). |
| `run/` | Simulator working dir: emulated SPI flash, `sim.log`. |

InfiniSim vendors its own copy of InfiniTime at `InfiniSim/InfiniTime/` (a submodule
pointing at upstream). We ignore it: the build is configured with
`-DInfiniTime_DIR=/home/faisal/repos/faisal-shah/InfiniTime` so it compiles **your fork**.
If the sim ever seems to ignore your changes, check that setting first:

```sh
grep InfiniTime_DIR ../InfiniSim/build/CMakeCache.txt
```

## Workflow

```sh
./simctl.py start          # boot the sim on a hidden virtual display (Xvfb)
./simctl.py rebuild        # compile the fork + restart the sim — the inner dev loop
./simctl.py shot name      # capture the watch screen -> shots/name.png
./simctl.py awake          # wake the screen ONLY if it's off (state-aware, reads sim.log)
./simctl.py stop
./simctl.py kill           # SIGKILL, no graceful stop: power-loss simulation
```

The sim always starts with the **TCP GATT bridge** on port 18632 (`--bridge-port` to
change): the watch's BLE characteristics served over TCP, so companion-app code and
tests can talk to the simulated watch with real protocol bytes. `node bridge-test.mjs`
is the standing protocol regression (schedule sync incl. 64-event, out-of-order and
disconnect-mid-sync cases, digest, event read-back, violation handling, CTS
time-travel, notifications, battery) — run it after firmware protocol changes.

Scenario regressions (each leaves the sim running):
`./powerloss-test.sh` kills the sim mid-sync and proves (littlefs-do post-mortem)
that the active schedule survives and the partial staging file is cleaned up at
boot; `./reminder-fire-test.sh` (~2 min) proves a schedule reminder fires from
sleep with combined same-second titles and that dismiss works;
`./prayer-fire-test.sh` (~4 min) writes prayer settings, warps the clock to just
before a prayer, and proves the alert fires from sleep (RAM-only path, flash
tripwire armed), dismiss reschedules, settings survive a reboot, and alerts-off
suppresses the alert.

`simnav.py` is a screenshot-verified UI navigator (with retries) that codifies
the two sim input gotchas: key-injected swipes and button wakes don't reset
LVGL's idle-activity clock, so blind tap sequences randomly hit sleep; and the
reliable way home from any state is to hold the button until the Screen-is-OFF
overlay shows, then wake.

`companion-cli.mjs` is a second companion (and the computer-syncing story): it speaks the
same wire protocol through the bridge, so you can drive a two-device scenario locally.

```sh
node companion-cli.mjs list
node companion-cli.mjs add "Quran practice" 17:00 weekly Mon,Wed,Fri
node companion-cli.mjs delete "Quran practice"
```

`build-asan/` in InfiniSim is an AddressSanitizer build (`LD_PRELOAD` libasan to run);
a wake/sleep stress loop under it is the regression test for the LVGL thread race fixed
in `sim/displayapp/LvglGuard.h`.

Driving the watch:

```sh
./simctl.py tap 120 120         # touch, in watch coords (0-239)
./simctl.py drag 120 200 120 40 # press-move-release swipe
./simctl.py swipe up            # gesture shortcut (up/down/left/right)
./simctl.py button              # hardware side button (back / wake)
./simctl.py button --hold 2000  # long press
./simctl.py key notify          # simulate an event, see aliases below
```

Event aliases: `ring`, `unring`, `buzz`, `notify`, `clear-notify`, `ble-connect`,
`ble-disconnect`, `battery-up`, `battery-down`, `charging`, `not-charging`,
`brightness-up`, `brightness-down`, `steps-up`, `steps-down`, `heartrate`,
`heartrate-stop`, `weather`, `clear-weather`. Any raw InfiniSim hotkey also works
(`./simctl.py key i`).

The screen sleeps on its own; use `./simctl.py awake` (only presses the button when
the screen is actually off — a blind `button` on a live watchface puts it to sleep).
When driving multi-step UI flows, chain the steps in one shell command: per-command
latency is longer than the watch's display timeout.

## Why input is "held"

InfiniSim polls `SDL_GetKeyboardState` / `SDL_GetMouseState` once per LVGL refresh
(~30 ms) rather than consuming SDL events. An instant xdotool click or keystroke
lands between two polls and is silently dropped. `simctl.py` therefore holds every
input ~150 ms. If you drive the sim by other means, do the same.

## First-time setup (already done)

```sh
sudo apt install -y xvfb xdotool xauth        # only step needing root
cd ../InfiniSim && npm install lv_font_conv@1.5.2
cmake -S . -B build \
  -DInfiniTime_DIR=/home/faisal/repos/faisal-shah/InfiniTime \
  -DWITH_PNG=ON -DBUILD_RESOURCES=ON -DMONITOR_ZOOM=1 -DCMAKE_BUILD_TYPE=Debug
cmake --build build -j$(nproc)
```

`MONITOR_ZOOM=1` keeps window pixels 1:1 with watch pixels, so screenshot
coordinates and tap coordinates are the same numbers. `simctl.py` handles other
zoom levels, but 1 keeps things simple.

## Writing an app

An InfiniTime app lives in `../InfiniTime/src/displayapp/screens/`, is registered in
`src/displayapp/apps/Apps.h.in` + `UserApps.h`, and gets instantiated in
`DisplayApp.cpp`. To control which apps are compiled in, configure the sim with
`-DENABLE_USERAPPS="Apps::Timer,Apps::Alarm,..."`.
