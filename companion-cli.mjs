#!/usr/bin/env node
// A second "phone" for the watch, from the command line — and the computer-
// companion story for individuals: pull the watch's schedule, list it, add or
// delete events, push back. Speaks the same wire protocol as the app through
// the sim's TCP GATT bridge (or, later, any transport exposing the same chars).
//
//   node companion-cli.mjs list
//   node companion-cli.mjs add "Quran practice" 17:00 daily
//   node companion-cli.mjs add "Dentist" 09:15 once 2026-08-01
//   node companion-cli.mjs add "Trash out" 19:00 weekly Sun
//   node companion-cli.mjs delete "Dentist"
//
// Acts like a freshly-installed companion (no base snapshot): it adopts
// whatever is on the watch and applies the requested change on top, exactly
// what a second family phone does on its first sync.

import net from 'node:net';
import { encodeEventRecord, eventMsg } from './schedule-protocol.mjs';
import { BRIDGE_CHAR as CHAR } from './generated/companion-protocol.mjs';

const PORT = Number(process.env.BRIDGE_PORT ?? 18632);
const HOST = process.env.BRIDGE_HOST ?? '127.0.0.1';
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

class Bridge {
  constructor(socket) {
    this.socket = socket;
    this.buf = Buffer.alloc(0);
    this.waiters = [];
    socket.on('data', (d) => {
      this.buf = Buffer.concat([this.buf, d]);
      while (this.waiters.length > 0 && this.buf.length >= 3) {
        const len = this.buf.readUInt16LE(1);
        if (this.buf.length < 3 + len) return;
        const status = this.buf[0];
        const payload = this.buf.subarray(3, 3 + len);
        this.buf = this.buf.subarray(3 + len);
        this.waiters.shift()({ status, payload });
      }
    });
  }
  static connect() {
    return new Promise((resolve, reject) => {
      const s = net.createConnection({ port: PORT, host: HOST }, () => resolve(new Bridge(s)));
      s.on('error', reject);
    });
  }
  request(charId, op, payload = Buffer.alloc(0)) {
    const h = Buffer.alloc(4);
    h[0] = charId;
    h[1] = op;
    h.writeUInt16LE(payload.length, 2);
    this.socket.write(Buffer.concat([h, payload]));
    return new Promise((resolve) => this.waiters.push(resolve));
  }
  write(c, p) { return this.request(c, 0, p); }
  read(c) { return this.request(c, 1); }
  close() { this.socket.end(); }
}

const decodeRecord = (b) => ({
  id: b.readUInt16LE(0),
  ruleKind: b[2],
  hour: b[3],
  minute: b[4],
  year: b.readUInt16LE(5),
  month: b[7],
  day: b[8],
  param: b[9],
  enabled: (b[10] & 1) !== 0,
  title: b.subarray(11, 35).toString('utf8').replace(/\0.*$/s, ''),
  lastModified: b.readUInt32LE(35),
  // Inclusive last day; year 0 means the rule never ends. Decoded so a
  // read-modify-write through this CLI does not silently drop the end date.
  endYear: b.readUInt16LE(39),
  endMonth: b[41],
  endDay: b[42],
  raw: Buffer.from(b),
});

// Wire format lives in schedule-protocol.mjs so the CLI, bridge-test and the
// scenario scripts cannot drift apart.
const encodeRecord = (e) =>
  encodeEventRecord({
    ...e,
    anchor: new Date(e.year, e.month - 1, e.day),
    end: e.endYear ? new Date(e.endYear, e.endMonth - 1, e.endDay) : undefined,
  });

const describeRule = (e) => {
  switch (e.ruleKind) {
    case 0: return `once ${e.year}-${String(e.month).padStart(2, '0')}-${String(e.day).padStart(2, '0')}`;
    case 1: return e.param <= 1 ? 'daily' : `every ${e.param} days`;
    case 2: return DAYS.filter((_, i) => (e.param >> i) & 1).join('/');
    case 3: return `monthly on day ${e.param}`;
    default: return `?rule ${e.ruleKind}`;
  }
};

async function pull(bridge) {
  const d = (await bridge.read(CHAR.scheduleDigest)).payload;
  const count = d[2];
  const version = d.readUInt32LE(3);
  const events = [];
  for (let i = 0; i < count; i++) {
    await bridge.write(CHAR.eventRead, Buffer.from([i]));
    events.push(decodeRecord(Buffer.from((await bridge.read(CHAR.eventRead)).payload)));
  }
  return { events, version, capacity: d[1] };
}

async function push(bridge, events) {
  const version = 1 + Math.floor(Math.random() * 0xfffffffe);
  const begin = Buffer.alloc(7);
  begin[0] = 0; begin[1] = 0; begin[2] = events.length;
  begin.writeUInt32LE(version, 3);
  let r = await bridge.write(CHAR.scheduleSync, begin);
  if (r.status !== 0) throw new Error('BeginSync rejected');
  for (const [i, e] of events.entries()) {
    r = await bridge.write(CHAR.scheduleSync, eventMsg(i, encodeRecord(e)));
    if (r.status !== 0) throw new Error(`record ${i} rejected`);
  }
  r = await bridge.write(CHAR.scheduleSync, Buffer.from([2, 0, events.length]));
  if (r.status !== 0) throw new Error('CommitSync rejected');
  for (let i = 0; i < 10; i++) {
    await new Promise((res) => setTimeout(res, 150));
    const d = (await bridge.read(CHAR.scheduleDigest)).payload;
    if (d.readUInt32LE(3) === version && d[2] === events.length) return version;
  }
  throw new Error('watch did not confirm');
}

const [cmd, ...args] = process.argv.slice(2);
const bridge = await Bridge.connect();
const { events, version, capacity } = await pull(bridge);

if (cmd === 'list' || cmd === undefined) {
  console.log(`watch schedule (version ${version}, ${events.length}/${capacity}):`);
  for (const e of events) {
    console.log(
      `  ${String(e.hour).padStart(2, '0')}:${String(e.minute).padStart(2, '0')}  ${e.title}` +
      `  [${describeRule(e)}${e.enabled ? '' : ', disabled'}]`
    );
  }
} else if (cmd === 'add') {
  const [title, time, kind = 'daily', extra] = args;
  const [hour, minute] = time.split(':').map(Number);
  const now = new Date();
  let ruleKind = 1, param = 1, year = now.getFullYear(), month = now.getMonth() + 1, day = now.getDate();
  if (kind === 'once') {
    ruleKind = 0; param = 0;
    if (extra) [year, month, day] = extra.split('-').map(Number);
  } else if (kind === 'weekly') {
    ruleKind = 2;
    param = (extra ?? 'Mon').split(',').reduce((m, d) => m | (1 << DAYS.findIndex((x) => x.toLowerCase() === d.trim().toLowerCase())), 0);
  } else if (kind === 'monthly') {
    ruleKind = 3; param = Number(extra ?? day);
  } else if (kind !== 'daily') {
    param = Number(kind); // "add T HH:MM 3" = every 3 days
  }
  let id;
  do { id = 1 + Math.floor(Math.random() * 0xfffe); } while (events.some((e) => e.id === id));
  events.push({ id, ruleKind, hour, minute, year, month, day, param, enabled: true,
                title, lastModified: Math.floor(Date.now() / 1000) });
  const v = await push(bridge, events);
  console.log(`added "${title}"; watch now has ${events.length} events (version ${v})`);
} else if (cmd === 'delete') {
  const title = args[0];
  const keep = events.filter((e) => e.title !== title);
  if (keep.length === events.length) {
    console.error(`no event titled "${title}"`);
    process.exit(1);
  }
  const v = await push(bridge, keep);
  console.log(`deleted "${title}"; watch now has ${keep.length} events (version ${v})`);
} else {
  console.error(`unknown command: ${cmd}`);
  process.exit(1);
}
bridge.close();
