from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


REPOSITORY_NAMES = (
    "InfiniTime",
    "InfiniSim",
    "PineTimeCompanion",
    "pinetime-dev-tools",
)


class WorkspaceError(ValueError):
    """The requested directory is not a complete PineTime workspace."""


@dataclass(frozen=True)
class Workspace:
    root: Path

    @property
    def infinitime(self) -> Path:
        return self.root / "InfiniTime"

    @property
    def infinisim(self) -> Path:
        return self.root / "InfiniSim"

    @property
    def companion(self) -> Path:
        return self.root / "PineTimeCompanion"

    @property
    def devtools(self) -> Path:
        return self.root / "pinetime-dev-tools"

    @property
    def repositories(self) -> dict[str, Path]:
        return {name: self.root / name for name in REPOSITORY_NAMES}


def _normalize_candidate(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.name in REPOSITORY_NAMES:
        return resolved.parent
    return resolved


def _is_workspace(path: Path) -> bool:
    return all((path / name).is_dir() for name in REPOSITORY_NAMES)


def discover_workspace(
    workspace_root: Path | None = None,
    *,
    search_from: Iterable[Path] | None = None,
) -> Workspace:
    if workspace_root is not None:
        candidate = _normalize_candidate(workspace_root)
        if not _is_workspace(candidate):
            missing = [name for name in REPOSITORY_NAMES if not (candidate / name).is_dir()]
            raise WorkspaceError(
                f"{candidate} is not a PineTime workspace; missing: {', '.join(missing)}"
            )
        return Workspace(candidate)

    package_repo = Path(__file__).resolve().parents[2]
    starts = list(search_from or (Path.cwd(), package_repo))
    checked: set[Path] = set()
    for start in starts:
        resolved = start.expanduser().resolve()
        for path in (resolved, *resolved.parents):
            candidate = _normalize_candidate(path)
            if candidate in checked:
                continue
            checked.add(candidate)
            if _is_workspace(candidate):
                return Workspace(candidate)

    raise WorkspaceError(
        "could not discover the PineTime workspace; pass --workspace-root "
        "with the directory containing InfiniTime, InfiniSim, "
        "PineTimeCompanion, and pinetime-dev-tools"
    )
