from pathlib import Path

import pytest

from ptlab.workspace import REPOSITORY_NAMES, WorkspaceError, discover_workspace


def make_workspace(root: Path) -> Path:
    for name in REPOSITORY_NAMES:
        (root / name).mkdir(parents=True)
    return root


def test_discovers_workspace_from_nested_repository_path(tmp_path: Path) -> None:
    root = make_workspace(tmp_path / "family")
    nested = root / "pinetime-dev-tools" / "src"
    nested.mkdir()

    workspace = discover_workspace(search_from=[nested])

    assert workspace.root == root
    assert workspace.infinitime == root / "InfiniTime"


def test_explicit_repository_path_resolves_to_workspace_parent(tmp_path: Path) -> None:
    root = make_workspace(tmp_path / "family")

    workspace = discover_workspace(root / "InfiniTime")

    assert workspace.root == root


def test_incomplete_explicit_workspace_has_actionable_error(tmp_path: Path) -> None:
    (tmp_path / "InfiniTime").mkdir()

    with pytest.raises(WorkspaceError, match="missing: InfiniSim"):
        discover_workspace(tmp_path)
