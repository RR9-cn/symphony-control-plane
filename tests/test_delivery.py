from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from control_plane.delivery import DeliveryError, IssueDeliveryManager, _github_repo
from symphony_windows.workspace import workspace_key


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)
    return result.stdout.strip()


async def test_prepare_local_commit_uses_issue_workspace(tmp_path: Path):
    root = tmp_path / "workspaces"
    workspace = root / workspace_key("ISSUE-001")
    workspace.mkdir(parents=True)
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "Test")
    (workspace / "README.md").write_text("base\n", encoding="utf-8")
    _git(workspace, "add", "README.md")
    _git(workspace, "commit", "-m", "base")
    (workspace / "README.md").write_text("changed\n", encoding="utf-8")

    branch, commit = await IssueDeliveryManager(root).prepare_local_commit("ISSUE-001", "User detail")

    assert branch == "codex/issue-001"
    assert commit == _git(workspace, "rev-parse", "HEAD")
    assert _git(workspace, "log", "-1", "--pretty=%s") == "feat: User detail"


async def test_prepare_rejects_missing_workspace(tmp_path: Path):
    with pytest.raises(DeliveryError, match="workspace does not exist"):
        await IssueDeliveryManager(tmp_path).prepare_local_commit("ISSUE-999", "Missing")


def test_github_repository_parsing():
    assert _github_repo("git@github.com:openai/symphony.git") == "openai/symphony"
    assert _github_repo("https://github.com/openai/symphony") == "openai/symphony"
    with pytest.raises(DeliveryError):
        _github_repo("https://gitlab.com/openai/symphony")
