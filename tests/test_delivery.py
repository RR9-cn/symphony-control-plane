from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from control_plane.delivery import FeatureDeliveryManager


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.mark.asyncio
async def test_prepare_local_commit_uses_feature_workspace_and_excludes_runtime(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspaces"
    workspace = workspace_root / "FEATURE-001"
    workspace.mkdir(parents=True)
    _git(workspace, "init", "--quiet")
    (workspace / "service.txt").write_text("base\n", encoding="utf-8")
    _git(workspace, "add", "service.txt")
    _git(
        workspace,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "base",
    )
    base = _git(workspace, "rev-parse", "HEAD")
    (workspace / "service.txt").write_text("implemented\n", encoding="utf-8")
    runtime = workspace / ".symphony" / "runtime.json"
    runtime.parent.mkdir()
    runtime.write_text("{}\n", encoding="utf-8")

    manager = FeatureDeliveryManager(
        str(Path(__file__).resolve().parents[1] / "WORKFLOW.md"),
        workspace_root=workspace_root,
    )
    branch, commit = await manager.prepare_local_commit("FEATURE-001", "Feature one")

    assert branch == "codex/feature-001"
    assert commit != base
    assert _git(workspace, "branch", "--show-current") == branch
    assert _git(workspace, "show", "HEAD:service.txt") == "implemented"
    assert ".symphony/runtime.json" not in _git(
        workspace, "ls-tree", "-r", "--name-only", "HEAD"
    )
