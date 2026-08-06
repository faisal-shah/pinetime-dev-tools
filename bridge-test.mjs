#!/usr/bin/env node
// End-to-end protocol regression against a running InfiniSim with
// --gatt-bridge (default port 18632; simctl.py start enables it).
//
//   ./simctl.py start && node bridge-test.mjs
//
// Exercises the exact byte protocols the PineTimeCompanion app uses:
// Schedule sync (doc/ScheduleService.md, incl. golden vectors), digest reads,
// CTS time set, New Alert message, battery read — through the same firmware
// code paths a real BLE central would hit.

import net from 'node:net';
import { BRIDGE_CHAR as CHAR, RECORDS } from './generated/companion-protocol.mjs';

const PORT = Number(process.env.BRIDGE_PORT ?? 18632);
const HOST = process.env.BRIDGE_HOST ?? '127.0.0.1';

const OP = { write: 0, read: 1 };

function mutationToken(payload) {
  let crc = 0xffffffff;
  for (const byte of payload) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit++) {
      crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
    }
  }
  const token = (~crc) >>> 0;
  return token === 0 ? 1 : token;
}

async function waitForFamilyCommit(bridge, operation, token) {
  for (let attempt = 0; attempt < 40; attempt++) {
    const response = await bridge.read(CHAR.familyStateStatus);
    if (response.status === 0 && response.payload.length === 16) {
      const state = response.payload[2];
      const activeOperation = response.payload[3];
      const activeToken = response.payload.readUInt32LE(6);
      if (activeOperation === operation && activeToken === token) {
        if (state === RECORDS.family_state.states.succeeded) {
          return true;
        }
        if (state === RECORDS.family_state.states.failed) {
          return false;
        }
      }
    }
    await sleep(25);
  }
  return false;
}

let failures = 0;
let checks = 0;
function check(ok, what, detail = '') {
  checks++;
  if (!ok) {
    failures++;
    console.error(`FAIL: ${what} ${detail}`);
  } else {
    console.log(`ok:   ${what}`);
  }
}

class Bridge {
  constructor(socket) {
    this.socket = socket;
    this.buf = Buffer.alloc(0);
    this.waiters = [];
    socket.on('data', (d) => {
      this.buf = Buffer.concat([this.buf, d]);
      this.drain();
    });
  }

  static connect() {
    return new Promise((resolve, reject) => {
      const s = net.createConnection({ port: PORT, host: HOST }, () => resolve(new Bridge(s)));
      s.on('error', reject);
    });
  }

  drain() {
    while (this.waiters.length > 0 && this.buf.length >= 3) {
      const len = this.buf.readUInt16LE(1);
      if (this.buf.length < 3 + len) return;
      const status = this.buf[0];
      const payload = this.buf.subarray(3, 3 + len);
      this.buf = this.buf.subarray(3 + len);
      this.waiters.shift()({ status, payload });
    }
  }

  request(charId, op, payload = Buffer.alloc(0)) {
    const header = Buffer.alloc(4);
    header[0] = charId;
    header[1] = op;
    header.writeUInt16LE(payload.length, 2);
    this.socket.write(Buffer.concat([header, payload]));
    return new Promise((resolve) => this.waiters.push(resolve));
  }

  write(charId, payload) { return this.request(charId, OP.write, payload); }
  read(charId) { return this.request(charId, OP.read); }
  close() { this.socket.end(); }
}

// ---- protocol encoders ----
// Shared with companion-cli.mjs and the scenario scripts so the record
// layout has exactly one definition; see schedule-protocol.mjs.
import { encodeEventRecord, beginSync, eventMsg, commitSync, abortSync } from './schedule-protocol.mjs';

async function readAllEvents(bridge, count) {
  const out = [];
  for (let i = 0; i < count; i++) {
    let r = await bridge.write(CHAR.eventRead, Buffer.from([i]));
    r = await bridge.read(CHAR.eventRead);
    out.push(Buffer.from(r.payload));
  }
  return out;
}

const decodeDigest = (p) => ({ proto: p[0], capacity: p[1], count: p[2], version: p.readUInt32LE(3) });

const encodeCts = (d) => {
  const b = Buffer.alloc(10);
  b.writeUInt16LE(d.getFullYear(), 0);
  b[2] = d.getMonth() + 1;
  b[3] = d.getDate();
  b[4] = d.getHours();
  b[5] = d.getMinutes();
  b[6] = d.getSeconds();
  b[7] = ((d.getDay() + 6) % 7) + 1; // 1=Mon..7=Sun
  b[8] = 0; // fractions256
  b[9] = 0; // adjust reason
  return b;
};

// New Alert (ANS 0x2A46) as Gadgetbridge sends notifications:
// category CustomHuami (0xFA), 1 alert, icon 0xFF, then "title\0body".
const encodeNewAlert = (title, body) =>
  Buffer.concat([Buffer.from([0xfa, 0x01, 0xff]), Buffer.from(`${title}\0${body}`, 'utf8')]).subarray(0, 100);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---- the tests ----

const bridge = await Bridge.connect();
console.log(`connected to GATT bridge at ${HOST}:${PORT}`);

// 1. Battery read
{
  const r = await bridge.read(CHAR.battery);
  check(r.status === 0 && r.payload.length === 1 && r.payload[0] <= 100, 'battery read', `-> ${r.payload[0]}%`);
}

// 2. Golden-vector cross-check: doc/ScheduleService.md EventRecord index 0
{
  const rec = encodeEventRecord({
    id: 1, ruleKind: 2, hour: 17, minute: 0,
    anchor: new Date(2026, 6, 13), param: 0x2a, enabled: true, title: 'Quran practice',
    lastModified: 1784000000,
  });
  const golden = Buffer.from(
    '01000211' + '00' + 'ea07070d' + '2a01' +
    '517572616e207072616374696365' + '00'.repeat(10) + '00ae556a' +
    '00000000', 'hex'); // end date: year 0 = never ends
  check(rec.equals(golden), 'encoder matches golden vector', rec.toString('hex'));
}

// 3. Full sync transaction + digest verification
const VERSION = 424242;
{
  const now = new Date();
  const events = [
    { id: 1, ruleKind: 1, hour: 6, minute: 30, anchor: now, param: 1, enabled: true, title: 'Fajr practice' },
    { id: 2, ruleKind: 2, hour: 17, minute: 0, anchor: now, param: 0x2a, enabled: true, title: 'Quran practice' },
    { id: 3, ruleKind: 3, hour: 12, minute: 0, anchor: now, param: 31, enabled: true, title: 'Month end' },
  ];
  let r = await bridge.write(CHAR.scheduleSync, beginSync(events.length, VERSION));
  check(r.status === 0, 'BeginSync accepted');
  for (const [i, e] of events.entries()) {
    r = await bridge.write(CHAR.scheduleSync, eventMsg(i, encodeEventRecord(e)));
    check(r.status === 0, `EventRecord[${i}] accepted`);
  }
  r = await bridge.write(CHAR.scheduleSync, commitSync(events.length));
  check(r.status === 0, 'CommitSync accepted');
  await sleep(300); // commit is processed on the SystemTask
  r = await bridge.read(CHAR.scheduleDigest);
  const d = decodeDigest(r.payload);
  check(d.count === 3 && d.version === VERSION, 'digest reflects committed sync', JSON.stringify(d));
  check(d.proto === 3 && d.capacity === 32, 'digest proto/capacity', JSON.stringify(d));

  // A record announcing the wrong version must be refused, not misread. This is
  // what makes an app/firmware version gap fail safely instead of writing
  // garbage into the schedule, and it pins the two constants together across
  // the repos: ScheduleService::eventRecordVersion and SCHEDULE_RECORD_VERSION.
  {
    const rec = encodeEventRecord({
      id: 99, ruleKind: 1, hour: 7, minute: 0, anchor: new Date(), param: 1,
      enabled: true, title: 'Wrong version',
    });
    const stale = Buffer.concat([Buffer.from([1, 1, 0]), rec]); // v1 record message
    const r = await bridge.write(CHAR.scheduleSync, stale);
    check(r.status !== 0, 'a v1 record message is rejected by v2 firmware', `status ${r.status}`);
    await bridge.write(CHAR.scheduleSync, Buffer.from([0x03, 0x00])); // abort, leave clean
  }
}

// 4. Protocol violations are rejected and leave state intact
{
  let r = await bridge.write(CHAR.scheduleSync, eventMsg(0, encodeEventRecord({
    id: 9, ruleKind: 0, hour: 1, minute: 0, anchor: new Date(), param: 0, enabled: true, title: 'Orphan',
  })));
  check(r.status !== 0, 'EventRecord without BeginSync rejected');

  r = await bridge.write(CHAR.scheduleSync, beginSync(33, 1)); // over capacity
  check(r.status !== 0, 'BeginSync over capacity rejected');

  r = await bridge.write(CHAR.scheduleSync, beginSync(2, 777));
  check(r.status === 0, 'BeginSync(2) accepted');
  r = await bridge.write(CHAR.scheduleSync, commitSync(2)); // incomplete
  check(r.status !== 0, 'incomplete CommitSync rejected');

  await bridge.write(CHAR.scheduleSync, abortSync());
  await sleep(200);
  r = await bridge.read(CHAR.scheduleDigest);
  const d = decodeDigest(r.payload);
  check(d.count === 3 && d.version === VERSION, 'active schedule untouched by bad syncs', JSON.stringify(d));
}

// 5. Duplicate index rejected
{
  let r = await bridge.write(CHAR.scheduleSync, beginSync(2, 778));
  const rec = encodeEventRecord({ id: 5, ruleKind: 1, hour: 8, minute: 0, anchor: new Date(), param: 1, enabled: true, title: 'Dup' });
  r = await bridge.write(CHAR.scheduleSync, eventMsg(0, rec));
  check(r.status === 0, 'first record accepted');
  r = await bridge.write(CHAR.scheduleSync, eventMsg(0, rec));
  check(r.status !== 0, 'duplicate index rejected');
  await bridge.write(CHAR.scheduleSync, abortSync());
}

// 6. Event read-back: pull the schedule and verify byte-exact round trip
{
  let r = await bridge.read(CHAR.scheduleDigest);
  const d = decodeDigest(r.payload);
  const pulled = await readAllEvents(bridge, d.count);
  check(pulled.length === 3, 'pulled all records', `${pulled.length}`);
  const expected = encodeEventRecord({
    id: 2, ruleKind: 2, hour: 17, minute: 0,
    anchor: new Date(), param: 0x2a, enabled: true, title: 'Quran practice',
  });
  const quran = pulled.find((p) => p.readUInt16LE(0) === 2);
  check(!!quran && quran.subarray(2, 5).equals(expected.subarray(2, 5)) &&
        quran.subarray(9, 35).equals(expected.subarray(9, 35)),
        'read-back record matches what was written');

  r = await bridge.write(CHAR.eventRead, Buffer.from([9]));
  check(r.status !== 0, 'out-of-range read select rejected');
}

// 7. Two-client pull/merge/push mechanics: a fresh client B adopts A's schedule,
//    adds its own event, then A sees B's addition after a pull.
{
  let r = await bridge.read(CHAR.scheduleDigest);
  let d = decodeDigest(r.payload);
  const theirs = await readAllEvents(bridge, d.count); // client B pulls
  const bNew = encodeEventRecord({
    id: 0x7777, ruleKind: 1, hour: 7, minute: 45,
    anchor: new Date(), param: 1, enabled: true, title: 'Added by phone B',
    lastModified: Math.floor(Date.now() / 1000),
  });
  const merged = [...theirs, bNew];
  const B_VERSION = 990001;
  await bridge.write(CHAR.scheduleSync, beginSync(merged.length, B_VERSION));
  for (const [i, rec] of merged.entries()) {
    await bridge.write(CHAR.scheduleSync, eventMsg(i, rec));
  }
  r = await bridge.write(CHAR.scheduleSync, commitSync(merged.length));
  check(r.status === 0, 'client B merged push accepted');
  await sleep(300);
  r = await bridge.read(CHAR.scheduleDigest);
  d = decodeDigest(r.payload);
  check(d.count === 4 && d.version === B_VERSION, "client B's merge is live", JSON.stringify(d));
  const afterB = await readAllEvents(bridge, d.count); // client A pulls
  check(afterB.some((p) => p.readUInt16LE(0) === 0x7777), "client A sees B's event after pull");
  check(afterB.filter((p) => p.readUInt16LE(0) !== 0x7777).length === 3, "A's original events survived B's sync");
}

// 8. Full-capacity sync, staged OUT OF ORDER: 32 records written index 31..0
//    (littlefs zero-fills the seek-past-EOF gaps; the receive bitmask ensures
//    completeness before commit). Spot-read records across the file after.
const mkSlotEvent = (i) => ({
  id: 1000 + i, ruleKind: 1, hour: i % 24, minute: (i * 5) % 60,
  anchor: new Date(2026, 6, 14), param: 1 + (i % 9), enabled: true, title: `Slot ${i}`,
  lastModified: 1789000000 + i,
});
{
  const FULL_VERSION = 323232;
  let r = await bridge.write(CHAR.scheduleSync, beginSync(32, FULL_VERSION));
  check(r.status === 0, 'BeginSync(32) accepted');
  let allOk = true;
  for (let i = 31; i >= 0; i--) {
    r = await bridge.write(CHAR.scheduleSync, eventMsg(i, encodeEventRecord(mkSlotEvent(i))));
    if (r.status !== 0) {
      allOk = false;
      break;
    }
  }
  check(allOk, 'all 32 records staged in reverse order');
  r = await bridge.write(CHAR.scheduleSync, commitSync(32));
  check(r.status === 0, 'CommitSync(32) accepted');
  await sleep(300);
  r = await bridge.read(CHAR.scheduleDigest);
  const d = decodeDigest(r.payload);
  check(d.count === 32 && d.version === FULL_VERSION, 'digest shows 32 events', JSON.stringify(d));
  for (const idx of [0, 20, 31]) {
    await bridge.write(CHAR.eventRead, Buffer.from([idx]));
    r = await bridge.read(CHAR.eventRead);
    const want = encodeEventRecord(mkSlotEvent(idx));
    check(r.status === 0 && r.payload.equals(want), `record[${idx}] round-trips byte-exact`);
  }
}

// 9. Stale read index: a select survives only while the schedule stays at
//    least that big; the firmware re-validates at read time.
{
  let r = await bridge.write(CHAR.eventRead, Buffer.from([31]));
  check(r.status === 0, 'select record 31 while 32 events live');
  const SHRUNK_VERSION = 655001;
  await bridge.write(CHAR.scheduleSync, beginSync(2, SHRUNK_VERSION));
  for (let i = 0; i < 2; i++) {
    await bridge.write(CHAR.scheduleSync, eventMsg(i, encodeEventRecord({
      id: 50 + i, ruleKind: 1, hour: 8, minute: 0, anchor: new Date(), param: 1, enabled: true, title: `Keep ${i}`,
    })));
  }
  r = await bridge.write(CHAR.scheduleSync, commitSync(2));
  check(r.status === 0, 'shrinking sync accepted');
  await sleep(300);
  r = await bridge.read(CHAR.eventRead);
  check(r.status !== 0, 'stale read index rejected after shrink');
  await bridge.write(CHAR.eventRead, Buffer.from([1]));
  r = await bridge.read(CHAR.eventRead);
  check(r.status === 0 && r.payload.readUInt16LE(0) === 51, 're-selected valid index reads fine');
}

// 10. CTS time set: jump the watch clock to a distinctive time (time travel!)
{
  const target = new Date(2026, 11, 25, 10, 8, 0); // Dec 25 2026 10:08
  const r = await bridge.write(CHAR.currentTime, encodeCts(target));
  check(r.status === 0, 'CTS time write accepted');
}

// 11. New Alert -> notification appears on the watch
{
  const r = await bridge.write(CHAR.newAlert, encodeNewAlert('PineTimeCompanion', 'Bridge says salaam!'));
  check(r.status === 0, 'New Alert accepted');
}

// 12. Disconnect mid-sync: dropping the link discards the staged transaction
//     (the bridge treats a superseding connection as a BLE disconnect). The
//     schedule is untouched and a fresh sync on the new link succeeds.
{
  const before = await bridge.read(CHAR.scheduleDigest).then((r) => decodeDigest(r.payload));
  await bridge.write(CHAR.scheduleSync, beginSync(3, 987654));
  await bridge.write(CHAR.scheduleSync, eventMsg(0, encodeEventRecord(mkSlotEvent(0))));
  bridge.socket.destroy(); // vanish mid-transaction, no Abort

  await sleep(100); // single-client bridge must observe incumbent EOF before B connects
  const bridge2 = await Bridge.connect();
  await sleep(300); // let the bridge notice and run OnDisconnect
  let r = await bridge2.read(CHAR.scheduleDigest);
  let d = decodeDigest(r.payload);
  check(d.count === before.count && d.version === before.version,
        'schedule untouched after disconnect mid-sync', JSON.stringify(d));

  const AFTER_VERSION = 987655;
  await bridge2.write(CHAR.scheduleSync, beginSync(1, AFTER_VERSION));
  await bridge2.write(CHAR.scheduleSync, eventMsg(0, encodeEventRecord(mkSlotEvent(7))));
  r = await bridge2.write(CHAR.scheduleSync, commitSync(1));
  check(r.status === 0, 'fresh sync succeeds after abandoned transaction');
  await sleep(300);
  d = decodeDigest((await bridge2.read(CHAR.scheduleDigest)).payload);
  check(d.count === 1 && d.version === AFTER_VERSION, 'post-disconnect sync is live', JSON.stringify(d));
  bridge2.close();
}


// 13. Prayer settings: write the doc/PrayerService.md golden vector, confirm
//     the asynchronous commit via read-back; invalid blobs are rejected and
//     leave the stored settings untouched.
{
  const golden = Buffer.from('020101015c10c5ddec', 'hex'); // Chicago, ISNA, Hanafi, alerts on, UTC-5
  await sleep(100);
  const bridge3 = await Bridge.connect();
  let r = await bridge3.write(CHAR.prayerSettings, golden);
  check(r.status === 0, 'prayer settings write accepted');
  check(
    await waitForFamilyCommit(
      bridge3,
      RECORDS.family_state.operations.prayer_settings,
      mutationToken(golden),
    ),
    'prayer settings became durable',
  );
  let echoed = null;
  for (let attempt = 0; attempt < 5; attempt++) {
    await sleep(200);
    r = await bridge3.read(CHAR.prayerSettings);
    if (r.status === 0 && r.payload.equals(golden)) {
      echoed = r.payload;
      break;
    }
  }
  check(echoed !== null, 'prayer settings read back byte-exact', r.payload?.toString('hex'));

  r = await bridge3.write(CHAR.prayerSettings, golden.subarray(0, 8));
  check(r.status !== 0, 'short prayer blob rejected');
  const badVersion = Buffer.from(golden);
  badVersion[0] = 1;
  r = await bridge3.write(CHAR.prayerSettings, badVersion);
  check(r.status !== 0, 'wrong prayer version rejected');
  const badLat = Buffer.from(golden);
  badLat.writeInt16LE(9001, 4);
  r = await bridge3.write(CHAR.prayerSettings, badLat);
  check(r.status !== 0, 'out-of-range latitude rejected');
  // flags: bit0 = alerts on, bit1 = skip Fajr. 0x03 is the valid "all but
  // Fajr" mode, so the reserved-bit case has to reach past it.
  const badFlags = Buffer.from(golden);
  badFlags[3] = 0x04; // reserved bit set
  r = await bridge3.write(CHAR.prayerSettings, badFlags);
  check(r.status !== 0, 'reserved flag bits rejected');
  const skipWithoutEnable = Buffer.from(golden);
  skipWithoutEnable[3] = 0x02; // skip-Fajr without the enable bit is meaningless
  r = await bridge3.write(CHAR.prayerSettings, skipWithoutEnable);
  check(r.status !== 0, 'skip-Fajr without the enable bit rejected');
  const allButFajr = Buffer.from(golden);
  allButFajr[3] = 0x03;
  r = await bridge3.write(CHAR.prayerSettings, allButFajr);
  check(r.status === 0, 'all-but-Fajr flags accepted');
  check(
    await waitForFamilyCommit(
      bridge3,
      RECORDS.family_state.operations.prayer_settings,
      mutationToken(allButFajr),
    ),
    'all-but-Fajr settings became durable',
  );
  // ...and put the golden settings back so the untouched-check below is about
  // the rejected writes, not this one.
  r = await bridge3.write(CHAR.prayerSettings, golden);
  check(r.status === 0, 'golden prayer settings restored');
  check(
    await waitForFamilyCommit(
      bridge3,
      RECORDS.family_state.operations.prayer_settings,
      mutationToken(golden),
    ),
    'restored prayer settings became durable',
  );

  await sleep(300);
  r = await bridge3.read(CHAR.prayerSettings);
  check(r.status === 0 && r.payload.equals(golden), 'rejected writes left settings untouched');
  bridge3.close();
}


// 14. Beacon (Find My) provisioning: write a 28-byte advertisement key, confirm
//     via the read-back status byte, and exercise validation. The watch stores
//     the key (no crypto) for beacon mode.
{
  await sleep(100);
  const bridge4 = await Bridge.connect();
  // hasKey should be 0 before provisioning (fresh flash) OR 1 if a prior run
  // left a key; write a fresh key and assert it reads back as present.
  const advKey = Buffer.from(Array.from({ length: 28 }, (_, i) => i + 1));
  let r = await bridge4.write(CHAR.beaconKey, advKey);
  check(r.status === 0, 'beacon key write accepted');
  await sleep(300); // commit runs on the SystemTask
  r = await bridge4.read(CHAR.beaconKey);
  check(r.status === 0 && r.payload.length === 1 && r.payload[0] === 1, 'beacon key read-back reports hasKey=1', r.payload?.toString('hex'));

  r = await bridge4.write(CHAR.beaconKey, advKey.subarray(0, 20));
  check(r.status !== 0, 'short beacon key rejected');
  r = await bridge4.write(CHAR.beaconKey, Buffer.concat([advKey, Buffer.from([0])]));
  check(r.status !== 0, 'oversized beacon key rejected');

  // Validate before enabling: the real virtual radio policy terminates the
  // connected link when beacon mode is accepted.
  r = await bridge4.write(CHAR.beaconControl, Buffer.from([0x02]));
  check(r.status !== 0, 'unknown beacon control command rejected');
  r = await bridge4.write(CHAR.beaconControl, Buffer.from([0x01]));
  check(r.status === 0, 'beacon enable command accepted with a key present');
  bridge4.close();
}

console.log(`\n${checks} checks, ${failures} failures`);
process.exit(failures === 0 ? 1 * (failures > 0) : 0);
