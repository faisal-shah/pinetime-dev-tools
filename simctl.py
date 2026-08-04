#!/usr/bin/env python3
"""Backward-compatible InfiniSim control wrapper."""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
try:
    import orjson  # noqa: F401
except ModuleNotFoundError:
    os.execvp(
        "uv",
        ["uv", "run", "--project", str(ROOT), "python", str(__file__), *sys.argv[1:]],
    )

sys.path.insert(0, str(ROOT / "src"))

from ptlab.sim import legacy_main


if __name__ == "__main__":
    raise SystemExit(legacy_main())
