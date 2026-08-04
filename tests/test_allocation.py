import socket

from ptlab.allocation import allocate_display, allocate_tcp_port, xvfb_dynamic_command


def test_tcp_allocator_returns_bindable_non_default_port() -> None:
    port = allocate_tcp_port()

    assert 1 <= port <= 65535
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))


def test_display_allocator_uses_first_inactive_display() -> None:
    active = {99, 100}

    assert allocate_display(range(99, 103), is_active=active.__contains__) == ":101"


def test_xvfb_command_requests_server_side_dynamic_display() -> None:
    command = xvfb_dynamic_command(440)

    assert command[:3] == ["Xvfb", "-displayfd", "1"]
    assert "440x440x24" in command
