// The schedule sync wire format, in one place.
//
// This used to be copy-pasted into bridge-test.mjs, companion-cli.mjs,
// powerloss-test.sh and reminder-fire-test.sh. When the Event record grew from
// 39 to 43 bytes for end dates, only bridge-test.mjs was updated, so the other
// three kept building short records that the firmware rejected -- and they
// failed with their own vague messages ("seed rec failed", "rec0 failed")
// rather than anything pointing at the record layout. Import from here instead
// of writing a fifth copy.
//
// Mirrors src/components/schedule/ScheduleRules.h (Event, 43 bytes) and
// ScheduleController.h (ProtocolVersion). Keep in step with both.

import { RECORDS } from './generated/companion-protocol.mjs';

/** ScheduleController::ProtocolVersion — the per-record version byte. */
export const RECORD_VERSION = RECORDS.schedule.record_version;

/** sizeof(ScheduleRules::Event); there is a static_assert on it in the firmware. */
export const RECORD_BYTES = RECORDS.schedule.record_size;

/**
 * One event record.
 *
 * `anchor` is a Date. `end` is an optional Date, inclusive; leaving it out
 * writes year 0, which is the firmware's "never ends" encoding.
 */
export function encodeEventRecord({
  id,
  ruleKind, // 0 once, 1 everyNdays, 2 weekly, 3 monthly
  hour,
  minute,
  anchor,
  param,
  enabled,
  title,
  lastModified = 0,
  end,
}) {
  const b = Buffer.alloc(RECORD_BYTES);
  b.writeUInt16LE(id, 0);
  b[2] = ruleKind;
  b[3] = hour;
  b[4] = minute;
  b.writeUInt16LE(anchor.getFullYear(), 5);
  b[7] = anchor.getMonth() + 1;
  b[8] = anchor.getDate();
  b[9] = param;
  b[10] = enabled ? 1 : 0;
  Buffer.from(title, 'utf8').subarray(0, 23).copy(b, 11);
  b.writeUInt32LE(lastModified, 35);
  if (end) {
    b.writeUInt16LE(end.getFullYear(), 39);
    b[41] = end.getMonth() + 1;
    b[42] = end.getDate();
  }
  return b;
}

export const beginSync = (count, version) => {
  const b = Buffer.alloc(7);
  b[0] = 0;
  b[1] = 0;
  b[2] = count;
  b.writeUInt32LE(version, 3);
  return b;
};

export const eventMsg = (index, record) => Buffer.concat([Buffer.from([1, RECORD_VERSION, index]), record]);

export const commitSync = (count) => Buffer.from([2, 0, count]);

export const abortSync = () => Buffer.from([3, 0]);
