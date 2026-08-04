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


def feature_payload() -> dict[str, object]:
    return {
        "id": "FEATURE-001",
        "title": "Catalog list",
        "description": "Backend control-plane fixture",
    }


def work_item_payload(
    item_id: str,
    *,
    status: str = "draft",
    dependencies: list[str] | None = None,
) -> dict[str, object]:
    role_by_id = {
        "WI-001": ("tech_analysis", "solution_architect"),
        "WI-002": ("implementation", "backend_builder"),
        "WI-003": ("code_review", "code_reviewer"),
    }
    stage, role = role_by_id.get(item_id, ("implementation", "backend_builder"))
    return {
        "id": item_id,
        "feature_id": "FEATURE-001",
        "parent_id": None,
        "title": f"Work item {item_id}",
        "description": "Fixture work item",
        "stage": stage,
        "agent_role": role,
        "status": status,
        "priority": 1,
        "repository": {
            "url": "git@example.local:catalog.git",
            "base_branch": "main",
            "head_branch": None,
            "commit": None,
            "pull_request": None,
        },
        "dependencies": dependencies or [],
        "input_artifacts": [],
        "output_artifacts": [],
        "acceptance_criteria": ["fixture passes"],
    }
