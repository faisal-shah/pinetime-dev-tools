from pathlib import Path

import orjson
import pytest

from ptlab.sim import (
    SimError,
    SimLayout,
    Simulator,
    build_sim_state,
    existing_state_is_healthy,
    legacy_main,
    wait_for_tcp_listener,
)
from ptlab.workspace import Workspace


def test_sim_state_preserves_legacy_pid_fields(tmp_path: Path) -> None:
    layout = SimLayout.modern(tmp_path)

    state = build_sim_state(
        display=":101",
        bridge_port=43123,
        window="7",
        zoom=1,
        sim_pid=123,
        xvfb_pid=456,
        layout=layout,
    )

    assert state["sim_pid"] == state["sim"]["pid"] == 123
    assert state["xvfb_pid"] == state["xvfb"]["pid"] == 456
    assert state["sim"]["pgid"] == 123
    assert state["xvfb"]["pgid"] == 456
    assert state["gatt_port"] == 43123
    assert state["ble_control_port"] is None


def test_headless_sim_state_has_no_xvfb_process(tmp_path: Path) -> None:
    layout = SimLayout.modern(tmp_path)
    state = build_sim_state(
        display=None,
        bridge_port=43123,
        ble_control_port=43124,
        window=None,
        zoom=1,
        sim_pid=123,
        xvfb_pid=None,
        layout=layout,
        headless=True,
    )

    assert state["headless"]
    assert state["display"] is None
    assert "xvfb" not in state
    assert state["ble_control_port"] == 43124


def test_wait_for_tcp_listener_retries_until_process_and_port_are_ready() -> None:
    clock = [0.0]
    attempts = []

    def probe(host: str, port: int, timeout: float) -> bool:
        attempts.append((host, port, timeout))
        return len(attempts) == 2

    wait_for_tcp_listener(
        "127.0.0.1",
        43210,
        process_alive=lambda: True,
        timeout=1.0,
        probe=probe,
        monotonic=lambda: clock[0],
        sleep=lambda duration: clock.__setitem__(0, clock[0] + duration),
    )

    assert len(attempts) == 2
    assert attempts[0][:2] == ("127.0.0.1", 43210)


def test_wait_for_tcp_listener_fails_immediately_when_process_exits() -> None:
    probes = []

    with pytest.raises(SimError, match="exited before bridge port"):
        wait_for_tcp_listener(
            "127.0.0.1",
            43210,
            process_alive=lambda: False,
            probe=lambda *args: probes.append(args) or True,
            monotonic=lambda: 0.0,
        )

    assert probes == []


def test_existing_state_requires_both_processes_and_responsive_display() -> None:
    state = {
        "display": ":109",
        "sim_pid": 101,
        "xvfb_pid": 102,
        "sim": {"pid": 101, "pgid": 101},
        "xvfb": {"pid": 102, "pgid": 102},
    }

    assert existing_state_is_healthy(
        state,
        alive={101, 102}.__contains__,
        display_responsive=lambda display: display == 109,
    )
    assert not existing_state_is_healthy(
        state,
        alive={101}.__contains__,
        display_responsive=lambda _: True,
    )
    assert not existing_state_is_healthy(
        state,
        alive={101, 102}.__contains__,
        display_responsive=lambda _: False,
    )


def test_start_cleans_unhealthy_existing_state_before_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    layout = SimLayout.modern(workspace.devtools)
    layout.state.parent.mkdir(parents=True)
    layout.state.write_bytes(
        orjson.dumps(
            {
                "display": ":99",
                "sim_pid": 101,
                "xvfb_pid": 102,
                "sim": {"pid": 101, "pgid": 101},
                "xvfb": {"pid": 102, "pgid": 102},
            }
        )
    )
    simulator = Simulator(workspace, layout)
    stop_calls = []

    monkeypatch.setattr("ptlab.sim.existing_state_is_healthy", lambda _: False)

    def stop(*, quiet: bool = False) -> bool:
        stop_calls.append(quiet)
        layout.state.unlink()
        return True

    monkeypatch.setattr(simulator, "stop", stop)

    with pytest.raises(SimError, match="not built"):
        simulator.start()

    assert stop_calls == [True]


def test_start_returns_healthy_existing_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    layout = SimLayout.modern(workspace.devtools)
    layout.state.parent.mkdir(parents=True)
    expected = {
        "display": ":99",
        "sim_pid": 101,
        "xvfb_pid": 102,
        "sim": {"pid": 101, "pgid": 101},
        "xvfb": {"pid": 102, "pgid": 102},
    }
    layout.state.write_bytes(orjson.dumps(expected))
    simulator = Simulator(workspace, layout)

    monkeypatch.setattr("ptlab.sim.existing_state_is_healthy", lambda _: True)

    assert simulator.start() == expected


def test_legacy_main_reports_runtime_dependency_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    monkeypatch.setattr("ptlab.sim.discover_workspace", lambda _: workspace)
    monkeypatch.setattr(
        "ptlab.sim.execute_legacy",
        lambda *_: (_ for _ in ()).throw(FileNotFoundError("Xvfb")),
    )

    assert legacy_main(["start"]) == 1
    assert capsys.readouterr().err == "error: Xvfb\n"
