from pathlib import Path

import orjson

from ptlab.artifacts import RESULT_SCHEMA_VERSION, create_run_skeleton
from ptlab.workspace import Workspace


def test_run_skeleton_writes_isolated_structured_artifacts(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    repositories = {
        "InfiniTime": {"path": "InfiniTime", "sha": "a" * 40, "dirty": False},
        "InfiniSim": {"path": "InfiniSim", "sha": "b" * 40, "dirty": True},
    }
    ports = iter((43210, 43211))

    paths = create_run_skeleton(
        workspace,
        runs_root=tmp_path / "runs",
        port_allocator=lambda: next(ports),
        display_allocator=lambda: ":117",
        repository_collector=lambda _: repositories,
    )

    result = orjson.loads(paths.result.read_bytes())
    events = [orjson.loads(line) for line in paths.events.read_bytes().splitlines()]
    assert result["schema_version"] == RESULT_SCHEMA_VERSION
    assert result["status"] == "created"
    assert result["finished_at"] is None
    assert result["allocations"] == {
        "bridge_port": 43210,
        "gatt_port": 43210,
        "ble_control_port": 43211,
        "display": ":117",
    }
    assert result["repositories"] == repositories
    assert result["artifacts"]["stdout"] == str(paths.stdout)
    assert result["artifacts"]["junit"] == str(paths.junit)
    assert result["artifacts"]["transcript"] == str(paths.transcript)
    assert {event["event"] for event in events} == {"run.created", "run.ready"}
    assert paths.run.parent == paths.flash.parent == paths.shots.parent
    assert paths.run != paths.flash != paths.shots
