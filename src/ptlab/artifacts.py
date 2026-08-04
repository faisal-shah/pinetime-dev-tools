from __future__ import annotations

import os
import subprocess
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import orjson

from ptlab.allocation import allocate_display, allocate_tcp_port
from ptlab.workspace import Workspace


RESULT_SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, value: Any) -> None:
    path.write_bytes(orjson.dumps(value, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS) + b"\n")


def repository_state(path: Path) -> dict[str, Any]:
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    return {"path": str(path), "sha": sha, "dirty": dirty}


def collect_repository_states(workspace: Workspace) -> dict[str, dict[str, Any]]:
    return {name: repository_state(path) for name, path in workspace.repositories.items()}


@dataclass(frozen=True)
class RunPaths:
    root: Path
    run: Path
    flash: Path
    shots: Path
    logs: Path
    result: Path
    junit: Path
    events: Path
    transcript: Path
    stdout: Path
    stderr: Path


class ResultWriter(Protocol):
    def write(self, result: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True)
class JsonResultWriter:
    path: Path

    def write(self, result: Mapping[str, Any]) -> None:
        _write_json(self.path, result)


class EventWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.sequence = 0

    def emit(self, event: str, **fields: Any) -> None:
        record = {"sequence": self.sequence, "timestamp": utc_now(), "event": event, **fields}
        self.sequence += 1
        with self.path.open("ab") as stream:
            stream.write(orjson.dumps(record, option=orjson.OPT_SORT_KEYS))
            stream.write(b"\n")


def write_junit(
    path: Path,
    *,
    suite: str,
    duration_seconds: float,
    assertions: list[Mapping[str, Any]],
    error: str | None = None,
    skipped: str | None = None,
) -> None:
    failures = sum(not bool(assertion["passed"]) for assertion in assertions)
    errors = int(error is not None)
    skipped_count = int(skipped is not None)
    tests = len(assertions) + errors + skipped_count
    root = ET.Element(
        "testsuite",
        {
            "name": suite,
            "tests": str(tests),
            "failures": str(failures),
            "errors": str(errors),
            "skipped": str(skipped_count),
            "time": f"{duration_seconds:.6f}",
        },
    )
    for assertion in assertions:
        case = ET.SubElement(
            root,
            "testcase",
            {
                "classname": f"ptlab.{suite}",
                "name": str(assertion["name"]),
                "time": f"{float(assertion.get('duration_seconds', 0.0)):.6f}",
            },
        )
        if not assertion["passed"]:
            failure = ET.SubElement(case, "failure", {"message": str(assertion.get("message", "assertion failed"))})
            failure.text = str(assertion.get("detail", ""))
    if error is not None:
        case = ET.SubElement(root, "testcase", {"classname": f"ptlab.{suite}", "name": "scenario"})
        node = ET.SubElement(case, "error", {"message": error.splitlines()[0]})
        node.text = error
    if skipped is not None:
        case = ET.SubElement(root, "testcase", {"classname": f"ptlab.{suite}", "name": "scenario"})
        ET.SubElement(case, "skipped", {"message": skipped})
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)


def create_run_paths(runs_root: Path, run_id: str | None = None) -> RunPaths:
    identifier = run_id or (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    )
    root = runs_root / identifier
    root.mkdir(parents=True, mode=0o700)
    os.chmod(root, 0o700)
    directories = {name: root / name for name in ("run", "flash", "shots", "logs")}
    for path in directories.values():
        path.mkdir(mode=0o700)
    stdout = directories["logs"] / "stdout.log"
    stderr = directories["logs"] / "stderr.log"
    stdout.touch()
    stderr.touch()
    return RunPaths(
        root=root,
        run=directories["run"],
        flash=directories["flash"],
        shots=directories["shots"],
        logs=directories["logs"],
        result=root / "result.json",
        junit=root / "junit.xml",
        events=root / "events.ndjson",
        transcript=root / "transcript.ndjson",
        stdout=stdout,
        stderr=stderr,
    )


def create_run_skeleton(
    workspace: Workspace,
    *,
    runs_root: Path,
    port_allocator: Callable[[], int] = allocate_tcp_port,
    display_allocator: Callable[[], str] = allocate_display,
    repository_collector: Callable[[Workspace], dict[str, dict[str, Any]]] = collect_repository_states,
) -> RunPaths:
    started_at = utc_now()
    paths = create_run_paths(runs_root)
    events = EventWriter(paths.events)
    events.emit("run.created", run_id=paths.root.name)
    gatt_port = port_allocator()
    ble_control_port = port_allocator()
    while ble_control_port == gatt_port:
        ble_control_port = port_allocator()

    result = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "run_id": paths.root.name,
        "status": "created",
        "started_at": started_at,
        "finished_at": None,
        "workspace_root": str(workspace.root),
        "allocations": {
            "bridge_port": gatt_port,
            "gatt_port": gatt_port,
            "ble_control_port": ble_control_port,
            "display": display_allocator(),
        },
        "repositories": repository_collector(workspace),
        "artifacts": {
            "result": str(paths.result),
            "events": str(paths.events),
            "junit": str(paths.junit),
            "transcript": str(paths.transcript),
            "stdout": str(paths.stdout),
            "stderr": str(paths.stderr),
            "run_dir": str(paths.run),
            "flash_dir": str(paths.flash),
            "shots_dir": str(paths.shots),
        },
    }
    JsonResultWriter(paths.result).write(result)
    events.emit("run.ready", status=result["status"])
    return paths
