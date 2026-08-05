from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from symphony_windows.attempt_events import normalize_codex_event
from symphony_windows.codex import CodexAppServer, CodexError, CodexRunResult
from symphony_windows.tracker import (
    ClaimConflict,
    ClaimLease,
    TrackerAdapter,
    TrackerError,
    create_tracker_adapter,
)
from symphony_windows.workflow import Workflow, WorkflowError, load_workflow
from symphony_windows.workspace import WorkspaceError, WorkspaceManager


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AttemptOutcome:
    issue_id: str
    status: str
    error: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None


@dataclass
class RunningEntry:
    issue: dict[str, Any]
    lease: ClaimLease
    workflow: Workflow
    tracker: TrackerAdapter
    workspace_manager: WorkspaceManager
    started_at: float
    scheduling_state: str
    task: asyncio.Task[AttemptOutcome] | None = None
    last_event_at: float | None = None
    cancel_reason: str | None = None

    @property
    def issue_id(self) -> str:
        return self.lease.id


CodexFactory = Callable[..., CodexAppServer]


class WindowsSymphony:
    def __init__(
        self,
        workflow: Workflow,
        *,
        tracker: TrackerAdapter | None = None,
        workspace_manager: WorkspaceManager | None = None,
        codex_factory: CodexFactory = CodexAppServer,
    ) -> None:
        self.workflow = workflow
        self.tracker = tracker or create_tracker_adapter(workflow.tracker)
        self.workspace_manager = workspace_manager or WorkspaceManager(workflow.workspace)
        self.codex_factory = codex_factory
        self._tracker_injected = tracker is not None
        self._workspace_injected = workspace_manager is not None
        self._owns_tracker = tracker is None
        self._running: dict[str, RunningEntry] = {}
        self._retry_attempts: dict[str, int] = {}
        self._closed = False
        self._initialized = False
        self._registered = False
        self._stop_requested = False
        self._workflow_signature = _file_signature(workflow.path)
        self._last_workflow_error: str | None = None

    async def __aenter__(self) -> "WindowsSymphony":
        await self._ensure_initialized()
        await self._register_worker()
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    @property
    def workflow_error(self) -> str | None:
        return self._last_workflow_error

    async def tick(self) -> list[str]:
        if self._closed:
            raise RuntimeError("WindowsSymphony is closed")
        await self._ensure_initialized()
        await self._register_worker()
        self._reap_finished()
        await self._reconcile_running()
        self._reap_finished()
        await self._heartbeat_worker()
        if self._stop_requested:
            return []
        # Reconciliation deliberately runs before reload/preflight. A broken
        # WORKFLOW.md must block new dispatch without abandoning live runs.
        if not await self._reload_workflow_if_changed():
            return []
        await self._register_worker()
        await self.tracker.maintenance_tick()
        available = self.workflow.agent.max_concurrent_agents - len(self._running)
        if available <= 0:
            return []
        candidates = await self.tracker.fetch_issues_by_states(
            list(self.workflow.tracker.active_states)
        )
        candidates.sort(key=_dispatch_key)
        running_by_state = Counter(
            entry.scheduling_state for entry in self._running.values()
        )
        dispatched: list[str] = []
        for issue in candidates:
            if len(dispatched) >= available:
                break
            issue_id = str(issue.get("id", ""))
            if not issue_dispatch_eligible(issue, self.workflow):
                continue
            if issue_id in self._running:
                continue
            scheduling_state = _state(issue.get("state"))
            if not state_concurrency_available(
                scheduling_state, running_by_state[scheduling_state], self.workflow
            ):
                continue
            try:
                lease = await self.tracker.claim(
                    issue,
                    {
                        **self.workflow.agent.snapshot(),
                        "project_id": self.workflow.tracker.project_id,
                        "workflow_path": str(self.workflow.path),
                    },
                )
            except ClaimConflict:
                continue
            claimed_workflow = load_workflow(
                self.workflow.path,
                source_override=lease.workflow_content or self.workflow.path.read_text(encoding="utf-8"),
            )
            entry = RunningEntry(
                issue=_normalize_issue(lease.issue),
                lease=lease,
                workflow=claimed_workflow,
                tracker=self.tracker,
                workspace_manager=WorkspaceManager(claimed_workflow.workspace),
                started_at=time.monotonic(),
                scheduling_state=scheduling_state,
            )
            entry.task = asyncio.create_task(
                self._run_claimed(entry), name=f"windows-symphony-{issue_id}"
            )
            self._running[issue_id] = entry
            running_by_state[scheduling_state] += 1
            dispatched.append(issue_id)
            logger.info(
                "issue_id=%s issue_identifier=%s action=dispatch outcome=started",
                issue_id,
                entry.issue.get("identifier", issue_id),
            )
        await self._heartbeat_worker()
        return dispatched

    async def run_once(self) -> list[AttemptOutcome]:
        await self.tick()
        tasks = [entry.task for entry in self._running.values() if entry.task]
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
                # Read the interval after every tick so a hot reload changes
                # the next sleep without restarting the process.
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

    async def _run_claimed(self, entry: RunningEntry) -> AttemptOutcome:
        lease = entry.lease
        issue_id = lease.id
        workspace = None
        heartbeat_task: asyncio.Task[None] | None = None
        codex_task: asyncio.Task[CodexRunResult] | None = None
        try:
            issue = entry.issue
            workspace = (await entry.workspace_manager.prepare(issue)).path
            await entry.workspace_manager.before_run(issue, workspace)
            attempt_number = lease.attempt.get("attempt_number")
            attempt = (
                attempt_number - 1
                if isinstance(attempt_number, int) and attempt_number > 1
                else None
            )
            prompt = entry.workflow.render_prompt(issue, attempt)
            if lease.resume_thread_id:
                prompt = (
                    "Continue the existing Issue in this resumed Codex thread. "
                    "Reuse prior work and finish the remaining scope.\n\n" + prompt
                )
            if lease.resume_decisions:
                prompt = (
                    "Resolved human decisions are authoritative. Apply them without asking again:\n"
                    f"<resolved_human_decisions>{json.dumps(lease.resume_decisions, ensure_ascii=False)}</resolved_human_decisions>\n\n"
                    + prompt
                )
            codex = self.codex_factory(
                entry.workflow.agent.codex_config(entry.workflow.codex),
                secret_environment_names=tuple(
                    sorted(
                        {
                            *entry.workflow.tracker.secret_environment_names,
                        }
                    )
                ),
                on_event=lambda message: self._record_codex_event(entry, message),
            )
            codex_task = asyncio.create_task(
                codex.run(
                    workspace,
                    prompt,
                    issue,
                    entry.tracker,
                    lease,
                    resume_thread_id=lease.resume_thread_id,
                    max_turns=entry.workflow.agent.max_turns,
                )
            )
            heartbeat_task = asyncio.create_task(self._heartbeat_loop(entry))
            done, _ = await asyncio.wait(
                {codex_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED
            )
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
                await entry.tracker.release(
                    lease,
                    "continuation_after_max_turns",
                    retry_delay_seconds=1,
                    thread_id=result.thread_id,
                )
                return AttemptOutcome(
                    issue_id,
                    "retry_queued",
                    thread_id=result.thread_id,
                    turn_id=result.turn_id,
                )
            self._retry_attempts.pop(issue_id, None)
            return AttemptOutcome(
                issue_id,
                result.status,
                thread_id=result.thread_id,
                turn_id=result.turn_id,
            )
        except asyncio.CancelledError:
            if codex_task and not codex_task.done():
                codex_task.cancel()
            if lease.active:
                reason = entry.cancel_reason or "runner_shutdown"
                delay = self._failure_retry_delay(issue_id, entry.workflow) if reason == "stall_timeout" else 1
                with contextlib.suppress(TrackerError):
                    await entry.tracker.release(
                        lease, reason, retry_delay_seconds=delay
                    )
            raise
        except (CodexError, TrackerError, WorkflowError, WorkspaceError, OSError) as error:
            delay = self._failure_retry_delay(issue_id, entry.workflow)
            if lease.active:
                with contextlib.suppress(TrackerError):
                    await entry.tracker.release(
                        lease,
                        f"agent_attempt_failed: {error}",
                        retry_delay_seconds=delay,
                    )
            logger.exception(
                "issue_id=%s issue_identifier=%s action=run outcome=retrying",
                issue_id,
                entry.issue.get("identifier", issue_id),
            )
            return AttemptOutcome(issue_id, "retry_queued", error=str(error))
        finally:
            if heartbeat_task and not heartbeat_task.done():
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            if workspace is not None:
                with contextlib.suppress(WorkspaceError):
                    await entry.workspace_manager.after_run(
                        _normalize_issue(lease.issue), workspace
                    )

    async def _record_codex_event(
        self, entry: RunningEntry, message: dict[str, Any]
    ) -> None:
        entry.last_event_at = time.monotonic()
        event = normalize_codex_event(message)
        if event is None or not entry.lease.active:
            return
        with contextlib.suppress(TrackerError):
            await entry.tracker.add_attempt_event(entry.lease, event)

    async def _heartbeat_loop(self, entry: RunningEntry) -> None:
        interval = max(3.0, entry.workflow.tracker.lease_seconds / 3)
        while entry.lease.active:
            await asyncio.sleep(interval)
            if entry.lease.active:
                await entry.tracker.heartbeat(entry.lease)

    async def _reconcile_running(self) -> None:
        if not self._running:
            return
        now = time.monotonic()
        for entry in list(self._running.values()):
            timeout_ms = entry.workflow.codex.stall_timeout_ms
            last_activity = entry.last_event_at or entry.started_at
            if timeout_ms > 0 and (now - last_activity) * 1000 > timeout_ms:
                logger.warning(
                    "issue_id=%s issue_identifier=%s action=reconcile outcome=stalled",
                    entry.issue_id,
                    entry.issue.get("identifier", entry.issue_id),
                )
                await self._cancel_entry(entry, "stall_timeout", cleanup=False)

        grouped: dict[int, tuple[TrackerAdapter, list[RunningEntry]]] = {}
        for entry in self._running.values():
            key = id(entry.tracker)
            grouped.setdefault(key, (entry.tracker, []))[1].append(entry)
        for tracker, entries in grouped.values():
            try:
                refreshed = await tracker.fetch_issues_by_ids(
                    [entry.issue_id for entry in entries]
                )
            except TrackerError:
                logger.exception("action=reconcile outcome=tracker_refresh_failed")
                continue
            by_id = {str(issue.get("id")): issue for issue in refreshed}
            for entry in list(entries):
                if entry.issue_id not in self._running:
                    continue
                issue = by_id.get(entry.issue_id)
                if issue is None:
                    entry.lease.active = False
                    await self._cancel_entry(entry, "issue_missing", cleanup=False)
                    continue
                entry.issue = _normalize_issue(issue)
                entry.lease.issue = issue
                state = _state(issue.get("state"))
                effective_workflow = (
                    self.workflow if entry.tracker is self.tracker else entry.workflow
                )
                if state in effective_workflow.tracker.terminal_states:
                    entry.lease.active = False
                    await self._cancel_entry(entry, "issue_terminal", cleanup=True)
                elif (
                    state not in effective_workflow.tracker.active_states
                    or not issue_routable(issue, effective_workflow)
                ):
                    entry.lease.active = False
                    await self._cancel_entry(
                        entry, "issue_no_longer_routable", cleanup=False
                    )

    async def _cancel_entry(
        self, entry: RunningEntry, reason: str, *, cleanup: bool
    ) -> None:
        task = entry.task
        entry.cancel_reason = reason
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._running.pop(entry.issue_id, None)
        if cleanup:
            with contextlib.suppress(WorkspaceError):
                removed = await entry.workspace_manager.remove(entry.issue)
                logger.info(
                    "issue_id=%s issue_identifier=%s action=workspace_cleanup outcome=%s",
                    entry.issue_id,
                    entry.issue.get("identifier", entry.issue_id),
                    "removed" if removed else "absent",
                )

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        try:
            terminal = await self.tracker.fetch_issues_by_states(
                list(self.workflow.tracker.terminal_states)
            )
        except TrackerError:
            logger.warning("action=startup_cleanup outcome=tracker_fetch_failed", exc_info=True)
        else:
            for issue in terminal:
                with contextlib.suppress(WorkspaceError):
                    await self.workspace_manager.remove(_normalize_issue(issue))
        self._initialized = True

    async def _reload_workflow_if_changed(self) -> bool:
        signature = _file_signature(self.workflow.path)
        if signature == self._workflow_signature and self._last_workflow_error is None:
            return True
        candidate_tracker: TrackerAdapter | None = None
        created_tracker = False
        update_tracker_config = False
        try:
            _require_committed_project_contract(self.workflow.path)
            candidate = load_workflow(self.workflow.path)
            provider_changed = _tracker_provider_identity(
                candidate.tracker
            ) != _tracker_provider_identity(self.workflow.tracker)
            if provider_changed and self._running:
                raise WorkflowError(
                    "tracker provider changed while Issues are running; reload is deferred until they finish"
                )
            candidate_tracker = self.tracker
            if candidate.tracker != self.workflow.tracker:
                if provider_changed:
                    if self._tracker_injected:
                        raise WorkflowError("injected tracker cannot be replaced by hot reload")
                    candidate_tracker = create_tracker_adapter(candidate.tracker)
                    created_tracker = True
                    await candidate_tracker.register_worker(
                        capacity=candidate.agent.max_concurrent_agents
                    )
                else:
                    update_tracker_config = True
            candidate_workspace_manager = self.workspace_manager
            if not self._workspace_injected:
                candidate_workspace_manager = WorkspaceManager(candidate.workspace)
        except (OSError, TrackerError, WorkflowError) as error:
            if created_tracker and candidate_tracker is not None:
                with contextlib.suppress(TrackerError):
                    await candidate_tracker.worker_stopped()
                await candidate_tracker.close()
            self._set_workflow_error(str(error))
            return False

        previous_tracker = self.tracker
        previous_capacity = self.workflow.agent.max_concurrent_agents
        assert candidate_tracker is not None
        tracker_changed = candidate_tracker is not previous_tracker
        self.workflow = candidate
        self.tracker = candidate_tracker
        if update_tracker_config:
            self.tracker.config = candidate.tracker
        self.workspace_manager = candidate_workspace_manager
        self._workflow_signature = signature
        self._last_workflow_error = None
        if tracker_changed:
            with contextlib.suppress(TrackerError):
                await previous_tracker.worker_stopped()
            await previous_tracker.close()
            self._registered = True
        elif candidate.agent.max_concurrent_agents != previous_capacity:
            self._registered = False
        logger.info(
            "action=workflow_reload outcome=applied file=%s max_concurrent_agents=%s polling_interval_ms=%s",
            candidate.path,
            candidate.agent.max_concurrent_agents,
            candidate.polling_interval_ms,
        )
        return True

    def _set_workflow_error(self, message: str) -> None:
        if message != self._last_workflow_error:
            logger.error(
                "action=workflow_reload outcome=rejected error=%s; keeping_last_known_good=true",
                message,
            )
        self._last_workflow_error = message

    async def _register_worker(self) -> None:
        if not self._registered:
            await self.tracker.register_worker(
                capacity=self.workflow.agent.max_concurrent_agents
            )
            self._registered = True

    async def _heartbeat_worker(self) -> None:
        active = [
            issue_id
            for issue_id, entry in self._running.items()
            if entry.task is not None and not entry.task.done()
        ]
        worker = await self.tracker.heartbeat_worker(active_issues=active)
        self._stop_requested = worker.get("stop_requested") is True

    async def _stop_running(self) -> None:
        entries = list(self._running.values())
        for entry in entries:
            entry.cancel_reason = "runner_shutdown"
            if entry.task is not None and not entry.task.done():
                entry.task.cancel()
        tasks = [entry.task for entry in entries if entry.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._running.clear()

    def _reap_finished(self) -> None:
        for issue_id, entry in list(self._running.items()):
            task = entry.task
            if task is not None and task.done():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    task.result()
                del self._running[issue_id]

    def _failure_retry_delay(self, issue_id: str, workflow: Workflow) -> int:
        attempt = self._retry_attempts.get(issue_id, 0) + 1
        self._retry_attempts[issue_id] = attempt
        return max(
            1,
            min(
                10 * (2 ** min(attempt - 1, 20)),
                workflow.agent.max_retry_backoff_ms // 1000,
            ),
        )


def _normalize_issue(issue: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(issue)
    normalized.setdefault("identifier", str(issue.get("id", "")))
    return normalized


def _dispatch_key(issue: dict[str, Any]) -> tuple[int, str, str]:
    priority = issue.get("priority")
    rank = priority if isinstance(priority, int) and 1 <= priority <= 4 else 5
    return rank, str(issue.get("created_at", "")), str(
        issue.get("identifier") or issue.get("id", "")
    )


def issue_routable(issue: dict[str, Any], workflow: Workflow) -> bool:
    labels = {
        str(label).strip().lower()
        for label in issue.get("labels", [])
        if isinstance(label, str) and label.strip()
    }
    return (
        issue.get("dispatchable") is True
        and set(workflow.tracker.required_labels) <= labels
    )


def issue_dispatch_eligible(issue: dict[str, Any], workflow: Workflow) -> bool:
    required = ("id", "identifier", "title", "state")
    if any(
        not isinstance(issue.get(field), str) or not issue[field].strip()
        for field in required
    ):
        return False
    state = _state(issue["state"])
    return (
        state in workflow.tracker.active_states
        and state not in workflow.tracker.terminal_states
        and issue_routable(issue, workflow)
    )


def state_concurrency_available(
    state: str, running_count: int, workflow: Workflow
) -> bool:
    normalized = _state(state)
    limit = workflow.agent.max_concurrent_agents_by_state.get(
        normalized, workflow.agent.max_concurrent_agents
    )
    return running_count < limit


def _state(value: object) -> str:
    return str(value or "").strip().lower()


def _tracker_provider_identity(config: Any) -> tuple[object, ...]:
    return (
        config.kind,
        config.endpoint,
        config.token,
        config.worker_id,
        config.request_timeout_seconds,
        config.secret_environment_names,
    )


def _require_committed_project_contract(workflow_path: Path) -> None:
    repository_value = os.getenv("SYMPHONY_PROJECT_REPOSITORY")
    if not repository_value:
        return
    repository = Path(repository_value).resolve()
    try:
        relative_workflow = workflow_path.resolve().relative_to(repository).as_posix()
    except ValueError as error:
        raise WorkflowError("project WORKFLOW.md escapes the registered repository") from error
    result = subprocess.run(
        ["git", "-C", str(repository), "status", "--porcelain", "--", relative_workflow, "AGENTS.md", ".codex/skills"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise WorkflowError(result.stderr.strip() or "cannot validate project contract revision")
    if result.stdout.strip():
        raise WorkflowError("WORKFLOW.md, AGENTS.md and .codex/skills must be committed before hot reload")


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size
