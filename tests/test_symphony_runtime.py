from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from symphony_windows.codex import CodexAppServer
from symphony_windows.orchestrator import RunningEntry, WindowsSymphony
from symphony_windows.tracker import ClaimLease
from symphony_windows.workflow import CodexConfig, load_workflow


class FakeTracker:
    def __init__(self, config: Any, terminal: list[dict[str, Any]] | None = None) -> None:
        self.config = config
        self.terminal = terminal or []

    async def terminal_issues(self) -> list[dict[str, Any]]:
        return self.terminal

    async def fetch_issues_by_states(
        self, state_names: list[str]
    ) -> list[dict[str, Any]]:
        states = {state.lower() for state in state_names}
        return [
            {
                **issue,
                "identifier": issue.get("identifier", issue["id"]),
                "state": str(issue.get("state") or issue.get("status") or "").lower(),
                "labels": issue.get("labels", []),
                "dispatchable": issue.get("dispatchable", True),
            }
            for issue in self.terminal
            if str(issue.get("state") or issue.get("status") or "").lower() in states
        ]

    async def issues_by_ids(self, issue_ids: list[str]) -> list[dict[str, Any]]:
        return [issue for issue in self.terminal if issue["id"] in issue_ids]

    async def fetch_issues_by_ids(
        self, issue_ids: list[str]
    ) -> list[dict[str, Any]]:
        return [
            {
                **issue,
                "identifier": issue.get("identifier", issue["id"]),
                "state": str(issue.get("state") or issue.get("status") or "").lower(),
                "labels": issue.get("labels", []),
                "dispatchable": issue.get("dispatchable", True),
            }
            for issue in await self.issues_by_ids(issue_ids)
        ]


class FakeSkills:
    def __init__(self) -> None:
        self.initialized = 0

    async def initialize(self) -> None:
        self.initialized += 1


class FakeWorkspaces:
    def __init__(self) -> None:
        self.removed: list[str] = []

    async def remove(self, issue: dict[str, Any]) -> bool:
        self.removed.append(str(issue["id"]))
        return True


async def test_turn_timeout_is_reset_for_each_app_server_message():
    server = CodexAppServer(CodexConfig(turn_timeout_ms=12_345))
    messages = [
        {"method": "item/started", "params": {"item": {"type": "reasoning"}}},
        {"method": "turn/completed", "params": {}},
    ]
    observed_timeouts: list[int] = []

    async def read_message(timeout_ms: int) -> dict[str, Any]:
        observed_timeouts.append(timeout_ms)
        return messages.pop(0)

    server._read_message = read_message  # type: ignore[method-assign]
    lease = ClaimLease(issue={"id": "ISSUE-1"}, token="token", attempt={})
    status = await server._receive_turn(object(), lease, "thread-1")  # type: ignore[arg-type]

    assert status == "turn_completed"
    assert observed_timeouts == [12_345, 12_345]


async def test_skill_validation_allows_codex_system_skills_in_isolated_home(
    tmp_path: Path,
):
    project_skill = tmp_path / ".codex" / "skills" / "project-skill" / "SKILL.md"
    project_skill.parent.mkdir(parents=True)
    project_skill.write_text("# Project Skill\n", encoding="utf-8")
    system_skill = (
        tmp_path
        / ".symphony"
        / "user-home"
        / ".codex"
        / "skills"
        / ".system"
        / "openai-docs"
        / "SKILL.md"
    )
    system_skill.parent.mkdir(parents=True)
    system_skill.write_text("# System Skill\n", encoding="utf-8")
    server = CodexAppServer(CodexConfig())

    async def skills_list(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "data": [
                {
                    "skills": [
                        {"name": "project-skill", "path": str(project_skill)},
                        {"name": "openai-docs", "path": str(system_skill)},
                    ]
                }
            ]
        }

    server._request = skills_list  # type: ignore[method-assign]

    discovered = await server._validate_skills(tmp_path)

    assert [skill["name"] for skill in discovered] == ["project-skill"]


async def test_completed_turn_refreshes_issue_before_next_turn(tmp_path: Path):
    server = CodexAppServer(CodexConfig(), on_event=None)
    started_turns: list[str] = []

    async def no_op(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def open_thread(*_args: Any, **_kwargs: Any) -> str:
        return "thread-1"

    async def start_turn(*_args: Any, **_kwargs: Any) -> str:
        turn_id = f"turn-{len(started_turns) + 1}"
        started_turns.append(turn_id)
        return turn_id

    async def receive_turn(*_args: Any, **_kwargs: Any) -> str:
        return "turn_completed"

    server._start = no_op  # type: ignore[method-assign]
    server._initialize = no_op  # type: ignore[method-assign]
    server._validate_skills = no_op  # type: ignore[method-assign]
    server._open_thread = open_thread  # type: ignore[method-assign]
    server._start_turn = start_turn  # type: ignore[method-assign]
    server._receive_turn = receive_turn  # type: ignore[method-assign]
    server.stop = no_op  # type: ignore[method-assign]

    class TurnTracker:
        def __init__(self) -> None:
            self.refreshes = 0

        async def update_attempt_context(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def refresh_claim(self, lease: ClaimLease) -> bool:
            self.refreshes += 1
            lease.active = False
            return False

    tracker = TurnTracker()
    lease = ClaimLease(issue={"id": "ISSUE-1"}, token="token", attempt={})
    result = await server.run(
        tmp_path,
        "work",
        lease.issue,
        tracker,  # type: ignore[arg-type]
        lease,
        max_turns=5,
    )

    assert result.status == "issue_released"
    assert result.turn_count == 1
    assert started_turns == ["turn-1"]
    assert tracker.refreshes == 1


async def test_startup_cleanup_removes_terminal_issue_workspaces(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-token")
    monkeypatch.setenv("FSHOWS_SKILLS_REPOSITORY", "https://example.test/skills.git")
    monkeypatch.setenv("FSHOWS_SKILLS_REVISION", "1" * 40)
    workflow_path = tmp_path / "WORKFLOW.md"
    source = (Path(__file__).resolve().parents[1] / "WORKFLOW.md").read_text(
        encoding="utf-8"
    )
    workflow_path.write_text(source, encoding="utf-8")
    workflow = load_workflow(workflow_path)
    tracker = FakeTracker(workflow.tracker, [{"id": "ISSUE-DONE", "status": "done"}])
    workspaces = FakeWorkspaces()
    symphony = WindowsSymphony(
        workflow,
        tracker=tracker,  # type: ignore[arg-type]
        workspace_manager=workspaces,  # type: ignore[arg-type]
    )

    await symphony._ensure_initialized()

    assert workspaces.removed == ["ISSUE-DONE"]


async def test_reconciliation_stops_terminal_run_and_cleans_workspace(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-token")
    monkeypatch.setenv("FSHOWS_SKILLS_REPOSITORY", "https://example.test/skills.git")
    monkeypatch.setenv("FSHOWS_SKILLS_REVISION", "1" * 40)
    workflow_path = tmp_path / "WORKFLOW.md"
    source = (Path(__file__).resolve().parents[1] / "WORKFLOW.md").read_text(
        encoding="utf-8"
    )
    workflow_path.write_text(source, encoding="utf-8")
    workflow = load_workflow(workflow_path)
    issue = {"id": "ISSUE-CANCEL", "identifier": "ISSUE-CANCEL"}
    tracker = FakeTracker(workflow.tracker, [{**issue, "status": "cancelled"}])
    workspaces = FakeWorkspaces()
    symphony = WindowsSymphony(
        workflow,
        tracker=tracker,  # type: ignore[arg-type]
        workspace_manager=workspaces,  # type: ignore[arg-type]
    )
    lease = ClaimLease(issue={**issue, "status": "running"}, token="token", attempt={})
    entry = RunningEntry(
        issue=lease.issue,
        lease=lease,
        workflow=workflow,
        tracker=tracker,  # type: ignore[arg-type]
        workspace_manager=workspaces,  # type: ignore[arg-type]
        started_at=time.monotonic(),
        scheduling_state="ready",
    )
    entry.task = asyncio.create_task(asyncio.sleep(60))  # type: ignore[assignment]
    symphony._running[lease.id] = entry

    await symphony._reconcile_running()

    assert lease.active is False
    assert lease.id not in symphony._running
    assert entry.task.cancelled()
    assert workspaces.removed == [lease.id]


async def test_workflow_hot_reload_keeps_last_known_good_on_invalid_edit(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-token")
    monkeypatch.setenv("FSHOWS_SKILLS_REPOSITORY", "https://example.test/skills.git")
    monkeypatch.setenv("FSHOWS_SKILLS_REVISION", "1" * 40)
    workflow_path = tmp_path / "WORKFLOW.md"
    source = (Path(__file__).resolve().parents[1] / "WORKFLOW.md").read_text(
        encoding="utf-8"
    )
    workflow_path.write_text(source, encoding="utf-8")
    workflow = load_workflow(workflow_path)
    tracker = FakeTracker(workflow.tracker)
    symphony = WindowsSymphony(
        workflow,
        tracker=tracker,  # type: ignore[arg-type]
        workspace_manager=FakeWorkspaces(),  # type: ignore[arg-type]
    )

    workflow_path.write_text(
        source.replace("interval_ms: 5000", "interval_ms: 5100"), encoding="utf-8"
    )
    assert await symphony._reload_workflow_if_changed() is True
    assert symphony.workflow.polling_interval_ms == 5100
    assert symphony.workflow_error is None

    workflow_path.write_text(
        source.replace("interval_ms: 5000", "interval_ms: 5200").replace(
            "required_labels: []", "required_labels: [Backend]"
        ),
        encoding="utf-8",
    )
    symphony._running["sentinel"] = object()  # type: ignore[assignment]
    assert await symphony._reload_workflow_if_changed() is True
    assert symphony.workflow.tracker.required_labels == ("backend",)
    assert tracker.config.required_labels == ("backend",)
    symphony._running.clear()

    workflow_path.write_text("not a workflow", encoding="utf-8")
    assert await symphony._reload_workflow_if_changed() is False
    assert symphony.workflow.polling_interval_ms == 5200
    assert symphony.workflow_error is not None
