from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from typing import Any

from ptlab.workspace import Workspace


PROTOCOL_TARGETS = ("infinitime", "infinisim", "companion", "devtools")


def protocol_check_command(
    workspace: Workspace,
    *,
    python_executable: str = sys.executable,
) -> list[str]:
    generator = workspace.infinitime / "tools" / "generate_companion_protocol.py"
    return [
        python_executable,
        str(generator),
        "--workspace-root",
        str(workspace.root),
        "--targets",
        ",".join(PROTOCOL_TARGETS),
        "--check",
    ]


def check_protocol(
    workspace: Workspace,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    command: Sequence[str] = protocol_check_command(workspace)
    result = runner(command, cwd=workspace.infinitime, check=False)
    return int(result.returncode)
