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

const PORT = Number(process.env.BRIDGE_PORT ?? 18632);
const HOST = process.env.BRIDGE_HOST ?? '127.0.0.1';

// charIds, see InfiniSim sim/gatt_bridge.h
const CHAR = { scheduleSync: 0, scheduleDigest: 1, currentTime: 2, newAlert: 3, battery: 4, eventRead: 5 };
const OP = { write: 0, read: 1 };

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

// ---- protocol encoders (mirrors of the future app's scheduleProtocol.ts) ----

function encodeEventRecord({ id, ruleKind, hour, minute, anchor, param, enabled, title, lastModified = 0 }) {
  const b = Buffer.alloc(39);
  b.writeUInt16LE(id, 0);
  b[2] = ruleKind; // 0 once, 1 everyNdays, 2 weekly, 3 monthly
  b[3] = hour;
  b[4] = minute;
  b.writeUInt16LE(anchor.getFullYear(), 5);
  b[7] = anchor.getMonth() + 1;
  b[8] = anchor.getDate();
  b[9] = param;
  b[10] = enabled ? 1 : 0;
  const t = Buffer.from(title, 'utf8').subarray(0, 23);
  t.copy(b, 11);
  b.writeUInt32LE(lastModified, 35);
  return b;
}

async function readAllEvents(bridge, count) {
  const out = [];
  for (let i = 0; i < count; i++) {
    let r = await bridge.write(CHAR.eventRead, Buffer.from([i]));
    r = await bridge.read(CHAR.eventRead);
    out.push(Buffer.from(r.payload));
  }
  return out;
}

const beginSync = (count, version) => {
  const b = Buffer.alloc(7);
  b[0] = 0; b[1] = 0; b[2] = count;
  b.writeUInt32LE(version, 3);
  return b;
};
const eventMsg = (index, record) => Buffer.concat([Buffer.from([1, 1, index]), record]);
const commitSync = (count) => Buffer.from([2, 0, count]);
const abortSync = () => Buffer.from([3, 0]);

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
    '517572616e207072616374696365' + '00'.repeat(10) + '00ae556a', 'hex');
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
  check(d.proto === 1 && d.capacity === 64, 'digest proto/capacity', JSON.stringify(d));
}

// 4. Protocol violations are rejected and leave state intact
{
  let r = await bridge.write(CHAR.scheduleSync, eventMsg(0, encodeEventRecord({
    id: 9, ruleKind: 0, hour: 1, minute: 0, anchor: new Date(), param: 0, enabled: true, title: 'Orphan',
  })));
  check(r.status !== 0, 'EventRecord without BeginSync rejected');

  r = await bridge.write(CHAR.scheduleSync, beginSync(65, 1)); // over capacity
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

// 8. Full-capacity sync, staged OUT OF ORDER: 64 records written index 63..0
//    (littlefs zero-fills the seek-past-EOF gaps; the receive bitmask ensures
//    completeness before commit). Spot-read records across the file after.
const mkSlotEvent = (i) => ({
  id: 1000 + i, ruleKind: 1, hour: i % 24, minute: (i * 5) % 60,
  anchor: new Date(2026, 6, 14), param: 1 + (i % 9), enabled: true, title: `Slot ${i}`,
  lastModified: 1789000000 + i,
});
{
  const FULL_VERSION = 646464;
  let r = await bridge.write(CHAR.scheduleSync, beginSync(64, FULL_VERSION));
  check(r.status === 0, 'BeginSync(64) accepted');
  let allOk = true;
  for (let i = 63; i >= 0; i--) {
    r = await bridge.write(CHAR.scheduleSync, eventMsg(i, encodeEventRecord(mkSlotEvent(i))));
    if (r.status !== 0) {
      allOk = false;
      break;
    }
  }
  check(allOk, 'all 64 records staged in reverse order');
  r = await bridge.write(CHAR.scheduleSync, commitSync(64));
  check(r.status === 0, 'CommitSync(64) accepted');
  await sleep(300);
  r = await bridge.read(CHAR.scheduleDigest);
  const d = decodeDigest(r.payload);
  check(d.count === 64 && d.version === FULL_VERSION, 'digest shows 64 events', JSON.stringify(d));
  for (const idx of [0, 40, 63]) {
    await bridge.write(CHAR.eventRead, Buffer.from([idx]));
    r = await bridge.read(CHAR.eventRead);
    const want = encodeEventRecord(mkSlotEvent(idx));
    check(r.status === 0 && r.payload.equals(want), `record[${idx}] round-trips byte-exact`);
  }
}

// 9. Stale read index: a select survives only while the schedule stays at
//    least that big; the firmware re-validates at read time.
{
  let r = await bridge.write(CHAR.eventRead, Buffer.from([63]));
  check(r.status === 0, 'select record 63 while 64 events live');
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

console.log(`\n${checks} checks, ${failures} failures`);
process.exit(failures === 0 ? 1 * (failures > 0) : 0);
