from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from control_plane.app import create_app  # noqa: E402
from control_plane.config import Settings  # noqa: E402
from control_plane.database import Database  # noqa: E402
from control_plane.models import Base  # noqa: E402


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
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.app = app  # type: ignore[attr-defined]
        yield client
    await database.dispose()


def issue_payload(issue_id: str = "ISSUE-001") -> dict[str, object]:
    return {
        "id": issue_id,
        "title": "Add user detail endpoint",
        "description": "Implement and test an endpoint that returns one user.",
        "priority": 1,
        "repository": {
            "url": "https://github.com/example/catalog.git",
            "base_branch": "main",
            "commit": "1" * 40,
        },
        "acceptance_criteria": ["Endpoint returns the requested user", "Tests pass"],
    }


def claim_payload(version: int = 1) -> dict[str, object]:
    return {
        "workerId": "windows-symphony-managed",
        "expectedVersion": version,
        "leaseSeconds": 300,
        "agent": {"config": {"kind": "coding_agent", "skills": [], "max_turns": 20}},
    }
