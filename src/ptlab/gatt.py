from __future__ import annotations

import socket
import struct
from collections import deque
from dataclasses import dataclass
from typing import Final

from ptlab.generated.companion_protocol import BRIDGE_CHAR
from ptlab.transcript import TranscriptWriter

OP_WRITE: Final = 0
OP_READ: Final = 1
OP_WRITE_WITHOUT_RESPONSE: Final = 2
MAX_REQUEST_PAYLOAD: Final = 508
MAX_RESPONSE_PAYLOAD: Final = 64
BUSY_STATUS: Final = 0xFD

_GENERATED_IDS = frozenset(
    int(value)
    for value in BRIDGE_CHAR.values()
    if value is not None
)
if len(_GENERATED_IDS) != len([value for value in BRIDGE_CHAR.values() if value is not None]):
    raise RuntimeError("generated companion protocol contains duplicate bridge IDs")


class GattError(RuntimeError):
    pass


class GattTimeout(GattError):
    pass


class GattProtocolError(GattError):
    pass


@dataclass(frozen=True)
class GattResponse:
    status: int
    payload: bytes


@dataclass(frozen=True)
class GattNotification:
    characteristic: int
    payload: bytes


class GattFrameParser:
    def __init__(self, *, maximum_payload: int = MAX_RESPONSE_PAYLOAD) -> None:
        if maximum_payload < 0 or maximum_payload > 0xFFFF:
            raise ValueError("maximum payload must be between 0 and 65535")
        self.maximum_payload = maximum_payload
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[GattResponse | GattNotification]:
        self._buffer.extend(data)
        frames: list[GattResponse | GattNotification] = []
        while self._buffer:
            notification = self._buffer[0] == 0xF0
            header_size = 4 if notification else 3
            if len(self._buffer) < header_size:
                break
            if notification:
                characteristic = self._buffer[1]
                payload_size = struct.unpack_from("<H", self._buffer, 2)[0]
            else:
                payload_size = struct.unpack_from("<H", self._buffer, 1)[0]
            if payload_size > self.maximum_payload:
                self._buffer.clear()
                raise GattProtocolError(
                    f"GATT frame payload {payload_size} exceeds {self.maximum_payload}"
                )
            total = header_size + payload_size
            if len(self._buffer) < total:
                break
            payload = bytes(self._buffer[header_size:total])
            if notification:
                frames.append(GattNotification(characteristic, payload))
            else:
                frames.append(GattResponse(self._buffer[0], payload))
            del self._buffer[:total]
        return frames

    @property
    def pending_bytes(self) -> int:
        return len(self._buffer)


def resolve_characteristic(characteristic: str | int) -> tuple[str, int]:
    if isinstance(characteristic, str):
        try:
            value = BRIDGE_CHAR[characteristic]
        except KeyError as error:
            raise ValueError(f"unknown generated characteristic {characteristic!r}") from error
        if value is None:
            raise ValueError(f"characteristic {characteristic!r} has no GATT bridge ID")
        return characteristic, int(value)
    if not isinstance(characteristic, int) or characteristic not in _GENERATED_IDS:
        raise ValueError("characteristic must be a generated bridge name or ID")
    name = next(name for name, value in BRIDGE_CHAR.items() if value == characteristic)
    return name, characteristic


class GattClient:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float = 3.0,
        transcript: TranscriptWriter | None = None,
        socket_factory=socket.create_connection,
    ) -> None:
        if not host:
            raise ValueError("host cannot be empty")
        if not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.host = host
        self.port = port
        self.timeout = timeout
        self.transcript = transcript
        try:
            self.socket = socket_factory((host, port), timeout=timeout)
        except TimeoutError as error:
            raise GattTimeout(f"timed out connecting to GATT bridge {host}:{port}") from error
        self.socket.settimeout(timeout)
        self.parser = GattFrameParser()
        self.responses: deque[GattResponse] = deque()
        self.notifications: deque[GattNotification] = deque()

    def __enter__(self) -> GattClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self.socket is None:
            return
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()
        self.socket = None

    def _record(
        self,
        direction: str,
        kind: str,
        *,
        name: str,
        characteristic: int,
        payload: bytes,
        **fields: object,
    ) -> None:
        if self.transcript is not None:
            self.transcript.emit(
                "gatt",
                direction,
                kind,
                characteristic=name,
                characteristic_id=characteristic,
                payload_hex=payload.hex(),
                payload_length=len(payload),
                **fields,
            )

    def _receive(self) -> None:
        try:
            chunk = self.socket.recv(4096)
        except socket.timeout as error:
            raise GattTimeout(
                f"timed out waiting for GATT bridge {self.host}:{self.port}"
            ) from error
        if not chunk:
            if self.parser.pending_bytes:
                raise GattProtocolError("GATT bridge closed with a fragmented frame")
            raise GattProtocolError("GATT bridge closed before a response")
        for frame in self.parser.feed(chunk):
            if isinstance(frame, GattNotification):
                name, _ = resolve_characteristic(frame.characteristic)
                self.notifications.append(frame)
                self._record(
                    "receive",
                    "notify",
                    name=name,
                    characteristic=frame.characteristic,
                    payload=frame.payload,
                )
            else:
                self.responses.append(frame)

    def request(
        self,
        characteristic: str | int,
        operation: int,
        payload: bytes = b"",
    ) -> GattResponse | None:
        name, characteristic_id = resolve_characteristic(characteristic)
        if operation not in (OP_WRITE, OP_READ, OP_WRITE_WITHOUT_RESPONSE):
            raise ValueError("GATT operation must be write(0), read(1), or write-without-response(2)")
        if not isinstance(payload, bytes):
            raise TypeError("GATT payload must be bytes")
        if len(payload) > MAX_REQUEST_PAYLOAD:
            raise ValueError(f"GATT payload exceeds {MAX_REQUEST_PAYLOAD} bytes")
        if operation == OP_READ and payload:
            raise ValueError("GATT reads cannot include a payload")

        frame = bytes((characteristic_id, operation)) + struct.pack("<H", len(payload)) + payload
        self._record(
            "send",
            "request",
            name=name,
            characteristic=characteristic_id,
            payload=payload,
            operation=operation,
        )
        try:
            self.socket.sendall(frame)
        except socket.timeout as error:
            raise GattTimeout(f"timed out sending GATT request to {self.host}:{self.port}") from error
        if operation == OP_WRITE_WITHOUT_RESPONSE:
            return None
        while not self.responses:
            self._receive()
        response = self.responses.popleft()
        self._record(
            "receive",
            "response",
            name=name,
            characteristic=characteristic_id,
            payload=response.payload,
            status=response.status,
        )
        return response

    def read(self, characteristic: str | int) -> GattResponse:
        response = self.request(characteristic, OP_READ)
        assert response is not None
        return response

    def write(self, characteristic: str | int, payload: bytes) -> GattResponse:
        response = self.request(characteristic, OP_WRITE, payload)
        assert response is not None
        return response

    def write_without_response(self, characteristic: str | int, payload: bytes) -> None:
        self.request(characteristic, OP_WRITE_WITHOUT_RESPONSE, payload)

    def next_notification(self) -> GattNotification:
        while not self.notifications:
            self._receive()
        return self.notifications.popleft()
