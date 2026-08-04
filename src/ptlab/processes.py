from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ProcessRef:
    pid: int
    pgid: int
    name: str


def process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_process_groups(
    processes: Iterable[ProcessRef],
    *,
    timeout: float = 3.0,
    alive: Callable[[int], bool] = process_is_alive,
    send_group_signal: Callable[[int, signal.Signals], None] = os.killpg,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    refs = [ref for ref in processes if ref.pid > 1 and ref.pgid > 1]
    own_group = os.getpgrp()
    if any(ref.pgid == own_group for ref in refs):
        raise RuntimeError("refusing to terminate the ptlab process group")

    groups = {ref.pgid for ref in refs if alive(ref.pid)}
    for pgid in groups:
        try:
            send_group_signal(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    deadline = monotonic() + timeout
    while groups and monotonic() < deadline:
        groups = {ref.pgid for ref in refs if ref.pgid in groups and alive(ref.pid)}
        if groups:
            sleep(min(0.05, max(0.0, deadline - monotonic())))

    for pgid in groups:
        try:
            send_group_signal(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def terminate_pids(
    pids: Iterable[int],
    *,
    timeout: float = 3.0,
    alive: Callable[[int], bool] = process_is_alive,
    send_signal: Callable[[int, signal.Signals], None] = os.kill,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    remaining = {pid for pid in pids if pid > 1 and alive(pid)}
    for pid in remaining:
        try:
            send_signal(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    deadline = monotonic() + timeout
    while remaining and monotonic() < deadline:
        remaining = {pid for pid in remaining if alive(pid)}
        if remaining:
            sleep(min(0.05, max(0.0, deadline - monotonic())))

    for pid in remaining:
        try:
            send_signal(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
