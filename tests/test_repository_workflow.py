from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from control_plane.project_service import ProjectService
import symphony_windows.workspace as workspace_module
from symphony_windows.workflow import HookConfig, SkillSourceConfig, WorkspaceConfig, load_workflow
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
    assert not hasattr(workflow, "skill_repository")
    assert not hasattr(workflow, "agent_profiles")
    prompt = workflow.render_prompt(
        {
            "id": "ISSUE-001",
            "identifier": "ISSUE-001",
            "title": "Implement endpoint",
            "description": "Build it",
            "acceptance_criteria": ["Tests pass"],
            "project_id": "unit-test-project",
            "source_commit": "1" * 40,
            "workflow_revision": "2" * 64,
        },
        2,
    )
    assert "ISSUE-001" in prompt
    assert "analyze" in prompt.lower()
    assert "implement" in prompt.lower()
    assert "test" in prompt.lower()


def test_workflow_uses_agent_defaults_when_section_is_absent(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-token")
    source = (ROOT / "WORKFLOW.md").read_text(encoding="utf-8").replace("agent:\n", "agents:\n", 1)
    path = tmp_path / "WORKFLOW.md"
    path.write_text(source, encoding="utf-8")
    workflow = load_workflow(path)
    assert workflow.agent.max_concurrent_agents == 10
    assert workflow.agent.max_turns == 20


def test_workflow_preserves_powershell_hooks_that_start_with_dollar(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-token")
    source = (ROOT / "WORKFLOW.md").read_text(encoding="utf-8")
    path = tmp_path / "WORKFLOW.md"
    path.write_text(source, encoding="utf-8")

    workflow = load_workflow(path)

    assert workflow.workspace.hooks.after_create is not None
    assert workflow.workspace.hooks.after_create.startswith('$ErrorActionPreference = "Stop"')
    assert workflow.workspace.hooks.before_run is not None
    assert workflow.workspace.hooks.before_run.startswith('$ErrorActionPreference = "Stop"')


def test_workflow_parses_pinned_external_skills(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-token")
    source = (ROOT / "WORKFLOW.md").read_text(encoding="utf-8").replace(
        "hooks:\n",
        "skills:\n"
        "  repository: D:/shared/fshows-skills\n"
        f"  revision: {'a' * 40}\n"
        "  source_path: coding\n"
        "  target_path: .codex/skills\n\n"
        "hooks:\n",
        1,
    )
    path = tmp_path / "WORKFLOW.md"
    path.write_text(source, encoding="utf-8")

    workflow = load_workflow(path)

    assert workflow.skills == SkillSourceConfig(
        repository="D:/shared/fshows-skills",
        revision="a" * 40,
        source_path="coding",
        target_path=".codex/skills",
    )


async def test_external_skill_manifest_uses_pinned_git_revision(tmp_path: Path):
    repository = tmp_path / "shared-skills"
    skill = repository / "coding" / "example-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: example-skill\ndescription: Example\n---\n", encoding="utf-8")
    _git(repository, "init")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test")
    _git(repository, "add", "coding/example-skill/SKILL.md")
    _git(repository, "commit", "-m", "add skill")
    revision = _git(repository, "rev-parse", "HEAD")
    service = ProjectService(None)  # type: ignore[arg-type]

    manifest = await service._external_skill_manifest(
        SkillSourceConfig(repository=str(repository), revision=revision)
    )

    assert [entry["name"] for entry in manifest] == ["example-skill"]
    assert manifest[0]["path"].endswith("/coding/example-skill/SKILL.md")
    assert len(manifest[0]["sha256"]) == 64


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
        "project_id": "unit-test-project",
        "source_commit": commit,
    }
    first = await manager.prepare(issue)
    marker = first.path / "agent-change.txt"
    marker.write_text("preserved", encoding="utf-8")
    second = await manager.prepare(issue)

    assert first.path == tmp_path / "workspaces" / workspace_key("ISSUE-001")
    assert second.path == first.path
    assert marker.read_text(encoding="utf-8") == "preserved"


async def test_workspace_remove_runs_before_remove_hook(tmp_path: Path):
    manager = WorkspaceManager(WorkspaceConfig(root=tmp_path / "workspaces"))
    issue = {"id": "ISSUE-REMOVE"}
    prepared = await manager.prepare(issue)
    marker = prepared.path / "change.txt"
    marker.write_text("remove me", encoding="utf-8")
    calls: list[tuple[str, Path, bool]] = []

    async def record_hook(
        name: str,
        _issue: dict[str, object],
        workspace: Path,
        *,
        ignore_failure: bool = False,
    ) -> None:
        assert marker.exists()
        calls.append((name, workspace, ignore_failure))

    manager.run_hook = record_hook  # type: ignore[method-assign]
    assert await manager.remove(issue) is True
    assert calls == [("before_remove", prepared.path, True)]
    assert not prepared.path.exists()
    assert await manager.remove(issue) is False


async def test_cancelled_workspace_prepare_removes_partial_directory(tmp_path: Path):
    manager = WorkspaceManager(WorkspaceConfig(root=tmp_path / "workspaces"))
    hook_started = asyncio.Event()
    release_hook = asyncio.Event()

    async def blocking_hook(
        _name: str,
        _issue: dict[str, object],
        workspace: Path,
        *,
        ignore_failure: bool = False,
    ) -> None:
        assert ignore_failure is False
        (workspace / "partial-clone.lock").write_text("active", encoding="utf-8")
        hook_started.set()
        await release_hook.wait()

    manager.run_hook = blocking_hook  # type: ignore[method-assign]
    task = asyncio.create_task(manager.prepare({"id": "ISSUE-CANCELLED"}))
    await hook_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert not (tmp_path / "workspaces" / "ISSUE-CANCELLED").exists()


async def test_cancelled_hook_terminates_subprocess_tree(tmp_path: Path, monkeypatch):
    manager = WorkspaceManager(
        WorkspaceConfig(
            root=tmp_path / "workspaces",
            hooks=HookConfig(after_create="Write-Output running"),
        )
    )
    communicate_started = asyncio.Event()
    terminated: list[object] = []

    class FakeProcess:
        pid = 123
        returncode = None

        async def communicate(self):
            communicate_started.set()
            await asyncio.Event().wait()

    process = FakeProcess()

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return process

    async def fake_terminate(candidate):
        terminated.append(candidate)

    monkeypatch.setattr(workspace_module.shutil, "which", lambda _name: "pwsh")
    monkeypatch.setattr(
        workspace_module.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(workspace_module, "_terminate_process_tree", fake_terminate)

    task = asyncio.create_task(
        manager.run_hook("after_create", {"id": "ISSUE-CANCELLED"}, tmp_path)
    )
    await communicate_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert terminated == [process]
