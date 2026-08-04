from __future__ import annotations

import hashlib
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.validate_workflow import expected_roles, validate
from symphony_windows.workflow import WorkspaceConfig
from symphony_windows.workspace import WorkspaceError, WorkspaceManager, workspace_key


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_formal_workflow_matches_roles_prompts_and_pinned_skills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "workflow-validation-only")
    monkeypatch.setenv("SYMPHONY_WORKER_ID", "workflow-validator")
    monkeypatch.setenv("FSHOWS_SKILLS_REPOSITORY", str(ROOT))
    monkeypatch.setenv("FSHOWS_SKILLS_REVISION", revision)

    workflow = await validate(ROOT / "WORKFLOW.md")

    assert {profile.agent_role for profile in workflow.agent_profiles} == set(
        expected_roles()
    )
    assert workflow.tracker.worker_id == "workflow-validator"
    assert workflow.skill_repository.revision == revision
    assert workflow.workspace.hooks.after_create
    assert "repository.commit" in workflow.workspace.hooks.after_create
    assert "checkout --detach" in workflow.workspace.hooks.after_create


@pytest.mark.asyncio
async def test_formal_workspace_hook_clones_exact_work_item_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "workflow-validation-only")
    monkeypatch.setenv("FSHOWS_SKILLS_REPOSITORY", str(ROOT))
    monkeypatch.setenv("FSHOWS_SKILLS_REVISION", revision)
    workflow = await validate(ROOT / "WORKFLOW.md", check_skills=False)
    manager = WorkspaceManager(
        replace(workflow.workspace, root=tmp_path / "workspaces")
    )

    prepared = await manager.prepare(
        {
            "id": "WI-WORKFLOW-001",
            "identifier": "WI-WORKFLOW-001",
            "repository": {
                "url": str(ROOT),
                "commit": revision,
            },
        }
    )

    actual = subprocess.run(
        ["git", "-C", str(prepared.path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert prepared.created is True
    assert actual == revision


@pytest.mark.asyncio
async def test_failed_formal_workspace_hook_leaves_no_partial_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "workflow-validation-only")
    monkeypatch.setenv("FSHOWS_SKILLS_REPOSITORY", str(ROOT))
    monkeypatch.setenv("FSHOWS_SKILLS_REVISION", revision)
    workflow = await validate(ROOT / "WORKFLOW.md", check_skills=False)
    workspace_root = tmp_path / "workspaces"
    manager = WorkspaceManager(replace(workflow.workspace, root=workspace_root))
    identifier = "WI-WORKFLOW-FAILED"

    with pytest.raises(WorkspaceError, match="repository.commit"):
        await manager.prepare(
            {
                "id": identifier,
                "identifier": identifier,
                "repository": {
                    "url": str(ROOT),
                    "commit": "not-an-immutable-commit",
                },
            }
        )

    assert not (workspace_root / workspace_key(identifier)).exists()


def test_workspace_materializes_registered_dependency_artifacts(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspaces"
    manager = WorkspaceManager(WorkspaceConfig(root=workspace_root))
    source = workspace_root / workspace_key("WI-001")
    target = workspace_root / workspace_key("WI-002")
    relative = Path("orchestration/handoffs/WI-001.yaml")
    source_file = source / relative
    source_file.parent.mkdir(parents=True)
    source_file.write_text("work_item_id: WI-001\n", encoding="utf-8")
    target.mkdir(parents=True)
    checksum = hashlib.sha256(source_file.read_bytes()).hexdigest()

    materialized = manager.materialize_input_artifacts(
        {
            "dependencies": ["WI-001"],
            "input_artifacts": [
                {
                    "path": relative.as_posix(),
                    "revision": "attempt-1",
                    "sha256": checksum,
                }
            ],
        },
        target,
    )

    assert materialized == (relative.as_posix(),)
    assert (target / relative).read_bytes() == source_file.read_bytes()
