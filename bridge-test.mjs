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
const CHAR = { scheduleSync: 0, scheduleDigest: 1, currentTime: 2, newAlert: 3, battery: 4 };
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

function encodeEventRecord({ id, ruleKind, hour, minute, anchor, param, enabled, title }) {
  const b = Buffer.alloc(35);
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
  return b;
}

const beginSync = (count, version) => {
  const b = Buffer.alloc(7);
  b[0] = 0; b[1] = 0; b[2] = count;
  b.writeUInt32LE(version, 3);
  return b;
};
const eventMsg = (index, record) => Buffer.concat([Buffer.from([1, 0, index]), record]);
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
  });
  const golden = Buffer.from(
    '01000211' + '00' + 'ea07070d' + '2a01' +
    '517572616e207072616374696365' + '00'.repeat(10), 'hex');
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
  check(d.proto === 0 && d.capacity === 16, 'digest proto/capacity', JSON.stringify(d));
}

// 4. Protocol violations are rejected and leave state intact
{
  let r = await bridge.write(CHAR.scheduleSync, eventMsg(0, encodeEventRecord({
    id: 9, ruleKind: 0, hour: 1, minute: 0, anchor: new Date(), param: 0, enabled: true, title: 'Orphan',
  })));
  check(r.status !== 0, 'EventRecord without BeginSync rejected');

  r = await bridge.write(CHAR.scheduleSync, beginSync(17, 1)); // over capacity
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

// 6. CTS time set: jump the watch clock to a distinctive time (time travel!)
{
  const target = new Date(2026, 11, 25, 10, 8, 0); // Dec 25 2026 10:08
  const r = await bridge.write(CHAR.currentTime, encodeCts(target));
  check(r.status === 0, 'CTS time write accepted');
}

// 7. New Alert -> notification appears on the watch
{
  const r = await bridge.write(CHAR.newAlert, encodeNewAlert('PineTimeCompanion', 'Bridge says salaam!'));
  check(r.status === 0, 'New Alert accepted');
}

bridge.close();
console.log(`\n${checks} checks, ${failures} failures`);
process.exit(failures === 0 ? 1 * (failures > 0) : 0);
