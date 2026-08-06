#!/bin/bash
# Behavioural check for the "Vibrate all but Fajr" alert mode.
#
# Same NYC/ISNA golden config as prayer-fire-test.sh, where Fajr on 2026-07-14
# is 04:02. Warps the watch to just before Fajr twice: once with alerts set to
# "all but Fajr" (flags 0x03) expecting silence, once with "all prayers"
# (0x01) expecting the alert. The second round is the control -- without it a
# broken clock or a bad blob would make round one "pass" for the wrong reason.
#
#   ./simctl.py start   # if not already running
#   ./prayer-fajr-test.sh     (takes ~4 minutes)
set -eu
cd "$(dirname "$0")"

PASS=0
FAIL=0
check() {
  if [ "$1" = "1" ]; then PASS=$((PASS + 1)); echo "ok:   $2"; else FAIL=$((FAIL + 1)); echo "FAIL: $2"; fi
}

# Bottom-centre of the alert screen is the full-width green OK button.
is_alert() { # is_alert <shot.png>
  python3 - "$1" <<'PYEOF'
import sys
from PIL import Image
img = Image.open(sys.argv[1]).convert('RGB')
w, h = img.size
r, g, b = img.getpixel((w // 2, int(h * 0.88)))
sys.exit(0 if g > 120 and g > r + 40 and g > b + 40 else 1)
PYEOF
}

# NYC, ISNA, Standard, UTC-4. Only the flags byte differs between the two.
BLOB_EXCEPT_FAJR="02010003e70f17e3f0"
BLOB_ALL="02010001e70f17e3f0"

configure() { # configure <blobhex> <hour> <min> <sec>
  node - "$1" "$2" "$3" "$4" <<'EOF'
import net from 'node:net';
const [blobHex, hh, mm, ss] = process.argv.slice(2);
const req = (s, b) => new Promise((res) => { s.once('data', res); s.write(b); });
const frame = (c, op, p = Buffer.alloc(0)) => {
  const h = Buffer.alloc(4);
  h[0] = c; h[1] = op; h.writeUInt16LE(p.length, 2);
  return Buffer.concat([h, p]);
};
const s = net.connect(18632, '127.0.0.1');
await new Promise((r) => s.on('connect', r));

const blob = Buffer.from(blobHex, 'hex');
let r = await req(s, frame(6, 0, blob));
if (r[0] !== 0) { console.error('settings write rejected'); process.exit(1); }

// The watch commits on its SystemTask, so confirm by read-back before warping.
let ok = false;
for (let i = 0; i < 5 && !ok; i++) {
  await new Promise((r2) => setTimeout(r2, 200));
  const back = await req(s, frame(6, 1));
  ok = Buffer.from(back.subarray(3)).equals(blob);
}
if (!ok) { console.error('settings read-back never matched'); process.exit(1); }

// CTS: 2026-07-14 hh:mm:ss (Tuesday = 2)
const cts = Buffer.alloc(10);
cts.writeUInt16LE(2026, 0); cts[2] = 7; cts[3] = 14;
cts[4] = Number(hh); cts[5] = Number(mm); cts[6] = Number(ss); cts[7] = 2;
r = await req(s, frame(2, 0, cts));
if (r[0] !== 0) { console.error('CTS write failed'); process.exit(1); }
s.end();
EOF
}

./simctl.py restart >/dev/null
sleep 2

# --- 1. "All but Fajr": Fajr must pass in silence ---------------------------
configure "$BLOB_EXCEPT_FAJR" 04 00 30 # Fajr at 04:02
echo "waiting 40 s for idle sleep..."
sleep 40
echo "waiting 60 s past Fajr with alerts set to all-but-Fajr..."
sleep 60
./simctl.py shot fajr-skipped >/dev/null
if is_alert shots/fajr-skipped.png; then A=1; else A=0; fi
check $((1 - A)) "no alert at Fajr when set to all-but-Fajr"

# --- 2. Control: "all prayers" must still alert at Fajr ---------------------
configure "$BLOB_ALL" 04 00 30
echo "waiting 40 s for idle sleep..."
sleep 40
echo "waiting 60 s past Fajr with alerts set to all prayers..."
sleep 60
./simctl.py shot fajr-fired >/dev/null
if is_alert shots/fajr-fired.png; then B=1; else B=0; fi
check $B "alert fires at Fajr when set to all prayers"

./simctl.py tap 120 210 >/dev/null
sleep 1

echo
echo "screenshots: shots/fajr-skipped.png shots/fajr-fired.png"
if [ "$FAIL" -eq 0 ]; then
  echo "PRAYER-FAJR TEST PASS ($PASS checks)"
else
  echo "PRAYER-FAJR TEST FAIL ($FAIL failures)"
  exit 1
fi
