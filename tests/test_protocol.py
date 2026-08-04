from pathlib import Path
from types import SimpleNamespace

from ptlab.protocol import PROTOCOL_TARGETS, check_protocol, protocol_check_command
from ptlab.workspace import Workspace


def test_protocol_check_constructs_all_sibling_targets(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)

    command = protocol_check_command(workspace, python_executable="python-under-test")

    assert command == [
        "python-under-test",
        str(tmp_path / "InfiniTime" / "tools" / "generate_companion_protocol.py"),
        "--workspace-root",
        str(tmp_path),
        "--targets",
        ",".join(PROTOCOL_TARGETS),
        "--check",
    ]


def test_protocol_check_runs_from_infinitime(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=7)

    assert check_protocol(workspace, runner=runner) == 7
    assert calls[0][1] == {"cwd": workspace.infinitime, "check": False}
