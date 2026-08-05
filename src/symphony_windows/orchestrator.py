from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from symphony_windows.attempt_events import normalize_codex_event
from symphony_windows.codex import CodexAppServer, CodexError, CodexRunResult
from symphony_windows.skill import SkillManager
from symphony_windows.tracker import ClaimConflict, ClaimLease, ControlPlaneTracker, TrackerError
from symphony_windows.workflow import Workflow, WorkflowError
from symphony_windows.workspace import WorkspaceError, WorkspaceManager


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AttemptOutcome:
    issue_id: str
    status: str
    error: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None


CodexFactory = Callable[..., CodexAppServer]


class WindowsSymphony:
    def __init__(
        self,
        workflow: Workflow,
        *,
        tracker: ControlPlaneTracker | None = None,
        workspace_manager: WorkspaceManager | None = None,
        skill_manager: SkillManager | None = None,
        codex_factory: CodexFactory = CodexAppServer,
    ) -> None:
        self.workflow = workflow
        self.tracker = tracker or ControlPlaneTracker(workflow.tracker)
        self._owns_tracker = tracker is None
        self.workspace_manager = workspace_manager or WorkspaceManager(workflow.workspace)
        self.skill_manager = skill_manager or SkillManager(workflow.skill_repository, workflow.agent)
        self.codex_factory = codex_factory
        self._running: dict[str, asyncio.Task[AttemptOutcome]] = {}
        self._retry_attempts: dict[str, int] = {}
        self._closed = False
        self._initialized = False
        self._registered = False
        self._stop_requested = False

    async def __aenter__(self) -> "WindowsSymphony":
        await self._ensure_initialized()
        await self._register_worker()
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def tick(self) -> list[str]:
        if self._closed:
            raise RuntimeError("WindowsSymphony is closed")
        await self._ensure_initialized()
        await self._register_worker()
        self._reap_finished()
        await self._heartbeat_worker()
        if self._stop_requested:
            return []
        await self.tracker.maintenance_tick()
        available = self.workflow.agent.max_concurrent_agents - len(self._running)
        if available <= 0:
            return []
        candidates = await self.tracker.candidates(limit=max(available * 4, available))
        candidates.sort(key=_dispatch_key)
        dispatched: list[str] = []
        for issue in candidates:
            if len(dispatched) >= available:
                break
            issue_id = str(issue.get("id", ""))
            if not issue_id or issue_id in self._running:
                continue
            try:
                lease = await self.tracker.claim(issue, self.skill_manager.snapshot())
            except ClaimConflict:
                continue
            task = asyncio.create_task(self._run_claimed(lease), name=f"windows-symphony-{issue_id}")
            self._running[issue_id] = task
            dispatched.append(issue_id)
            logger.info("issue_id=%s issue_identifier=%s action=dispatch outcome=started", issue_id, issue_id)
        await self._heartbeat_worker()
        return dispatched

    async def run_once(self) -> list[AttemptOutcome]:
        await self.tick()
        tasks = list(self._running.values())
        if not tasks:
            return []
        outcomes = await asyncio.gather(*tasks)
        self._reap_finished()
        return outcomes

    async def serve(self) -> None:
        try:
            while True:
                try:
                    await self.tick()
                except TrackerError:
                    logger.exception("action=poll outcome=failed")
                if self._stop_requested:
                    break
                await asyncio.sleep(self.workflow.polling_interval_ms / 1000)
        finally:
            await self._stop_running()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._stop_running()
        if self._registered:
            with contextlib.suppress(TrackerError):
                await self.tracker.worker_stopped()
        if self._owns_tracker:
            await self.tracker.close()

    async def _run_claimed(self, lease: ClaimLease) -> AttemptOutcome:
        issue_id = lease.id
        workspace = None
        skills_installed = False
        heartbeat_task: asyncio.Task[None] | None = None
        codex_task: asyncio.Task[CodexRunResult] | None = None
        try:
            issue = _normalize_issue(lease.issue)
            workspace = (await self.workspace_manager.prepare(issue)).path
            self.skill_manager.install(workspace)
            skills_installed = True
            await self.workspace_manager.before_run(issue, workspace)
            prompt = self.workflow.render_prompt(issue, lease.attempt.get("attempt_number"))
            if lease.resume_thread_id:
                prompt = "Continue the existing Issue in this resumed Codex thread. Reuse prior work and finish the remaining scope.\n\n" + prompt
            if lease.resume_decisions:
                prompt = (
                    "Resolved human decisions are authoritative. Apply them without asking again:\n"
                    f"<resolved_human_decisions>{json.dumps(lease.resume_decisions, ensure_ascii=False)}</resolved_human_decisions>\n\n"
                    + prompt
                )
            codex = self.codex_factory(
                self.workflow.agent.codex_config(self.workflow.codex),
                secret_environment_names=tuple(sorted({*self.workflow.tracker.secret_environment_names, *self.skill_manager.secret_environment_names()})),
                on_event=lambda message: self._record_codex_event(lease, message),
            )
            codex_task = asyncio.create_task(
                codex.run(
                    workspace, prompt, issue, self.tracker, lease,
                    resume_thread_id=lease.resume_thread_id, max_turns=self.workflow.agent.max_turns,
                )
            )
            heartbeat_task = asyncio.create_task(self._heartbeat_loop(lease))
            done, _ = await asyncio.wait({codex_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED)
            if heartbeat_task in done and (error := heartbeat_task.exception()) is not None:
                codex_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await codex_task
                raise error
            result = await codex_task
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
            if result.status == "turn_completed" and lease.active:
                await self.tracker.release(
                    lease, "continuation_after_max_turns", retry_delay_seconds=1, thread_id=result.thread_id
                )
                return AttemptOutcome(issue_id, "retry_queued", thread_id=result.thread_id, turn_id=result.turn_id)
            self._retry_attempts.pop(issue_id, None)
            return AttemptOutcome(issue_id, result.status, thread_id=result.thread_id, turn_id=result.turn_id)
        except asyncio.CancelledError:
            if codex_task and not codex_task.done():
                codex_task.cancel()
            if lease.active:
                with contextlib.suppress(TrackerError):
                    await self.tracker.release(lease, "runner_shutdown", retry_delay_seconds=1)
            raise
        except (CodexError, TrackerError, WorkflowError, WorkspaceError, OSError) as error:
            attempt = self._retry_attempts.get(issue_id, 0) + 1
            self._retry_attempts[issue_id] = attempt
            if lease.active:
                delay = min(10 * (2 ** min(attempt - 1, 20)), self.workflow.agent.max_retry_backoff_ms // 1000)
                with contextlib.suppress(TrackerError):
                    await self.tracker.release(lease, f"agent_attempt_failed: {error}", retry_delay_seconds=max(1, delay))
            logger.exception("issue_id=%s issue_identifier=%s action=run outcome=retrying", issue_id, issue_id)
            return AttemptOutcome(issue_id, "retry_queued", error=str(error))
        finally:
            if heartbeat_task and not heartbeat_task.done():
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            if workspace is not None:
                if skills_installed:
                    with contextlib.suppress(WorkflowError, OSError):
                        self.skill_manager.restore(workspace)
                with contextlib.suppress(WorkspaceError):
                    await self.workspace_manager.after_run(_normalize_issue(lease.issue), workspace)

    async def _record_codex_event(self, lease: ClaimLease, message: dict[str, Any]) -> None:
        event = normalize_codex_event(message)
        if event is None or not lease.active:
            return
        with contextlib.suppress(TrackerError):
            await self.tracker.add_attempt_event(lease, event)

    async def _heartbeat_loop(self, lease: ClaimLease) -> None:
        interval = max(3.0, self.workflow.tracker.lease_seconds / 3)
        while lease.active:
            await asyncio.sleep(interval)
            if lease.active:
                await self.tracker.heartbeat(lease)

    async def _ensure_initialized(self) -> None:
        if not self._initialized:
            await self.skill_manager.initialize()
            self._initialized = True

    async def _register_worker(self) -> None:
        if not self._registered:
            await self.tracker.register_worker(capacity=self.workflow.agent.max_concurrent_agents)
            self._registered = True

    async def _heartbeat_worker(self) -> None:
        active = [issue_id for issue_id, task in self._running.items() if not task.done()]
        worker = await self.tracker.heartbeat_worker(active_issues=active)
        self._stop_requested = worker.get("stop_requested") is True

    async def _stop_running(self) -> None:
        tasks = [task for task in self._running.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reap_finished()

    def _reap_finished(self) -> None:
        for issue_id, task in list(self._running.items()):
            if task.done():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    task.result()
                del self._running[issue_id]


def _normalize_issue(issue: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(issue)
    normalized["identifier"] = str(issue.get("id", ""))
    return normalized


def _dispatch_key(issue: dict[str, Any]) -> tuple[int, str, str]:
    priority = issue.get("priority")
    rank = priority if isinstance(priority, int) and 0 <= priority <= 4 else 5
    return rank, str(issue.get("created_at", "")), str(issue.get("id", ""))
