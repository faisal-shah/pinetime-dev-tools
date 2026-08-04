from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import orjson

from ptlab.artifacts import utc_now


class TranscriptWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._sequence = 0
        self._lock = threading.Lock()

    def emit(self, channel: str, direction: str, kind: str, **fields: Any) -> None:
        with self._lock:
            record = {
                "sequence": self._sequence,
                "timestamp": utc_now(),
                "channel": channel,
                "direction": direction,
                "kind": kind,
                **fields,
            }
            self._sequence += 1
            with self.path.open("ab") as stream:
                stream.write(orjson.dumps(record, option=orjson.OPT_SORT_KEYS))
                stream.write(b"\n")
