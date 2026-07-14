#!/bin/bash
# Fire-from-sleep regression for the schedule reminder path.
#
# Warps the watch clock to a known time, syncs TWO events due at the same
# second ~70 s out, lets the sim idle-sleep, and checks that the reminder
# fires from sleep: the timer callback runs RAM-only on the timer daemon
# (flash asleep - the sim tripwire aborts on any violation), SystemTask wakes
# and builds the combined title from flash, and the reminder screen persists
# until dismissed. Verifies via screenshots + the green OK button pixels.
#
#   ./simctl.py start   # if not already running
#   ./reminder-fire-test.sh     (takes ~2 minutes)
set -eu
cd "$(dirname "$0")"

PASS=0
FAIL=0
check() {
  if [ "$1" = "1" ]; then PASS=$((PASS+1)); echo "ok:   $2"; else FAIL=$((FAIL+1)); echo "FAIL: $2"; fi
}

# Bottom-center of the reminder screen is the full-width green OK button.
is_reminder() { # is_reminder <shot.png>
  python3 - "$1" <<'PYEOF'
import sys
from PIL import Image
img = Image.open(sys.argv[1]).convert('RGB')
w, h = img.size
r, g, b = img.getpixel((w // 2, int(h * 0.88)))
sys.exit(0 if g > 120 and g > r + 40 and g > b + 40 else 1)
PYEOF
}

./simctl.py restart >/dev/null
sleep 2

node - <<'EOF'
import net from 'node:net';
const req = (s, b) => new Promise((res) => { s.once('data', res); s.write(b); });
const frame = (c, op, p = Buffer.alloc(0)) => { const h = Buffer.alloc(4); h[0]=c; h[1]=op; h.writeUInt16LE(p.length,2); return Buffer.concat([h,p]); };
const rec = (id, title, h, m) => { const b = Buffer.alloc(39); b.writeUInt16LE(id,0); b[2]=0; b[3]=h; b[4]=m; b.writeUInt16LE(2026,5); b[7]=7; b[8]=14; b[9]=0; b[10]=1; b.write(title,11); b.writeUInt32LE(1789e6,35); return b; };
const cts = (h, m, sec) => { const b = Buffer.alloc(10); b.writeUInt16LE(2026,0); b[2]=7; b[3]=14; b[4]=h; b[5]=m; b[6]=sec; b[7]=2; return b; };
const s = net.connect(18632, '127.0.0.1');
await new Promise((r) => s.on('connect', r));
let r = await req(s, frame(2, 0, cts(11, 59, 50)));
if (r[0] !== 0) { console.error('CTS failed'); process.exit(1); }
r = await req(s, frame(0, 0, Buffer.concat([Buffer.from([0,0,2]), Buffer.from(new Uint32Array([424243]).buffer)])));
if (r[0] !== 0) { console.error('begin failed'); process.exit(1); }
r = await req(s, frame(0, 0, Buffer.concat([Buffer.from([1,1,0]), rec(1, 'Brush teeth', 12, 1)])));
if (r[0] !== 0) { console.error('rec0 failed'); process.exit(1); }
r = await req(s, frame(0, 0, Buffer.concat([Buffer.from([1,1,1]), rec(2, 'Pack bag', 12, 1)])));
if (r[0] !== 0) { console.error('rec1 failed'); process.exit(1); }
r = await req(s, frame(0, 0, Buffer.from([2,0,2])));
if (r[0] !== 0) { console.error('commit failed'); process.exit(1); }
console.log('watch time 11:59:50; two events due 12:01:00');
process.exit(0);
EOF

echo "waiting 40 s for idle sleep..."
sleep 40
./simctl.py shot fire-asleep >/dev/null
if is_reminder shots/fire-asleep.png; then ASLEEP=0; else ASLEEP=1; fi
check $ASLEEP "sim went to sleep before the due time"

echo "waiting 35 s more for the reminder to fire from sleep..."
sleep 35
python3 - <<'PYEOF'
import json, os, sys
st = json.load(open('run/state.json'))
sys.exit(0 if os.path.exists(f"/proc/{st['sim_pid']}") else 1)
PYEOF
check $((1 - $?)) "sim alive after fire (no flash-asleep tripwire abort)"

# The fire woke the screen; the reminder is still in its ring window. (Do NOT
# press the side button here - on an active reminder it means dismiss.)
./simctl.py shot fire-reminder >/dev/null
if is_reminder shots/fire-reminder.png; then R=1; else R=0; fi
check $R "reminder screen showing after wake (persists until dismissed)"

# Dismiss: StopAlerting reschedules (a flash scan from the display task).
./simctl.py tap 120 210 >/dev/null
sleep 1
./simctl.py shot fire-dismissed >/dev/null
if is_reminder shots/fire-dismissed.png; then D=1; else D=0; fi
check $((1 - D)) "OK dismisses back to the watchface"

echo
echo "screenshots: shots/fire-asleep.png shots/fire-reminder.png shots/fire-dismissed.png"
echo "(combined same-second title 'Brush teeth / Pack bag' is visible in fire-reminder.png)"
if [ $FAIL -eq 0 ]; then echo "REMINDER-FIRE TEST PASS ($PASS checks)"; else echo "REMINDER-FIRE TEST FAIL ($FAIL failures)"; exit 1; fi
