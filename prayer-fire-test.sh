#!/bin/bash
# Prayer-alert regression for the flash-free alert path.
#
# Writes prayer settings (NYC, ISNA - Dhuhr on 2026-07-14 is 13:02, a frozen
# golden vector), warps the watch clock to just before Dhuhr, lets the sim
# idle-sleep and checks the alert fires from sleep (the timer callback is
# RAM-only; the sim's flash-asleep tripwire aborts on any violation). Then:
# dismiss works, settings survive a reboot, and alerts-off suppresses the
# alert entirely.
#
#   ./simctl.py start   # if not already running
#   ./prayer-fire-test.sh     (takes ~4 minutes)
set -eu
cd "$(dirname "$0")"

PASS=0
FAIL=0
check() {
  if [ "$1" = "1" ]; then PASS=$((PASS+1)); echo "ok:   $2"; else FAIL=$((FAIL+1)); echo "FAIL: $2"; fi
}

# Bottom-center of the alert screen is the full-width green OK button.
is_alert() {
  python3 - "$1" <<'PYEOF'
import sys
from PIL import Image
img = Image.open(sys.argv[1]).convert('RGB')
w, h = img.size
r, g, b = img.getpixel((w // 2, int(h * 0.88)))
sys.exit(0 if g > 120 and g > r + 40 and g > b + 40 else 1)
PYEOF
}

sim_alive() {
  python3 - <<'PYEOF'
import json, os, sys
st = json.load(open('run/state.json'))
sys.exit(0 if os.path.exists(f"/proc/{st['sim_pid']}") else 1)
PYEOF
}

# NYC, ISNA, Standard, UTC-4: lat 40.71 -> 4071, lon -74.01 -> -7401, tz -16 quarters.
BLOB_ALERTS_ON="01010001e70f17e3f0"
BLOB_ALERTS_OFF="01010000e70f17e3f0"

# node helper: write a prayer blob and confirm via read-back; then CTS-warp.
configure() { # configure <blobhex> <hour> <min> <sec>
  node - "$1" "$2" "$3" "$4" <<'EOF'
import net from 'node:net';
const [blobHex, hh, mm, ss] = process.argv.slice(2);
const req = (s, b) => new Promise((res) => { s.once('data', res); s.write(b); });
const frame = (c, op, p = Buffer.alloc(0)) => { const h = Buffer.alloc(4); h[0]=c; h[1]=op; h.writeUInt16LE(p.length,2); return Buffer.concat([h,p]); };
const s = net.connect(18632, '127.0.0.1');
await new Promise((r) => s.on('connect', r));
const blob = Buffer.from(blobHex, 'hex');
let r = await req(s, frame(6, 0, blob));
if (r[0] !== 0) { console.error('settings write failed'); process.exit(1); }
let ok = false;
for (let i = 0; i < 5 && !ok; i++) {
  await new Promise((r2) => setTimeout(r2, 200));
  r = await req(s, frame(6, 1));
  ok = r[0] === 0 && r.subarray(3).equals(blob);
}
if (!ok) { console.error('settings read-back mismatch'); process.exit(1); }
// CTS: 2026-07-14 hh:mm:ss (Tuesday = 2)
const cts = Buffer.alloc(10);
cts.writeUInt16LE(2026, 0); cts[2] = 7; cts[3] = 14;
cts[4] = Number(hh); cts[5] = Number(mm); cts[6] = Number(ss); cts[7] = 2;
r = await req(s, frame(2, 0, cts));
if (r[0] !== 0) { console.error('CTS failed'); process.exit(1); }
console.log('configured; watch time set');
process.exit(0);
EOF
}

./simctl.py restart >/dev/null
sleep 2

# --- 1. Alert fires from sleep ---------------------------------------------
configure "$BLOB_ALERTS_ON" 13 00 30   # Dhuhr at 13:02
echo "waiting 40 s for idle sleep..."
sleep 40
./simctl.py shot prayer-asleep >/dev/null
if is_alert shots/prayer-asleep.png; then A=1; else A=0; fi
check $((1 - A)) "sim asleep before Dhuhr"

echo "waiting 55 s more for the alert to fire from sleep..."
sleep 55
sim_alive && ALIVE=1 || ALIVE=0
check "$ALIVE" "sim alive after fire (no flash-asleep tripwire abort)"
./simctl.py shot prayer-alert >/dev/null
if is_alert shots/prayer-alert.png; then R=1; else R=0; fi
check $R "prayer alert on screen (Dhuhr + OK button)"

./simctl.py tap 120 210 >/dev/null
sleep 1
./simctl.py shot prayer-dismissed >/dev/null
if is_alert shots/prayer-dismissed.png; then D=1; else D=0; fi
check $((1 - D)) "OK dismisses back to the watchface"

# --- 2. Settings survive a reboot -------------------------------------------
./simctl.py restart >/dev/null
sleep 2
node - "$BLOB_ALERTS_ON" <<'EOF' && PERSIST=1 || PERSIST=0
import net from 'node:net';
const blob = Buffer.from(process.argv[2], 'hex');
// Accumulate: the reply is a TCP stream, so comparing the blob against whatever
// the first chunk happened to hold fails whenever it arrives split -- and reads
// as "the settings did not survive the reboot".
let buf = Buffer.alloc(0);
const s = net.connect(18632, '127.0.0.1', () => s.write(Buffer.from([6, 1, 0, 0])));
s.on('data', (d) => {
  buf = Buffer.concat([buf, d]);
  if (buf.length < 3 + blob.length) return;
  process.exit(buf[0] === 0 && buf.subarray(3, 3 + blob.length).equals(blob) ? 0 : 1);
});
setTimeout(() => process.exit(1), 5000);
EOF
check "$PERSIST" "settings survive a reboot byte-exact"

# --- 3. Alerts off: no alert fires ------------------------------------------
configure "$BLOB_ALERTS_OFF" 13 00 30
echo "waiting 95 s past Dhuhr with alerts off..."
sleep 95
sim_alive && ALIVE=1 || ALIVE=0
check "$ALIVE" "sim alive (alerts-off path)"
./simctl.py shot prayer-suppressed >/dev/null
if is_alert shots/prayer-suppressed.png; then S=1; else S=0; fi
check $((1 - S)) "no alert with alerts disabled"

echo
echo "screenshots: shots/prayer-asleep.png shots/prayer-alert.png shots/prayer-dismissed.png shots/prayer-suppressed.png"
if [ $FAIL -eq 0 ]; then echo "PRAYER-FIRE TEST PASS ($PASS checks)"; else echo "PRAYER-FIRE TEST FAIL ($FAIL failures)"; exit 1; fi
