from __future__ import annotations

import subprocess
from pathlib import Path

from control_plane.app import _refresh_projects_once
from control_plane.project_service import ProjectService
from conftest import claim_payload, issue_payload


async def test_project_snapshot_is_pinned_into_new_issue(api) -> None:
    projects = (await api.get("/api/projects")).json()
    assert len(projects) == 1
    project = projects[0]
    assert project["status"] == "available"
    assert len(project["current_snapshot"]["source_commit"]) == 40
    assert len(project["current_snapshot"]["workflow_revision"]) == 64

    response = await api.post("/api/issues", json=issue_payload())
    assert response.status_code == 201, response.text
    issue = response.json()
    assert issue["project_id"] == project["id"]
    assert issue["workflow_snapshot_id"] == project["current_snapshot_id"]
    assert issue["source_commit"] == project["current_snapshot"]["source_commit"]
    assert issue["workflow_revision"] == project["current_snapshot"]["workflow_revision"]
    assert issue["workspace_path"].endswith("ISSUE-001")


async def test_new_issue_refreshes_default_branch_snapshot(api) -> None:
    project = (await api.get("/api/projects")).json()[0]
    repository = Path(project["repository_path"])
    (repository / "latest.txt").write_text("latest\n", encoding="utf-8")
    subprocess.run(["git", "add", "latest.txt"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-m", "advance default branch"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    latest = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    response = await api.post("/api/issues", json=issue_payload())

    assert response.status_code == 201, response.text
    assert response.json()["source_commit"] == latest
    refreshed = (await api.get(f"/api/projects/{project['id']}")).json()
    assert refreshed["current_snapshot"]["source_commit"] == latest


async def test_project_snapshot_uses_default_branch_not_checked_out_head(
    api, tmp_path: Path
) -> None:
    repository = tmp_path / "project-on-side-branch"
    repository.mkdir()
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    source_project = (await api.get("/api/projects")).json()[0]
    workflow = Path(source_project["repository_path"]) / "WORKFLOW.md"
    (repository / "WORKFLOW.md").write_text(
        workflow.read_text(encoding="utf-8"), encoding="utf-8"
    )
    subprocess.run(["git", "add", "WORKFLOW.md"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-m", "default branch"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    default_commit = subprocess.run(
        ["git", "rev-parse", "master"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "checkout", "-b", "side"], cwd=repository, check=True, capture_output=True)
    (repository / "side.txt").write_text("side\n", encoding="utf-8")
    subprocess.run(["git", "add", "side.txt"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-m", "side branch"],
        cwd=repository,
        check=True,
        capture_output=True,
    )

    response = await api.post(
        "/api/projects",
        json={
            "key": "side-project",
            "name": "Side Project",
            "repository_path": str(repository),
            "default_branch": "master",
            "workflow_path": "WORKFLOW.md",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["current_snapshot"]["source_commit"] == default_commit


async def test_project_refresh_continues_after_one_project_fails(
    api, tmp_path: Path, monkeypatch
) -> None:
    first = (await api.get("/api/projects")).json()[0]
    repository = tmp_path / "second-project"
    subprocess.run(
        ["git", "clone", "--quiet", first["repository_path"], str(repository)],
        check=True,
        capture_output=True,
    )
    second_response = await api.post(
        "/api/projects",
        json={
            "key": "second-project",
            "name": "Second Project",
            "repository_path": str(repository),
            "default_branch": "master",
            "workflow_path": "WORKFLOW.md",
        },
    )
    assert second_response.status_code == 201, second_response.text
    second = second_response.json()
    calls: list[str] = []

    async def refresh(_self, project_id: str):
        calls.append(project_id)
        if project_id == first["id"]:
            raise RuntimeError("first project failed")

    monkeypatch.setattr(ProjectService, "refresh_if_changed", refresh)
    failures = api.app.state.project_refresh_failures

    await _refresh_projects_once(api.app)

    assert set(calls) == {first["id"], second["id"]}
    assert api.app.state.project_refresh_failures == failures + 1


async def test_missing_workflow_is_bootstrapped_without_auto_commit(api, tmp_path: Path) -> None:
    repository = tmp_path / "project-without-workflow"
    repository.mkdir()
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    (repository / "README.md").write_text("project\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repository, check=True, capture_output=True)

    response = await api.post(
        "/api/projects",
        json={
            "key": "bootstrapped-project",
            "name": "Bootstrapped Project",
            "repository_path": str(repository),
            "default_branch": "master",
        },
    )
    assert response.status_code == 201, response.text
    project = response.json()
    workflow = repository / "WORKFLOW.md"
    assert workflow.is_file()
    assert "generated" in project["validation_error"]
    assert project["status"] == "invalid"
    assert "?? WORKFLOW.md" in subprocess.run(
        ["git", "status", "--porcelain"], cwd=repository, check=True, capture_output=True, text=True
    ).stdout

    subprocess.run(["git", "add", "WORKFLOW.md"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "add project workflow"], cwd=repository, check=True, capture_output=True)
    validated = await api.post(f"/api/projects/{project['id']}/validate")
    assert validated.status_code == 200
    assert validated.json()["status"] == "available"


async def test_issue_rejects_repository_override(api) -> None:
    payload = issue_payload()
    payload["repository"] = {"url": "wrong", "base_branch": "main", "commit": "1" * 40}
    response = await api.post("/api/issues", json=payload)
    assert response.status_code == 422


async def test_claim_is_project_scoped(api) -> None:
    created = (await api.post("/api/issues", json=issue_payload())).json()
    command = claim_payload(created["version"])
    command["projectId"] = "another-project"
    response = await api.post(f"/api/issues/{created['id']}/claim", json=command)
    assert response.status_code == 409


async def test_project_runtime_view_and_delete(api) -> None:
    project = (await api.get("/api/projects")).json()[0]
    runtime = await api.get(f"/api/projects/{project['id']}/runtime")
    assert runtime.status_code == 200
    assert runtime.json()["state"] == "stopped"
    deleted = await api.delete(f"/api/projects/{project['id']}")
    assert deleted.status_code == 204
    assert (await api.get("/api/projects")).json() == []
    response = await api.post("/api/issues", json=issue_payload())
    assert response.status_code == 404


async def test_project_with_issue_cannot_be_deleted(api) -> None:
    project = (await api.get("/api/projects")).json()[0]
    assert (await api.post("/api/issues", json=issue_payload())).status_code == 201
    response = await api.delete(f"/api/projects/{project['id']}")
    assert response.status_code == 409
    assert "has Issues" in response.json()["error"]["message"]
    assert (await api.get(f"/api/projects/{project['id']}")).status_code == 200


async def test_workflow_validation_uses_last_valid_snapshot(api) -> None:
    project = (await api.get("/api/projects")).json()[0]
    issue = (await api.post("/api/issues", json=issue_payload())).json()
    repository = Path(project["repository_path"])
    workflow = repository / "WORKFLOW.md"
    original = workflow.read_text(encoding="utf-8")
    workflow.write_text("invalid workflow", encoding="utf-8")
    invalid = await api.post(f"/api/projects/{project['id']}/validate")
    assert invalid.status_code == 200
    assert invalid.json()["status"] == "invalid"
    assert invalid.json()["current_snapshot_id"] == project["current_snapshot_id"]

    workflow.write_text(original.replace("interval_ms: 5000", "interval_ms: 5100"), encoding="utf-8")
    subprocess.run(["git", "add", "WORKFLOW.md"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "workflow update"], cwd=repository, check=True, capture_output=True)
    valid = await api.post(f"/api/projects/{project['id']}/validate")
    assert valid.status_code == 200
    assert valid.json()["status"] == "available"
    assert valid.json()["current_snapshot_id"] != project["current_snapshot_id"]
    snapshots = (await api.get(f"/api/projects/{project['id']}/workflow-snapshots")).json()
    assert len(snapshots) == 3
    assert {snapshot["status"] for snapshot in snapshots} == {"valid", "invalid"}

    claim = await api.post(f"/api/issues/{issue['id']}/claim", json=claim_payload(issue["version"]))
    assert claim.status_code == 200, claim.text
    assert "interval_ms: 5000" in claim.json()["workflow_content"]
    config = claim.json()["attempt"]["config_snapshot"]
    assert config["workflow_snapshot_id"] == issue["workflow_snapshot_id"]
    assert config["workflow_revision"] == issue["workflow_revision"]
    assert config["source_commit"] == issue["source_commit"]
    assert len(config["repository_path_hash"]) == 64
