#!/bin/bash
# Power-loss regression for the flash-resident schedule storage.
#
# Seeds a known schedule, stages half of a 64-record sync WITHOUT committing,
# then SIGKILLs the simulator (simctl.py kill) while the companion connection
# is still open - the moral equivalent of the battery dying mid-sync.
# Verifies with littlefs-do that the active schedule file survived untouched
# and the partial staging file is discarded on the next boot.
#
#   ./simctl.py start   # if not already running
#   ./powerloss-test.sh
set -eu
cd "$(dirname "$0")"

# littlefs-do reads spiNorFlash.raw from its cwd; a cp destination that does
# not start with '/' copies out of the image into that host directory.
lfsdo() { (cd run && ../../InfiniSim/build/littlefs-do "$@"); }
PASS=0
FAIL=0
check() { # check <ok> <label>
  if [ "$1" = "1" ]; then PASS=$((PASS+1)); echo "ok:   $2"; else FAIL=$((FAIL+1)); echo "FAIL: $2"; fi
}

./simctl.py restart >/dev/null
sleep 2

# 1. Seed a known schedule (3 events, version 31337), then stage 32 of 64
#    records without committing and HOLD the connection open (a disconnect
#    would let the firmware clean up the transaction - that is a different,
#    already-tested path; a power cut gives it no such chance).
STAGER_LOG=$(mktemp)
node - >"$STAGER_LOG" 2>&1 <<'EOF' &
import net from 'node:net';
import { encodeEventRecord, eventMsg } from './schedule-protocol.mjs';
const req = (s, b) => new Promise((res) => { s.once('data', res); s.write(b); });
const frame = (c, op, p = Buffer.alloc(0)) => { const h = Buffer.alloc(4); h[0]=c; h[1]=op; h.writeUInt16LE(p.length,2); return Buffer.concat([h,p]); };
const rec = (id, title) => encodeEventRecord({ id, ruleKind: 1, hour: 7, minute: 30, anchor: new Date(2026, 6, 14), param: 1, enabled: true, title, lastModified: 1789e6 });
const s = net.connect(18632, '127.0.0.1');
await new Promise((r) => s.on('connect', r));
s.on('error', () => {}); // the sim dying under us is the point

// seed: 3 events, version 31337
let r = await req(s, frame(0, 0, Buffer.concat([Buffer.from([0,0,3]), Buffer.from(new Uint32Array([31337]).buffer)])));
if (r[0] !== 0) { console.error('seed begin failed'); process.exit(1); }
for (let i = 0; i < 3; i++) {
  r = await req(s, frame(0, 0, eventMsg(i, rec(i+1, 'Seed ' + i))));
  if (r[0] !== 0) { console.error('seed rec failed'); process.exit(1); }
}
r = await req(s, frame(0, 0, Buffer.from([2,0,3])));
if (r[0] !== 0) { console.error('seed commit failed'); process.exit(1); }
await new Promise((r2) => setTimeout(r2, 400)); // let SystemTask commit

r = await req(s, frame(1, 1));
const d = r.subarray(3);
if (d[2] !== 3 || d.readUInt32LE(3) !== 31337) { console.error('seed digest wrong'); process.exit(1); }

// stage 32 of 64 records, never commit
r = await req(s, frame(0, 0, Buffer.concat([Buffer.from([0,0,64]), Buffer.from(new Uint32Array([777777]).buffer)])));
if (r[0] !== 0) { console.error('stage begin failed'); process.exit(1); }
for (let i = 0; i < 32; i++) {
  r = await req(s, frame(0, 0, eventMsg(i, rec(100+i, 'Doomed ' + i))));
  if (r[0] !== 0) { console.error('stage rec failed', i); process.exit(1); }
}
console.log('STAGED');
setTimeout(() => process.exit(0), 60000); // hold the socket until killed
EOF
STAGER=$!

for _ in $(seq 1 100); do
  grep -q "STAGED" "$STAGER_LOG" 2>/dev/null && break
  kill -0 $STAGER 2>/dev/null || { echo "stager died:"; cat "$STAGER_LOG"; exit 1; }
  sleep 0.2
done
grep -q "STAGED" "$STAGER_LOG" || { echo "stager never finished:"; cat "$STAGER_LOG"; exit 1; }
echo "seeded 3 events (version 31337); 32/64 records staged, connection held open"

# 2. Power loss, mid-transaction.
./simctl.py kill >/dev/null
kill $STAGER 2>/dev/null || true
rm -f "$STAGER_LOG"
sleep 0.5

# 3. Post-mortem on the raw flash image.
LS=$(lfsdo ls /.system 2>/dev/null || true)
echo "$LS" | grep -q "schedule.dat" && DAT=1 || DAT=0
echo "$LS" | grep -q "schedule.stg" && STG=1 || STG=0
check "$DAT" "schedule.dat survived the power cut"
check "$STG" "partial schedule.stg present after the power cut"

# header of schedule.dat: format version, count=3, scheduleVersion=31337 (0x7a69).
# The leading byte is ScheduleController::scheduleFormatVersion -- bump both
# together, since a stale expectation here fails as "header corrupted" and
# reads like the power cut damaged the file.
rm -f run/schedule.dat
lfsdo cp /.system/schedule.dat . >/dev/null 2>&1 || true
HDR=$(xxd -p -l 6 run/schedule.dat 2>/dev/null || echo missing)
[ "$HDR" = "0203697a0000" ] && OK=1 || OK=0
check "$OK" "schedule.dat header intact (v2, 3 events, version 31337) [$HDR]"
rm -f run/schedule.dat

# 4. Reboot: the watch must load the old schedule and clean up the leftover.
./simctl.py start >/dev/null
sleep 2
DIGEST_OK=0
node - <<'EOF' && DIGEST_OK=1 || true
import net from 'node:net';
// Accumulate: the reply is a TCP stream, not a datagram. Parsing whatever the
// first chunk happened to contain threw ERR_BUFFER_OUT_OF_BOUNDS whenever it
// arrived split, killing this script and failing the test for a reason that had
// nothing to do with the filesystem.
const HEADER = 3;
const DIGEST = 7; // proto, capacity, count, version(u32)
let buf = Buffer.alloc(0);
const s = net.connect(18632, '127.0.0.1', () => s.write(Buffer.from([1, 1, 0, 0])));
s.on('data', (d) => {
  buf = Buffer.concat([buf, d]);
  if (buf.length < HEADER + DIGEST) return;
  const p = buf.subarray(HEADER);
  const ok = p[2] === 3 && p.readUInt32LE(3) === 31337;
  console.log('post-reboot digest: count=' + p[2] + ', version=' + p.readUInt32LE(3));
  process.exit(ok ? 0 : 1);
});
setTimeout(() => { console.log('digest read timeout'); process.exit(1); }, 5000);
EOF
check $DIGEST_OK "watch rebooted into the pre-cut schedule"

./simctl.py stop >/dev/null
sleep 0.5
LS2=$(lfsdo ls /.system 2>/dev/null || true)
echo "$LS2" | grep -q "schedule.stg" && LEFT=1 || LEFT=0
check $((1 - LEFT)) "leftover schedule.stg deleted at boot"
./simctl.py start >/dev/null

echo
if [ $FAIL -eq 0 ]; then echo "POWER-LOSS TEST PASS ($PASS checks)"; else echo "POWER-LOSS TEST FAIL ($FAIL failures)"; exit 1; fi
