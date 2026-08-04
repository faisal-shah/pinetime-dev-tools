from __future__ import annotations

import socket
from collections.abc import Callable
from typing import Any

from ptlab.transcript import TranscriptWriter


MAX_CONTROL_LINE = 1024


class ControlError(RuntimeError):
    pass


class ControlTimeout(ControlError):
    pass


class ControlProtocolError(ControlError):
    pass


class LineParser:
    def __init__(self, *, maximum_line: int = MAX_CONTROL_LINE) -> None:
        if maximum_line < 1:
            raise ValueError("maximum line must be positive")
        self.maximum_line = maximum_line
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[str]:
        self._buffer.extend(data)
        lines: list[str] = []
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                if len(self._buffer) > self.maximum_line:
                    self._buffer.clear()
                    raise ControlProtocolError("BLE-control line exceeds 1024 bytes")
                return lines
            if newline > self.maximum_line:
                self._buffer.clear()
                raise ControlProtocolError("BLE-control line exceeds 1024 bytes")
            raw = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ControlProtocolError("BLE-control response is not UTF-8") from error
            if line:
                lines.append(line)


def parse_control_line(line: str) -> tuple[bool, str, dict[str, str]]:
    if not line or "\n" in line or "\r" in line:
        raise ControlProtocolError("invalid BLE-control response line")
    fields = line.split()
    if fields[0] not in {"OK", "ERR"}:
        raise ControlProtocolError(f"BLE-control response lacks OK/ERR prefix: {line!r}")
    values: dict[str, str] = {}
    message: list[str] = []
    for field in fields[1:]:
        if "=" in field:
            key, value = field.split("=", 1)
            if not key or key in values:
                raise ControlProtocolError(f"invalid BLE-control field {field!r}")
            values[key] = value
        else:
            message.append(field)
    return fields[0] == "OK", " ".join(message), values


def coerce_control_values(values: dict[str, str]) -> dict[str, Any]:
    coerced: dict[str, Any] = {}
    for key, value in values.items():
        if key == "retained":
            coerced[key] = [] if not value else value.split(",")
            continue
        try:
            coerced[key] = int(value, 10)
        except ValueError:
            coerced[key] = value
    return coerced


class BleControlClient:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float = 3.0,
        transcript: TranscriptWriter | None = None,
        socket_factory: Callable[..., socket.socket] = socket.create_connection,
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
            raise ControlTimeout(f"timed out connecting to BLE control {host}:{port}") from error
        self.socket.settimeout(timeout)
        self.parser = LineParser()
        self.lines: list[str] = []

    def __enter__(self) -> BleControlClient:
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

    def _next_line(self) -> str:
        while not self.lines:
            try:
                chunk = self.socket.recv(4096)
            except socket.timeout as error:
                raise ControlTimeout(
                    f"timed out waiting for BLE control {self.host}:{self.port}"
                ) from error
            if not chunk:
                raise ControlProtocolError("BLE-control connection closed before a response")
            self.lines.extend(self.parser.feed(chunk))
        return self.lines.pop(0)

    def command(self, command: str, *, allow_error: bool = False) -> tuple[str, dict[str, Any]]:
        if not command or command.strip() != command or "\n" in command or "\r" in command:
            raise ValueError("BLE-control command must be one non-empty trimmed line")
        encoded = command.encode("utf-8")
        if len(encoded) > MAX_CONTROL_LINE:
            raise ValueError(f"BLE-control command exceeds {MAX_CONTROL_LINE} bytes")
        if self.transcript is not None:
            self.transcript.emit("ble-control", "send", "command", line=command)
        try:
            self.socket.sendall(encoded + b"\n")
        except socket.timeout as error:
            raise ControlTimeout(f"timed out sending BLE-control command {command!r}") from error
        line = self._next_line()
        ok, message, values = parse_control_line(line)
        if self.transcript is not None:
            self.transcript.emit(
                "ble-control",
                "receive",
                "response",
                line=line,
                ok=ok,
                message=message,
                values=values,
            )
        if not ok and not allow_error:
            raise ControlError(f"{command}: {line}")
        return message, coerce_control_values(values)

    def query(self) -> dict[str, Any]:
        _, values = self.command("QUERY")
        return values
