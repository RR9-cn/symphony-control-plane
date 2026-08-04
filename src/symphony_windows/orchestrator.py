from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any, Callable

from symphony_windows.codex import CodexAppServer, CodexError, CodexRunResult
from symphony_windows.tracker import (
    ClaimConflict,
    ClaimLease,
    ControlPlaneTracker,
    TrackerError,
)
from symphony_windows.workflow import Workflow, WorkflowError
from symphony_windows.workspace import WorkspaceError, WorkspaceManager


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AttemptOutcome:
    item_id: str
    status: str
    error: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None


CodexFactory = Callable[..., CodexAppServer]


class WindowsSymphony:
    """A Windows-native subset of the language-agnostic Symphony service spec."""

    def __init__(
        self,
        workflow: Workflow,
        *,
        tracker: ControlPlaneTracker | None = None,
        workspace_manager: WorkspaceManager | None = None,
        codex_factory: CodexFactory = CodexAppServer,
    ) -> None:
        self.workflow = workflow
        self.tracker = tracker or ControlPlaneTracker(workflow.tracker)
        self._owns_tracker = tracker is None
        self.workspace_manager = workspace_manager or WorkspaceManager(workflow.workspace)
        self.codex_factory = codex_factory
        self._running: dict[str, asyncio.Task[AttemptOutcome]] = {}
        self._retry_attempts: dict[str, int] = {}
        self._closed = False

    async def __aenter__(self) -> "WindowsSymphony":
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def tick(self) -> list[str]:
        if self._closed:
            raise RuntimeError("WindowsSymphony is closed")
        self._reap_finished()
        await self.tracker.maintenance_tick()
        available = self.workflow.agent.max_concurrent_agents - len(self._running)
        if available <= 0:
            return []
        candidates = await self.tracker.candidates(limit=max(available * 4, available))
        candidates.sort(key=_dispatch_key)
        dispatched: list[str] = []
        for item in candidates:
            if len(dispatched) >= available:
                break
            item_id = str(item.get("id", ""))
            if not item_id or item_id in self._running:
                continue
            try:
                lease = await self.tracker.claim(item)
            except ClaimConflict:
                continue
            task = asyncio.create_task(
                self._run_claimed(lease),
                name=f"windows-symphony-{item_id}",
            )
            self._running[item_id] = task
            dispatched.append(item_id)
            logger.info("dispatched WorkItem %s", item_id)
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
                    logger.exception("poll tick failed")
                await asyncio.sleep(self.workflow.polling_interval_ms / 1000)
        except asyncio.CancelledError:
            raise
        finally:
            await self._stop_running()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._stop_running()
        if self._owns_tracker:
            await self.tracker.close()

    async def _run_claimed(self, lease: ClaimLease) -> AttemptOutcome:
        item_id = lease.id
        workspace = None
        heartbeat_task: asyncio.Task[None] | None = None
        codex_task: asyncio.Task[CodexRunResult] | None = None
        try:
            issue = _normalize_issue(lease.item)
            prepared = await self.workspace_manager.prepare(issue)
            workspace = prepared.path
            await self.workspace_manager.before_run(issue, workspace)
            event = await self.tracker.execute_tool(
                lease,
                "work_item_add_event",
                {"event_type": "agent_started", "payload": {"worker_id": self.workflow.tracker.worker_id}},
            )
            if not event.response["success"]:
                raise TrackerError("failed to record agent_started")

            prompt = self.workflow.render_prompt(issue, self._retry_attempts.get(item_id))
            codex = self.codex_factory(
                self.workflow.codex,
                secret_environment_names=self.workflow.tracker.secret_environment_names,
            )
            codex_task = asyncio.create_task(
                codex.run(workspace, prompt, issue, self.tracker, lease)
            )
            heartbeat_task = asyncio.create_task(self._heartbeat_loop(lease))
            done, _pending = await asyncio.wait(
                {codex_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                error = heartbeat_task.exception()
                if error is not None:
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
                    lease,
                    "codex_turn_completed_without_handoff",
                    retry_delay_seconds=1,
                )
                self._retry_attempts[item_id] = 1
                return AttemptOutcome(
                    item_id=item_id,
                    status="continuation_queued",
                    thread_id=result.thread_id,
                    turn_id=result.turn_id,
                )

            self._retry_attempts.pop(item_id, None)
            return AttemptOutcome(
                item_id=item_id,
                status=result.status,
                thread_id=result.thread_id,
                turn_id=result.turn_id,
            )
        except asyncio.CancelledError:
            if codex_task and not codex_task.done():
                codex_task.cancel()
            if lease.active:
                with contextlib.suppress(TrackerError):
                    await self.tracker.release(
                        lease,
                        "runner_shutdown",
                        retry_delay_seconds=1,
                    )
            raise
        except (CodexError, TrackerError, WorkflowError, WorkspaceError, OSError) as error:
            attempt = self._retry_attempts.get(item_id, 0) + 1
            self._retry_attempts[item_id] = attempt
            if lease.active:
                delay = min(
                    10 * (2 ** min(attempt - 1, 20)),
                    self.workflow.agent.max_retry_backoff_ms // 1000,
                )
                with contextlib.suppress(TrackerError):
                    await self.tracker.release(
                        lease,
                        f"agent_attempt_failed: {error}",
                        retry_delay_seconds=max(1, delay),
                    )
            logger.exception("WorkItem %s failed", item_id)
            return AttemptOutcome(item_id=item_id, status="retry_queued", error=str(error))
        finally:
            if heartbeat_task and not heartbeat_task.done():
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            if workspace is not None:
                with contextlib.suppress(WorkspaceError):
                    await self.workspace_manager.after_run(
                        _normalize_issue(lease.item), workspace
                    )

    async def _heartbeat_loop(self, lease: ClaimLease) -> None:
        interval = max(3.0, self.workflow.tracker.lease_seconds / 3)
        while lease.active:
            await asyncio.sleep(interval)
            if lease.active:
                await self.tracker.heartbeat(lease)

    async def _stop_running(self) -> None:
        tasks = [task for task in self._running.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reap_finished()

    def _reap_finished(self) -> None:
        for item_id, task in list(self._running.items()):
            if not task.done():
                continue
            with contextlib.suppress(asyncio.CancelledError, Exception):
                task.result()
            del self._running[item_id]


def _normalize_issue(item: dict[str, Any]) -> dict[str, Any]:
    issue = dict(item)
    issue["identifier"] = str(item.get("identifier") or item.get("id") or "")
    issue["state"] = str(item.get("status") or "")
    issue["labels"] = [
        f"agent/{item.get('agent_role')}",
        f"stage/{item.get('stage')}",
    ]
    issue["blocked_by"] = item.get("blocked_by", [])
    return issue


def _dispatch_key(item: dict[str, Any]) -> tuple[int, str, str]:
    priority = item.get("priority")
    return (
        priority if isinstance(priority, int) else 2**31 - 1,
        str(item.get("created_at") or ""),
        str(item.get("id") or ""),
    )
