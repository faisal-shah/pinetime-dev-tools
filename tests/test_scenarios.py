import time
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock

import orjson
import pytest

from ptlab.gatt import GattClient
from ptlab.scenario import (
    ScenarioDefinition,
    ScenarioTimeout,
    run_scenario,
    scenario_deadline,
)
from ptlab.scenarios import SCENARIO_BY_NAME, SCENARIOS, select_scenarios
from ptlab.workspace import Workspace


def fake_state() -> dict:
    return {
        "display": None,
        "bridge_port": 41000,
        "gatt_port": 41000,
        "ble_control_port": 41001,
        "headless": True,
        "window": None,
        "zoom": 1,
        "sim_pid": 999,
        "xvfb_pid": None,
        "sim": {"pid": 999, "pgid": 999},
        "paths": {},
    }


def test_scenario_registration_and_filtering() -> None:
    assert len(SCENARIOS) == len(SCENARIO_BY_NAME)
    assert {"bond-lifecycle", "radio-policy", "link-auth", "persistence-faults", "family-handoff"} <= set(SCENARIO_BY_NAME)
    assert [scenario.name for scenario in select_scenarios("all", ["fast"])] == [
        "bond-lifecycle",
        "radio-policy",
        "link-auth",
        "persistence-faults",
        "family-handoff",
    ]
    with pytest.raises(ValueError, match="unknown"):
        select_scenarios("missing")


def test_scenario_deadline_raises() -> None:
    with pytest.raises(ScenarioTimeout):
        with scenario_deadline(0.01):
            time.sleep(0.1)


def test_scenario_deadline_is_not_swallowed_by_socket_timeout_handler() -> None:
    blocking_socket = Mock()
    blocking_socket.recv.side_effect = lambda _size: time.sleep(0.1)
    client = GattClient(
        "127.0.0.1",
        41000,
        socket_factory=lambda *_args, **_kwargs: blocking_socket,
    )

    with pytest.raises(ScenarioTimeout):
        with scenario_deadline(0.01):
            client.read("battery")


def test_mock_scenario_writes_result_events_and_junit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ptlab.scenario.collect_repository_states", lambda _workspace: {})
    monkeypatch.setattr("ptlab.scenario.Simulator.start", lambda *_args, **_kwargs: fake_state())
    stops = []
    monkeypatch.setattr("ptlab.scenario.Simulator.stop", lambda *_args, **_kwargs: stops.append(True) or True)

    def mock(context):
        context.check(True, "mock passes")
        context.check(False, "mock fails", actual=1, expected=2)

    code, paths, result = run_scenario(
        Workspace(tmp_path / "workspace"),
        ScenarioDefinition("mock", "mock scenario", mock),
        runs_root=tmp_path / "runs",
    )

    assert code == 1
    assert result["status"] == "failed"
    assert result["check_count"] == 2
    assert result["failure_count"] == 1
    assert stops == [True]
    stored = orjson.loads(paths.result.read_bytes())
    assert stored["allocations"]["ble_control_port"] == 41001
    events = [orjson.loads(line) for line in paths.events.read_bytes().splitlines()]
    assert len([event for event in events if event["event"] == "assertion"]) == 2
    suite = ET.parse(paths.junit).getroot()
    assert suite.attrib["tests"] == "2"
    assert suite.attrib["failures"] == "1"


def test_mock_scenario_timeout_still_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ptlab.scenario.collect_repository_states", lambda _workspace: {})
    monkeypatch.setattr("ptlab.scenario.Simulator.start", lambda *_args, **_kwargs: fake_state())
    stops = []
    monkeypatch.setattr("ptlab.scenario.Simulator.stop", lambda *_args, **_kwargs: stops.append(True) or True)

    code, _paths, result = run_scenario(
        Workspace(tmp_path / "workspace"),
        ScenarioDefinition("timeout", "timeout scenario", lambda _context: time.sleep(0.1)),
        runs_root=tmp_path / "runs",
        timeout=0.01,
    )

    assert code == 1
    assert result["status"] == "timed_out"
    assert stops == [True]
