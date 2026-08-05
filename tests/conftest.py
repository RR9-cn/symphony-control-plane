from __future__ import annotations

import sys
import subprocess
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from control_plane.app import create_app  # noqa: E402
from control_plane.config import Settings  # noqa: E402
from control_plane.database import Database  # noqa: E402
from control_plane.models import Base  # noqa: E402


_CURRENT_PROJECT_ID = ""


@pytest.fixture(autouse=True)
def project_runtime_environment(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-token")
    monkeypatch.setenv("SYMPHONY_PROJECT_ID", "unit-test-project")


def _project_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "project"
    repository.mkdir()
    source = (ROOT / "WORKFLOW.md").read_text(encoding="utf-8")
    (repository / "WORKFLOW.md").write_text(source, encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    subprocess.run(["git", "add", "WORKFLOW.md"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "test project"], cwd=repository, check=True, capture_output=True)
    return repository


async def _register_project(client: httpx.AsyncClient, repository: Path) -> str:
    response = await client.post(
        "/api/projects",
        json={
            "key": "test-project",
            "name": "Test Project",
            "repository_path": str(repository),
            "default_branch": "master",
            "workflow_path": "WORKFLOW.md",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "available", response.text
    return str(response.json()["id"])


@pytest_asyncio.fixture
async def api(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}",
        enable_lease_sweeper=False,
        default_retry_delay_seconds=0,
        issue_workspace_root=str(tmp_path / "workspaces"),
    )
    database = Database(settings)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    app = create_app(settings, database)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.app = app  # type: ignore[attr-defined]
        global _CURRENT_PROJECT_ID
        _CURRENT_PROJECT_ID = await _register_project(client, _project_repository(tmp_path))
        client.project_id = _CURRENT_PROJECT_ID  # type: ignore[attr-defined]
        client.headers.pop("Authorization", None)
        yield client
    await database.dispose()


@pytest_asyncio.fixture
async def authenticated_api(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'authenticated.db').as_posix()}",
        enable_lease_sweeper=False,
        api_token="integration-secret",
        issue_workspace_root=str(tmp_path / "workspaces"),
    )
    database = Database(settings)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    app = create_app(settings, database)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": "Bearer integration-secret"}) as client:
        client.app = app  # type: ignore[attr-defined]
        global _CURRENT_PROJECT_ID
        _CURRENT_PROJECT_ID = await _register_project(client, _project_repository(tmp_path))
        client.project_id = _CURRENT_PROJECT_ID  # type: ignore[attr-defined]
        client.headers.pop("Authorization", None)
        yield client
    await database.dispose()


def issue_payload(issue_id: str = "ISSUE-001") -> dict[str, object]:
    return {
        "id": issue_id,
        "project_id": _CURRENT_PROJECT_ID,
        "title": "Add user detail endpoint",
        "description": "Implement and test an endpoint that returns one user.",
        "priority": 1,
        "acceptance_criteria": ["Endpoint returns the requested user", "Tests pass"],
    }


def claim_payload(version: int = 1) -> dict[str, object]:
    return {
        "workerId": "windows-symphony-managed",
        "projectId": _CURRENT_PROJECT_ID,
        "expectedVersion": version,
        "leaseSeconds": 300,
        "agent": {"config": {"kind": "coding_agent", "skills": [], "max_turns": 20}},
    }
