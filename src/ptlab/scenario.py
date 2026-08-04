from __future__ import annotations

import shutil
import signal
import subprocess
import time
import traceback
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ptlab.artifacts import (
    EventWriter,
    JsonResultWriter,
    RunPaths,
    collect_repository_states,
    create_run_paths,
    utc_now,
    write_junit,
)
from ptlab.control import BleControlClient
from ptlab.gatt import GattClient
from ptlab.sim import SimLayout, Simulator
from ptlab.transcript import TranscriptWriter
from ptlab.workspace import Workspace


class ScenarioTimeout(RuntimeError):
    pass


class ScenarioSkip(RuntimeError):
    pass


@dataclass(frozen=True)
class ScenarioDefinition:
    name: str
    description: str
    function: Callable[[ScenarioContext], None]
    tags: frozenset[str] = frozenset({"fast"})
    timeout: float = 60.0
    headless: bool = True


@dataclass
class ScenarioContext:
    definition: ScenarioDefinition
    workspace: Workspace
    paths: RunPaths
    simulator: Simulator
    sim_state: dict[str, Any]
    events: EventWriter
    transcript: TranscriptWriter
    assertions: list[dict[str, Any]] = field(default_factory=list)
    control: BleControlClient | None = None

    @property
    def gatt_port(self) -> int:
        return int(self.sim_state["gatt_port"])

    @property
    def control_port(self) -> int:
        return int(self.sim_state["ble_control_port"])

    def open_control(self) -> BleControlClient:
        if self.control is None:
            self.control = BleControlClient(
                "127.0.0.1",
                self.control_port,
                transcript=self.transcript,
            )
        return self.control

    def reconnect_control(self) -> BleControlClient:
        if self.control is not None:
            self.control.close()
        self.control = None
        return self.open_control()

    def gatt(self, *, timeout: float = 3.0) -> GattClient:
        return GattClient(
            "127.0.0.1",
            self.gatt_port,
            timeout=timeout,
            transcript=self.transcript,
        )

    def check(
        self,
        condition: bool,
        name: str,
        *,
        actual: Any = None,
        expected: Any = None,
        detail: str = "",
    ) -> bool:
        assertion = {
            "name": name,
            "passed": bool(condition),
            "actual": actual,
            "expected": expected,
            "detail": detail,
            "duration_seconds": 0.0,
        }
        if not condition:
            assertion["message"] = detail or f"expected {expected!r}, got {actual!r}"
        self.assertions.append(assertion)
        self.events.emit(
            "assertion",
            suite=self.definition.name,
            name=name,
            passed=bool(condition),
            actual=actual,
            expected=expected,
            detail=detail,
        )
        return bool(condition)

    def require(
        self,
        condition: bool,
        name: str,
        *,
        actual: Any = None,
        expected: Any = None,
        detail: str = "",
    ) -> None:
        if not self.check(condition, name, actual=actual, expected=expected, detail=detail):
            raise AssertionError(name)

    def run_command(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.events.emit("command.started", command=command, cwd=str(cwd or self.workspace.devtools))
        result = subprocess.run(
            command,
            cwd=cwd or self.workspace.devtools,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        with (self.paths.logs / "commands.log").open("a", encoding="utf-8") as stream:
            stream.write(f"$ {' '.join(command)}\n{result.stdout}{result.stderr}\n")
        self.events.emit("command.finished", command=command, returncode=result.returncode)
        return result

    def close(self) -> None:
        if self.control is not None:
            self.control.close()
            self.control = None


@contextmanager
def scenario_deadline(seconds: float) -> Iterator[None]:
    if seconds <= 0:
        raise ValueError("scenario timeout must be positive")
    prior_handler = signal.getsignal(signal.SIGALRM)

    def expired(_signum: int, _frame: object) -> None:
        raise ScenarioTimeout(f"scenario exceeded {seconds:g} seconds")

    signal.signal(signal.SIGALRM, expired)
    prior_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *prior_timer)
        signal.signal(signal.SIGALRM, prior_handler)


def _layout(paths: RunPaths) -> SimLayout:
    return SimLayout(
        root=paths.root,
        run=paths.run,
        flash=paths.flash,
        shots=paths.shots,
        logs=paths.logs,
        state=paths.run / "state.json",
        stdout=paths.stdout,
        stderr=paths.stderr,
    )


def _copy_persistence_artifacts(paths: RunPaths) -> list[str]:
    copied: list[str] = []
    for name in ("infinisim-ble-bonds.bin", "infinisim-ble-bonds.bin.next"):
        source = paths.run / name
        if source.exists():
            target = paths.flash / name
            shutil.copy2(source, target)
            copied.append(str(target))
    return copied


def run_scenario(
    workspace: Workspace,
    definition: ScenarioDefinition,
    *,
    runs_root: Path,
    timeout: float | None = None,
) -> tuple[int, RunPaths, dict[str, Any]]:
    paths = create_run_paths(
        runs_root,
        run_id=f"{time.strftime('%Y%m%dT%H%M%SZ')}-{definition.name}-{uuid.uuid4().hex[:8]}",
    )
    events = EventWriter(paths.events)
    transcript = TranscriptWriter(paths.transcript)
    simulator = Simulator(workspace, _layout(paths))
    started_monotonic = time.monotonic()
    started_at = utc_now()
    result: dict[str, Any] = {
        "schema_version": 1,
        "run_id": paths.root.name,
        "target": "sim",
        "suite": definition.name,
        "description": definition.description,
        "tags": sorted(definition.tags),
        "status": "running",
        "started_at": started_at,
        "finished_at": None,
        "duration_seconds": None,
        "check_count": 0,
        "failure_count": 0,
        "error": None,
        "workspace_root": str(workspace.root),
        "repositories": collect_repository_states(workspace),
        "allocations": {},
        "artifacts": {
            "root": str(paths.root),
            "result": str(paths.result),
            "junit": str(paths.junit),
            "events": str(paths.events),
            "transcript": str(paths.transcript),
            "stdout": str(paths.stdout),
            "stderr": str(paths.stderr),
            "run_dir": str(paths.run),
            "flash_dir": str(paths.flash),
            "shots_dir": str(paths.shots),
        },
    }
    JsonResultWriter(paths.result).write(result)
    events.emit("run.started", suite=definition.name, target="sim")
    context: ScenarioContext | None = None
    skipped: str | None = None
    try:
        state = simulator.start(headless=definition.headless)
        result["allocations"] = {
            "gatt_port": state["gatt_port"],
            "bridge_port": state["bridge_port"],
            "ble_control_port": state["ble_control_port"],
            "display": state["display"],
            "headless": state["headless"],
        }
        result["simulator"] = state
        events.emit("simulator.started", **result["allocations"], pid=state["sim_pid"])
        context = ScenarioContext(
            definition,
            workspace,
            paths,
            simulator,
            state,
            events,
            transcript,
        )
        with scenario_deadline(timeout or definition.timeout):
            definition.function(context)
        result["status"] = "passed" if all(a["passed"] for a in context.assertions) else "failed"
    except ScenarioSkip as error:
        skipped = str(error)
        result["status"] = "skipped"
        result["error"] = skipped
        events.emit("scenario.skipped", reason=skipped)
    except BaseException as error:
        result["status"] = "timed_out" if isinstance(error, ScenarioTimeout) else "failed"
        result["error"] = "".join(traceback.format_exception(error))
        events.emit(
            "scenario.failure",
            error_type=type(error).__name__,
            message=str(error),
            traceback=result["error"],
        )
    finally:
        if context is not None:
            context.close()
            result["check_count"] = len(context.assertions)
            result["failure_count"] = sum(not assertion["passed"] for assertion in context.assertions)
        stopped = simulator.stop()
        events.emit("simulator.stopped", stopped=stopped)
        persistence = _copy_persistence_artifacts(paths)
        if persistence:
            result["artifacts"]["persistence"] = persistence
        duration = time.monotonic() - started_monotonic
        result["duration_seconds"] = duration
        result["finished_at"] = utc_now()
        assertions = context.assertions if context is not None else []
        write_junit(
            paths.junit,
            suite=definition.name,
            duration_seconds=duration,
            assertions=assertions,
            error=result["error"] if result["status"] in {"failed", "timed_out"} else None,
            skipped=skipped,
        )
        JsonResultWriter(paths.result).write(result)
        events.emit(
            "run.finished",
            status=result["status"],
            duration_seconds=duration,
            check_count=result["check_count"],
            failure_count=result["failure_count"],
        )
    return (0 if result["status"] in {"passed", "skipped"} else 1), paths, result
