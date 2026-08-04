from __future__ import annotations

import os
import socket
import subprocess
from collections.abc import Callable, Iterable


def allocate_tcp_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def display_is_active(display: int) -> bool:
    env = dict(os.environ, DISPLAY=f":{display}")
    try:
        result = subprocess.run(
            ["xdotool", "getdisplaygeometry"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=0.5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def allocate_display(
    candidates: Iterable[int] = range(99, 130),
    *,
    is_active: Callable[[int], bool] = display_is_active,
) -> str:
    for number in candidates:
        if not is_active(number):
            return f":{number}"
    raise RuntimeError("no free X display found")


def xvfb_dynamic_command(canvas: int) -> list[str]:
    return [
        "Xvfb",
        "-displayfd",
        "1",
        "-screen",
        "0",
        f"{canvas}x{canvas}x24",
        "-nolisten",
        "tcp",
    ]
