from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import date

from ptlab.gatt import GattClient


SCHEDULE_RECORD_VERSION = 2
SCHEDULE_RECORD_SIZE = 43
TASK_RECORD_VERSION = 1
TASK_RECORD_SIZE = 31


@dataclass(frozen=True)
class Digest:
    protocol: int
    capacity: int
    count: int
    version: int
    streak: int | None = None


@dataclass(frozen=True)
class CompanionStatus:
    protocol: int
    capacity: int
    bonded_count: int
    eviction_policy: int
    reset_epoch: int
    eviction_count: int
    cccd_overflow_rejections: int
    invariant_violations: int
    flags: int


def encode_schedule_record(
    *,
    identifier: int,
    title: str,
    hour: int = 7,
    minute: int = 30,
    rule_kind: int = 1,
    anchor: date = date(2026, 7, 14),
    parameter: int = 1,
    enabled: bool = True,
    last_modified: int = 1_789_000_000,
    end: date | None = None,
) -> bytes:
    if not 0 <= identifier <= 0xFFFF:
        raise ValueError("schedule identifier must be a u16")
    if not 0 <= rule_kind <= 3:
        raise ValueError("schedule rule kind must be between 0 and 3")
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("schedule time is invalid")
    if not 0 <= parameter <= 0xFF:
        raise ValueError("schedule parameter must be a u8")
    title_bytes = title.encode("utf-8")
    if len(title_bytes) > 23:
        raise ValueError("schedule title exceeds 23 UTF-8 bytes")
    record = bytearray(SCHEDULE_RECORD_SIZE)
    struct.pack_into("<HBBB HBBBB", record, 0, identifier, rule_kind, hour, minute, anchor.year, anchor.month, anchor.day, parameter, int(enabled))
    record[11 : 11 + len(title_bytes)] = title_bytes
    struct.pack_into("<I", record, 35, last_modified)
    if end is not None:
        struct.pack_into("<HBB", record, 39, end.year, end.month, end.day)
    return bytes(record)


def encode_task_record(
    *,
    identifier: int,
    order: int,
    title: str,
    last_modified: int = 1_789_000_000,
) -> bytes:
    if not 0 <= identifier <= 0xFFFF:
        raise ValueError("task identifier must be a u16")
    if not 0 <= order <= 0xFF:
        raise ValueError("task order must be a u8")
    title_bytes = title.encode("utf-8")
    if len(title_bytes) > 23:
        raise ValueError("task title exceeds 23 UTF-8 bytes")
    field = title_bytes + b"\0" * (24 - len(title_bytes))
    record = struct.pack("<HB24sI", identifier, order, field, last_modified)
    if len(record) != TASK_RECORD_SIZE:
        raise AssertionError("task encoder layout drifted")
    return record


def begin_sync(count: int, version: int) -> bytes:
    if not 0 <= count <= 0xFF:
        raise ValueError("sync count must be a u8")
    return struct.pack("<BBBI", 0, 0, count, version)


def record_message(index: int, record: bytes, *, record_version: int) -> bytes:
    if not 0 <= index <= 0xFF:
        raise ValueError("record index must be a u8")
    return bytes((1, record_version, index)) + record


def commit_sync(count: int) -> bytes:
    if not 0 <= count <= 0xFF:
        raise ValueError("sync count must be a u8")
    return bytes((2, 0, count))


def abort_sync() -> bytes:
    return b"\x03\x00"


def parse_digest(payload: bytes) -> Digest:
    if len(payload) not in (7, 9):
        raise ValueError(f"digest must be 7 or 9 bytes, got {len(payload)}")
    protocol, capacity, count, version = struct.unpack_from("<BBBI", payload)
    streak = struct.unpack_from("<H", payload, 7)[0] if len(payload) == 9 else None
    return Digest(protocol, capacity, count, version, streak)


def parse_companion_status(payload: bytes) -> CompanionStatus:
    if len(payload) != 20:
        raise ValueError(f"companion status must be 20 bytes, got {len(payload)}")
    values = struct.unpack("<BBBBIIHHI", payload)
    return CompanionStatus(*values)


def push_records(
    client: GattClient,
    *,
    sync_characteristic: str,
    records: list[bytes],
    version: int,
    record_version: int,
) -> None:
    response = client.write(sync_characteristic, begin_sync(len(records), version))
    if response.status != 0:
        raise RuntimeError(f"{sync_characteristic} begin failed with ATT status 0x{response.status:02x}")
    for index, record in enumerate(records):
        response = client.write(
            sync_characteristic,
            record_message(index, record, record_version=record_version),
        )
        if response.status != 0:
            raise RuntimeError(
                f"{sync_characteristic} record {index} failed with ATT status 0x{response.status:02x}"
            )
    response = client.write(sync_characteristic, commit_sync(len(records)))
    if response.status != 0:
        raise RuntimeError(f"{sync_characteristic} commit failed with ATT status 0x{response.status:02x}")


def pull_records(
    client: GattClient,
    *,
    digest_characteristic: str,
    read_characteristic: str,
) -> tuple[Digest, list[bytes]]:
    digest_response = client.read(digest_characteristic)
    if digest_response.status != 0:
        raise RuntimeError(
            f"{digest_characteristic} read failed with ATT status 0x{digest_response.status:02x}"
        )
    digest = parse_digest(digest_response.payload)
    records: list[bytes] = []
    for index in range(digest.count):
        selected = client.write(read_characteristic, bytes((index,)))
        if selected.status != 0:
            raise RuntimeError(
                f"{read_characteristic} select {index} failed with ATT status 0x{selected.status:02x}"
            )
        record = client.read(read_characteristic)
        if record.status != 0:
            raise RuntimeError(
                f"{read_characteristic} read {index} failed with ATT status 0x{record.status:02x}"
            )
        records.append(record.payload)
    return digest, records
