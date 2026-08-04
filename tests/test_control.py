import socket

import pytest

from ptlab.control import (
    BleControlClient,
    ControlError,
    ControlProtocolError,
    ControlTimeout,
    LineParser,
    coerce_control_values,
    parse_control_line,
)


class FakeSocket:
    def __init__(self, chunks=(), *, timeout=False):
        self.chunks = list(chunks)
        self.timeout = timeout
        self.sent = []

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
        pass


def test_line_parser_handles_fragmentation_crlf_and_multiple_lines() -> None:
    parser = LineParser()
    assert parser.feed(b"OK one\r") == []
    assert parser.feed(b"\nOK two\n") == ["OK one", "OK two"]


def test_line_parser_rejects_length_and_utf8_boundaries() -> None:
    parser = LineParser(maximum_line=4)
    with pytest.raises(ControlProtocolError, match="exceeds"):
        parser.feed(b"12345")
    with pytest.raises(ControlProtocolError, match="UTF-8"):
        LineParser().feed(b"\xff\n")


def test_control_response_parser_and_query_coercion() -> None:
    ok, message, fields = parse_control_line(
        "OK radio_actual=Fast retained=1:010203040506,1:020303040506 boot=restored"
    )
    assert ok and message == ""
    assert coerce_control_values(fields) == {
        "radio_actual": "Fast",
        "retained": ["1:010203040506", "1:020303040506"],
        "boot": "restored",
    }
    with pytest.raises(ControlProtocolError, match="prefix"):
        parse_control_line("MAYBE")


def test_control_client_validates_commands_errors_and_timeouts() -> None:
    fake = FakeSocket([b"ERR rejected\n"])
    client = BleControlClient("127.0.0.1", 1234, socket_factory=lambda *_args, **_kwargs: fake)
    with pytest.raises(ControlError, match="rejected"):
        client.command("CONNECT")
    assert fake.sent == [b"CONNECT\n"]
    with pytest.raises(ValueError, match="trimmed"):
        client.command(" QUERY")

    timed_out = BleControlClient(
        "127.0.0.1",
        1234,
        socket_factory=lambda *_args, **_kwargs: FakeSocket(timeout=True),
    )
    with pytest.raises(ControlTimeout):
        timed_out.query()
