from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from symphony_windows.workflow import WorkflowError, load_workflow
from symphony_windows.workspace import WorkspaceManager, workspace_key


ROOT = Path(__file__).resolve().parents[1]


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def test_repository_workflow_is_one_generic_agent(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-token")
    workflow = load_workflow(ROOT / "WORKFLOW.md")
    assert workflow.agent.max_concurrent_agents >= 1
    assert workflow.agent.max_turns > 1
    assert workflow.agent.sandbox == "danger-full-access"
    assert "fskill-analysis-tech" in workflow.agent.skills
    assert not hasattr(workflow, "agent_profiles")
    prompt = workflow.render_prompt(
        {
            "id": "ISSUE-001",
            "identifier": "ISSUE-001",
            "title": "Implement endpoint",
            "description": "Build it",
            "acceptance_criteria": ["Tests pass"],
            "repository": {"url": "repo", "base_branch": "main", "commit": "1" * 40},
        },
        2,
    )
    assert "ISSUE-001" in prompt
    assert "analyze" in prompt.lower()
    assert "implement" in prompt.lower()
    assert "test" in prompt.lower()


def test_workflow_requires_one_agent_section(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-token")
    source = (ROOT / "WORKFLOW.md").read_text(encoding="utf-8").replace("agent:\n", "agents:\n", 1)
    path = tmp_path / "WORKFLOW.md"
    path.write_text(source, encoding="utf-8")
    with pytest.raises(WorkflowError):
        load_workflow(path)


async def test_workspace_is_persistent_and_issue_scoped(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "config", "user.name", "Test")
    (source / "README.md").write_text("base\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "-m", "base")
    commit = _git(source, "rev-parse", "HEAD")

    from symphony_windows.workflow import WorkspaceConfig

    manager = WorkspaceManager(WorkspaceConfig(root=tmp_path / "workspaces"))
    issue = {
        "id": "ISSUE-001",
        "repository": {"url": str(source), "base_branch": "master", "commit": commit},
    }
    first = await manager.prepare(issue)
    marker = first.path / "agent-change.txt"
    marker.write_text("preserved", encoding="utf-8")
    second = await manager.prepare(issue)

    assert first.path == tmp_path / "workspaces" / workspace_key("ISSUE-001")
    assert second.path == first.path
    assert marker.read_text(encoding="utf-8") == "preserved"
