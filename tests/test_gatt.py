import socket
import struct
from pathlib import Path

import orjson
import pytest

from ptlab.gatt import (
    BRIDGE_CHAR,
    GattClient,
    GattFrameParser,
    GattNotification,
    GattProtocolError,
    GattResponse,
    GattTimeout,
    resolve_characteristic,
)
from ptlab.transcript import TranscriptWriter


class FakeSocket:
    def __init__(self, chunks=(), *, timeout=False):
        self.chunks = list(chunks)
        self.timeout = timeout
        self.sent = []
        self.closed = False

    def settimeout(self, _timeout):
        pass

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, _size):
        if self.timeout:
            raise socket.timeout
        return self.chunks.pop(0) if self.chunks else b""

    def shutdown(self, _how):
        pass

    def close(self):
        self.closed = True


def test_gatt_parser_reassembles_fragmented_response_and_notification() -> None:
    parser = GattFrameParser()
    response = b"\x00\x02\x00ok"
    notification = b"\xf0\x04\x01\x00\x64"

    assert parser.feed(response[:2]) == []
    assert parser.feed(response[2:] + notification[:3]) == [GattResponse(0, b"ok")]
    assert parser.feed(notification[3:]) == [GattNotification(4, b"\x64")]


def test_gatt_parser_rejects_oversized_or_truncated_frames() -> None:
    parser = GattFrameParser(maximum_payload=4)
    with pytest.raises(GattProtocolError, match="exceeds"):
        parser.feed(b"\x00\x05\x00")

    fake = FakeSocket([b"\x00\x02\x00x"])
    client = GattClient("127.0.0.1", 1234, socket_factory=lambda *_args, **_kwargs: fake)
    with pytest.raises(GattProtocolError, match="fragmented"):
        client.read("battery")


def test_gatt_client_uses_generated_ids_and_records_transcript(tmp_path: Path) -> None:
    fake = FakeSocket([b"\x00\x01", b"\x00\x55"])
    transcript = TranscriptWriter(tmp_path / "transcript.ndjson")
    client = GattClient(
        "127.0.0.1",
        1234,
        transcript=transcript,
        socket_factory=lambda *_args, **_kwargs: fake,
    )

    response = client.read("battery")

    assert response == GattResponse(0, b"\x55")
    assert fake.sent == [bytes((BRIDGE_CHAR["battery"], 1, 0, 0))]
    records = [orjson.loads(line) for line in transcript.path.read_bytes().splitlines()]
    assert [record["kind"] for record in records] == ["request", "response"]
    assert records[0]["characteristic_id"] == BRIDGE_CHAR["battery"]


def test_gatt_client_validates_boundaries_and_timeouts() -> None:
    fake = FakeSocket(timeout=True)
    client = GattClient("127.0.0.1", 1234, socket_factory=lambda *_args, **_kwargs: fake)
    with pytest.raises(GattTimeout):
        client.read("battery")
    with pytest.raises(ValueError, match="operation"):
        client.request("battery", 9)
    with pytest.raises(ValueError, match="cannot include"):
        client.request("battery", 1, b"x")
    with pytest.raises(ValueError, match="exceeds"):
        client.write("battery", b"x" * 509)
    with pytest.raises(ValueError, match="generated"):
        resolve_characteristic(255)


def test_generated_bridge_ids_are_unique() -> None:
    ids = [value for value in BRIDGE_CHAR.values() if value is not None]
    assert len(ids) == len(set(ids))
