from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.config import Settings
from control_plane.delivery import DeliveryError, IssueDeliveryManager
from control_plane.errors import ClaimError, ConflictError, InvalidTransitionError, NotFoundError
from control_plane.models import (
    AgentAttempt,
    AgentAttemptEvent,
    HumanDecision,
    Issue,
    IssueArtifact,
    IssueEvent,
    Worker,
    utc_now,
)
from control_plane.schemas import (
    AgentAttemptEventCreate,
    AgentAttemptEventView,
    AgentAttemptView,
    AgentRuntimeView,
    ArtifactCreate,
    ArtifactView,
    AttemptContextUpdate,
    ClaimRequest,
    ClaimResult,
    ClaimView,
    DecisionCommand,
    DecisionView,
    EventCreate,
    EventView,
    HeartbeatRequest,
    IssueCreate,
    IssueDeliveryCommand,
    IssuePatch,
    IssueView,
    MaintenanceResult,
    ReleaseRequest,
    StatusTransitionRequest,
    WorkerHeartbeat,
    WorkerRegistration,
    WorkerView,
)


ACTIVE_STATUSES = {"ready", "running", "retry_queued", "needs_human", "blocked", "reviewing", "awaiting_publish", "pr_open"}
RUNTIME_STATE = {
    "ready": "ready",
    "running": "running",
    "retry_queued": "retrying",
    "needs_human": "waiting_human",
    "blocked": "blocked",
    "reviewing": "reviewing",
    "awaiting_publish": "awaiting_publish",
    "pr_open": "pr_open",
    "done": "done",
    "cancelled": "cancelled",
}
TRANSITIONS = {
    ("running", "reviewing", "agent_completed"),
    ("running", "needs_human", "human_input_requested"),
    ("running", "blocked", "agent_blocked"),
    ("running", "cancelled", "cancelled"),
    ("reviewing", "ready", "result_rejected"),
    ("blocked", "ready", "retry_requested"),
    ("ready", "cancelled", "cancelled"),
    ("retry_queued", "cancelled", "cancelled"),
    ("needs_human", "cancelled", "cancelled"),
    ("blocked", "cancelled", "cancelled"),
    ("reviewing", "cancelled", "cancelled"),
}


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ControlPlaneService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or Settings()
        root = Path(self.settings.issue_workspace_root)
        if not root.is_absolute():
            root = Path(self.settings.managed_runner_workflow).resolve().parent / root
        self.delivery = IssueDeliveryManager(root)

    async def create_issue(self, command: IssueCreate) -> IssueView:
        async with self.session.begin():
            if await self.session.get(Issue, command.id):
                raise ConflictError(f"issue already exists: {command.id}")
            issue = Issue(
                **command.model_dump(), status="ready", version=1,
            )
            self.session.add(issue)
            await self.session.flush()
            self._add_event(issue.id, "created", "user", "manual-intake", None, "ready", {})
        return await self.get_issue(command.id)

    async def list_issues(self, statuses: list[str] | None = None, issue_ids: list[str] | None = None) -> list[IssueView]:
        statement = select(Issue)
        if statuses:
            statement = statement.where(Issue.status.in_(statuses))
        if issue_ids:
            statement = statement.where(Issue.id.in_(issue_ids))
        rows = (await self.session.scalars(statement.order_by(Issue.priority, Issue.created_at))).all()
        return [await self._issue_view(row) for row in rows]

    async def candidates(self, limit: int = 100) -> list[IssueView]:
        rows = (
            await self.session.scalars(
                select(Issue).where(Issue.status == "ready").order_by(Issue.priority, Issue.created_at).limit(limit)
            )
        ).all()
        return [await self._issue_view(row) for row in rows]

    async def get_issue(self, issue_id: str) -> IssueView:
        issue = await self.session.get(Issue, issue_id)
        if issue is None:
            raise NotFoundError(f"issue not found: {issue_id}")
        return await self._issue_view(issue)

    async def patch_issue(self, issue_id: str, command: IssuePatch) -> IssueView:
        async with self.session.begin():
            issue = await self._require_issue(issue_id)
            if issue.version != command.expected_version:
                raise ConflictError("issue version changed")
            if issue.status not in {"ready", "blocked", "needs_human", "reviewing"}:
                raise ConflictError(f"issue cannot be edited while status={issue.status}")
            values = command.model_dump(exclude={"expected_version"}, exclude_none=True)
            for key, value in values.items():
                setattr(issue, key, value)
            issue.version += 1
            issue.updated_at = utc_now()
            self._add_event(issue_id, "updated", "user", None, issue.status, issue.status, {"fields": sorted(values)})
        return await self.get_issue(issue_id)

    async def claim(self, issue_id: str, command: ClaimRequest) -> ClaimResult:
        token = secrets.token_urlsafe(32)
        now = utc_now()
        expires = now + timedelta(seconds=command.lease_seconds)
        async with self.session.begin():
            result = await self.session.execute(
                update(Issue)
                .where(Issue.id == issue_id, Issue.status == "ready", Issue.version == command.expected_version)
                .values(
                    status="running", version=Issue.version + 1, claim_worker_id=command.worker_id,
                    claim_token_hash=_token_hash(token), claim_expires_at=expires, retry_at=None,
                    blocker=None, updated_at=now,
                )
            )
            if result.rowcount != 1:
                raise ClaimError("issue is not claimable or its version changed")
            number = int(await self.session.scalar(select(func.count()).select_from(AgentAttempt).where(AgentAttempt.issue_id == issue_id)) or 0) + 1
            attempt = AgentAttempt(
                issue_id=issue_id, attempt_number=number, worker_id=command.worker_id,
                config_snapshot=command.agent.config, status="running",
            )
            self.session.add(attempt)
            self._add_event(issue_id, "claimed", "worker", command.worker_id, "ready", "running", {"attempt": number})
            await self.session.flush()
        latest_previous = await self.session.scalar(
            select(AgentAttempt).where(AgentAttempt.issue_id == issue_id, AgentAttempt.id != attempt.id, AgentAttempt.thread_id.is_not(None)).order_by(AgentAttempt.attempt_number.desc()).limit(1)
        )
        decisions = (
            await self.session.scalars(
                select(HumanDecision).where(HumanDecision.issue_id == issue_id, HumanDecision.status == "resolved").order_by(HumanDecision.created_at)
            )
        ).all()
        return ClaimResult(
            issue=await self.get_issue(issue_id), claim_token=token,
            attempt=AgentAttemptView.model_validate(attempt),
            resume_thread_id=latest_previous.thread_id if latest_previous else None,
            resume_decisions=[DecisionView.model_validate(row) for row in decisions],
            continuation_turn_count=latest_previous.turn_count if latest_previous else 0,
        )

    async def heartbeat(self, issue_id: str, command: HeartbeatRequest) -> IssueView:
        async with self.session.begin():
            issue = await self._require_running_claim(issue_id, command.claim_token)
            issue.claim_expires_at = utc_now() + timedelta(seconds=command.lease_seconds)
            issue.updated_at = utc_now()
        return await self.get_issue(issue_id)

    async def release(self, issue_id: str, command: ReleaseRequest) -> IssueView:
        delay = self.settings.default_retry_delay_seconds if command.retry_delay_seconds is None else command.retry_delay_seconds
        async with self.session.begin():
            issue = await self._require_running_claim(issue_id, command.claim_token)
            await self._finish_attempt(issue_id, "retry_queued", thread_id=command.thread_id)
            actor = issue.claim_worker_id
            self._clear_claim(issue)
            issue.status = "retry_queued"
            issue.retry_at = utc_now() + timedelta(seconds=delay)
            issue.version += 1
            issue.updated_at = utc_now()
            self._add_event(issue_id, "retry_scheduled", "worker", actor, "running", "retry_queued", {"reason": command.reason, "delay_seconds": delay})
        return await self.get_issue(issue_id)

    async def transition(self, issue_id: str, command: StatusTransitionRequest) -> IssueView:
        async with self.session.begin():
            issue = await self._require_issue(issue_id)
            from_status = issue.status
            if (from_status, command.to_status, command.event) not in TRANSITIONS:
                raise InvalidTransitionError(f"transition {from_status} -> {command.to_status} with {command.event} is not allowed")
            if from_status == "running":
                human_cancellation = (
                    command.to_status == "cancelled"
                    and command.event == "cancelled"
                    and command.actor_type == "human"
                )
                if not human_cancellation:
                    await self._require_running_claim(issue_id, command.claim_token)
                await self._finish_attempt(issue_id, command.to_status)
                actor_id = command.actor_id or issue.claim_worker_id
                self._clear_claim(issue)
            else:
                actor_id = command.actor_id
            issue.status = command.to_status
            issue.version += 1
            issue.updated_at = utc_now()
            if command.to_status != "blocked":
                issue.blocker = None
            if command.to_status == "blocked":
                issue.blocker = command.payload.get("blocker") or command.payload
            self._add_event(issue_id, command.event, command.actor_type, actor_id, from_status, command.to_status, command.payload)
        return await self.get_issue(issue_id)

    async def deliver_issue(self, issue_id: str, command: IssueDeliveryCommand) -> IssueView:
        issue = await self._require_issue(issue_id)
        if issue.version != command.expected_version:
            raise ConflictError("issue version changed")
        repository = dict(issue.repository)
        status = issue.status
        title = issue.title
        head_branch = issue.head_branch
        local_commit = issue.local_commit
        pull_request_url = issue.pull_request
        # SQLAlchemy starts a transaction for the read above. End that read
        # transaction before invoking git/GitHub, then use a fresh transaction
        # to compare-and-set the delivery state after the side effect succeeds.
        await self.session.rollback()
        if command.action == "approve_result":
            if status != "reviewing":
                raise ConflictError(f"issue cannot approve result while status={status}")
            try:
                branch, commit = await self.delivery.prepare_local_commit(issue_id, title)
            except DeliveryError as error:
                raise ConflictError(str(error)) from error
            async with self.session.begin():
                current = await self._require_issue(issue_id)
                if current.status != "reviewing" or current.version != command.expected_version:
                    raise ConflictError("issue changed while preparing delivery")
                current.status = "awaiting_publish"
                current.head_branch = branch
                current.local_commit = commit
                current.version += 1
                current.updated_at = utc_now()
                self._add_event(issue_id, "result_approved", "human", "control-plane-ui", "reviewing", "awaiting_publish", {"branch": branch, "commit": commit})
        elif command.action == "authorize_publish":
            if status != "awaiting_publish" or not head_branch or not local_commit:
                raise ConflictError(f"issue cannot publish while status={status}")
            try:
                pull_request = await self.delivery.publish(
                    issue_id, repository_url=str(repository["url"]), base_branch=str(repository["base_branch"]),
                    branch=head_branch, commit=local_commit, title=title,
                    body=f"Generated by Fshows Symphony for {issue_id} after the coding agent completed the issue.",
                )
            except DeliveryError as error:
                raise ConflictError(str(error)) from error
            async with self.session.begin():
                current = await self._require_issue(issue_id)
                if current.status != "awaiting_publish" or current.version != command.expected_version:
                    raise ConflictError("issue changed while publishing")
                current.status = "pr_open"
                current.pull_request = pull_request
                current.version += 1
                current.updated_at = utc_now()
                self._add_event(issue_id, "pull_request_created", "human", "control-plane-ui", "awaiting_publish", "pr_open", {"pull_request": pull_request})
        else:
            if status != "pr_open" or not pull_request_url:
                raise ConflictError(f"issue cannot confirm merge while status={status}")
            try:
                await self.delivery.verify_merged(str(repository["url"]), pull_request_url)
            except DeliveryError as error:
                raise ConflictError(str(error)) from error
            async with self.session.begin():
                current = await self._require_issue(issue_id)
                if current.status != "pr_open" or current.version != command.expected_version:
                    raise ConflictError("issue changed while confirming merge")
                current.status = "done"
                current.merged_at = utc_now()
                current.version += 1
                current.updated_at = utc_now()
                self._add_event(issue_id, "pull_request_merged", "human", "control-plane-ui", "pr_open", "done", {"pull_request": current.pull_request})
        return await self.get_issue(issue_id)

    async def update_attempt_context(self, issue_id: str, command: AttemptContextUpdate) -> AgentAttemptView:
        async with self.session.begin():
            await self._require_running_claim(issue_id, command.claim_token)
            attempt = await self._active_attempt(issue_id)
            attempt.thread_id = command.thread_id
            if command.turn_id is not None:
                attempt.turn_id = command.turn_id
            if command.turn_count is not None:
                attempt.turn_count = command.turn_count
        return AgentAttemptView.model_validate(attempt)

    async def list_attempts(self, issue_id: str) -> list[AgentAttemptView]:
        await self._require_issue(issue_id)
        rows = (await self.session.scalars(select(AgentAttempt).where(AgentAttempt.issue_id == issue_id).order_by(AgentAttempt.attempt_number.desc()))).all()
        return [AgentAttemptView.model_validate(row) for row in rows]

    async def add_attempt_event(self, issue_id: str, attempt_id: str, command: AgentAttemptEventCreate) -> AgentAttemptEventView:
        async with self.session.begin():
            await self._require_running_claim(issue_id, command.claim_token)
            attempt = await self.session.get(AgentAttempt, attempt_id)
            if attempt is None or attempt.issue_id != issue_id:
                raise NotFoundError("attempt not found")
            sequence = int(await self.session.scalar(select(func.max(AgentAttemptEvent.sequence)).where(AgentAttemptEvent.attempt_id == attempt_id)) or 0) + 1
            row = AgentAttemptEvent(
                attempt_id=attempt_id, issue_id=issue_id, sequence=sequence,
                **command.model_dump(exclude={"claim_token"}),
            )
            self.session.add(row)
            await self.session.flush()
        return AgentAttemptEventView.model_validate(row)

    async def list_attempt_events(self, issue_id: str, attempt_id: str, *, after_sequence: int = 0, limit: int = 500) -> list[AgentAttemptEventView]:
        rows = (
            await self.session.scalars(
                select(AgentAttemptEvent).where(
                    AgentAttemptEvent.issue_id == issue_id, AgentAttemptEvent.attempt_id == attempt_id,
                    AgentAttemptEvent.sequence > after_sequence,
                ).order_by(AgentAttemptEvent.sequence).limit(limit)
            )
        ).all()
        return [AgentAttemptEventView.model_validate(row) for row in rows]

    async def add_event(self, issue_id: str, command: EventCreate) -> EventView:
        async with self.session.begin():
            issue = await self._require_issue(issue_id)
            if issue.status == "running":
                await self._require_running_claim(issue_id, command.claim_token)
            row = self._add_event(issue_id, command.event_type, command.actor_type, command.actor_id, issue.status, issue.status, command.payload)
            await self.session.flush()
        return EventView.model_validate(row)

    async def list_events(self, issue_id: str) -> list[EventView]:
        await self._require_issue(issue_id)
        rows = (await self.session.scalars(select(IssueEvent).where(IssueEvent.issue_id == issue_id).order_by(IssueEvent.created_at))).all()
        return [EventView.model_validate(row) for row in rows]

    async def add_artifact(self, issue_id: str, command: ArtifactCreate) -> ArtifactView:
        path = PurePosixPath(command.path)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or "\\" in command.path:
            raise ConflictError("artifact path must be a safe relative POSIX path")
        async with self.session.begin():
            issue = await self._require_issue(issue_id)
            if issue.status == "running":
                await self._require_running_claim(issue_id, command.claim_token)
            row = IssueArtifact(issue_id=issue_id, **command.model_dump(exclude={"claim_token"}))
            self.session.add(row)
            self._add_event(issue_id, "artifact_registered", "agent", issue.claim_worker_id, issue.status, issue.status, {"path": command.path})
            await self.session.flush()
        return ArtifactView.model_validate(row)

    async def decision(self, issue_id: str, command: DecisionCommand) -> DecisionView:
        if command.action == "request":
            async with self.session.begin():
                issue = await self._require_running_claim(issue_id, command.claim_token)
                row = HumanDecision(
                    issue_id=issue_id, question=command.question or "", options=command.options,
                    requested_by=command.actor_id or issue.claim_worker_id,
                )
                self.session.add(row)
                await self._finish_attempt(issue_id, "needs_human", thread_id=command.thread_id)
                actor = issue.claim_worker_id
                self._clear_claim(issue)
                issue.status = "needs_human"
                issue.version += 1
                issue.updated_at = utc_now()
                self._add_event(issue_id, "human_input_requested", "agent", actor, "running", "needs_human", {"question": command.question, "options": command.options})
                await self.session.flush()
            return DecisionView.model_validate(row)
        async with self.session.begin():
            issue = await self._require_issue(issue_id)
            if issue.status != "needs_human":
                raise InvalidTransitionError("issue is not waiting for human input")
            row = await self.session.get(HumanDecision, command.decision_id)
            if row is None or row.issue_id != issue_id or row.status != "open":
                raise NotFoundError("open decision not found")
            row.status = "resolved"
            row.response = command.response
            row.resolved_by = command.actor_id
            row.resolved_at = utc_now()
            issue.status = "ready"
            issue.version += 1
            issue.updated_at = utc_now()
            self._add_event(issue_id, "human_input_resolved", "human", command.actor_id, "needs_human", "ready", {"decision_id": row.id})
        return DecisionView.model_validate(row)

    async def list_decisions(self, issue_id: str) -> list[DecisionView]:
        rows = (await self.session.scalars(select(HumanDecision).where(HumanDecision.issue_id == issue_id).order_by(HumanDecision.created_at))).all()
        return [DecisionView.model_validate(row) for row in rows]

    async def register_worker(self, command: WorkerRegistration) -> WorkerView:
        async with self.session.begin():
            worker = await self.session.get(Worker, command.worker_id)
            values = command.model_dump()
            values["id"] = values.pop("worker_id")
            if worker is None:
                worker = Worker(**values, state="starting")
                self.session.add(worker)
            else:
                for key, value in values.items():
                    setattr(worker, key, value)
                worker.state = "starting"
                worker.stop_requested = False
                worker.stopped_at = None
                worker.last_seen_at = utc_now()
        return WorkerView.model_validate(worker)

    async def heartbeat_worker(self, worker_id: str, command: WorkerHeartbeat) -> WorkerView:
        async with self.session.begin():
            worker = await self.session.get(Worker, worker_id)
            if worker is None:
                raise NotFoundError(f"worker not found: {worker_id}")
            worker.state = command.state
            worker.active_issues = command.active_issues
            worker.last_seen_at = utc_now()
        return WorkerView.model_validate(worker)

    async def list_workers(self) -> list[WorkerView]:
        rows = (await self.session.scalars(select(Worker).order_by(Worker.started_at.desc()))).all()
        return [WorkerView.model_validate(row) for row in rows]

    async def request_worker_stop(self, worker_id: str) -> WorkerView:
        async with self.session.begin():
            worker = await self.session.get(Worker, worker_id)
            if worker is None:
                raise NotFoundError(f"worker not found: {worker_id}")
            worker.stop_requested = True
            worker.state = "stopping"
        return WorkerView.model_validate(worker)

    async def stop_worker(self, worker_id: str) -> WorkerView:
        async with self.session.begin():
            worker = await self.session.get(Worker, worker_id)
            if worker is None:
                raise NotFoundError(f"worker not found: {worker_id}")
            worker.state = "stopped"
            worker.active_issues = []
            worker.stopped_at = utc_now()
            worker.last_seen_at = utc_now()
        return WorkerView.model_validate(worker)

    async def list_agent_runtimes(self) -> list[AgentRuntimeView]:
        issues = (await self.session.scalars(select(Issue).order_by(Issue.updated_at.desc()))).all()
        result: list[AgentRuntimeView] = []
        for issue in issues:
            attempt = await self.session.scalar(select(AgentAttempt).where(AgentAttempt.issue_id == issue.id).order_by(AgentAttempt.attempt_number.desc()).limit(1))
            result.append(
                AgentRuntimeView(
                    issue_id=issue.id, title=issue.title, state=RUNTIME_STATE[issue.status],
                    worker_id=issue.claim_worker_id, attempt_id=attempt.id if attempt else None,
                    attempt_number=attempt.attempt_number if attempt else None,
                    thread_id=attempt.thread_id if attempt else None, turn_id=attempt.turn_id if attempt else None,
                    turn_count=attempt.turn_count if attempt else 0,
                    started_at=attempt.started_at if attempt else None, updated_at=issue.updated_at,
                )
            )
        return result

    async def maintenance_tick(self) -> MaintenanceResult:
        now = utc_now()
        expired = 0
        readied = 0
        async with self.session.begin():
            expired_rows = (await self.session.scalars(select(Issue).where(Issue.status == "running", Issue.claim_expires_at < now))).all()
            for issue in expired_rows:
                await self._finish_attempt(issue.id, "retry_queued")
                actor = issue.claim_worker_id
                self._clear_claim(issue)
                issue.status = "retry_queued"
                issue.retry_at = now + timedelta(seconds=self.settings.default_retry_delay_seconds)
                issue.version += 1
                issue.updated_at = now
                self._add_event(issue.id, "lease_expired", "system", actor, "running", "retry_queued", {})
                expired += 1
            ready_rows = (await self.session.scalars(select(Issue).where(Issue.status == "retry_queued", Issue.retry_at <= now))).all()
            for issue in ready_rows:
                issue.status = "ready"
                issue.retry_at = None
                issue.version += 1
                issue.updated_at = now
                self._add_event(issue.id, "retry_due", "system", None, "retry_queued", "ready", {})
                readied += 1
        return MaintenanceResult(expired=expired, readied=readied)

    async def _issue_view(self, issue: Issue) -> IssueView:
        artifacts = (await self.session.scalars(select(IssueArtifact).where(IssueArtifact.issue_id == issue.id).order_by(IssueArtifact.created_at))).all()
        return IssueView(
            id=issue.id, title=issue.title, description=issue.description, status=issue.status,
            priority=issue.priority, version=issue.version, repository=issue.repository,
            acceptance_criteria=issue.acceptance_criteria, blocker=issue.blocker,
            claim=ClaimView(worker_id=issue.claim_worker_id, expires_at=issue.claim_expires_at),
            retry_at=issue.retry_at, head_branch=issue.head_branch, local_commit=issue.local_commit,
            pull_request=issue.pull_request, merged_at=issue.merged_at,
            artifacts=[ArtifactView.model_validate(row) for row in artifacts],
            created_at=issue.created_at, updated_at=issue.updated_at,
        )

    async def _require_issue(self, issue_id: str) -> Issue:
        issue = await self.session.get(Issue, issue_id)
        if issue is None:
            raise NotFoundError(f"issue not found: {issue_id}")
        return issue

    async def _require_running_claim(self, issue_id: str, token: str | None) -> Issue:
        issue = await self._require_issue(issue_id)
        if issue.status != "running" or not token or not issue.claim_token_hash:
            raise ClaimError("active issue claim is required")
        if not hmac.compare_digest(issue.claim_token_hash, _token_hash(token)):
            raise ClaimError("claim token is invalid")
        if issue.claim_expires_at is None or _as_utc(issue.claim_expires_at) <= utc_now():
            raise ClaimError("claim has expired")
        return issue

    async def _active_attempt(self, issue_id: str) -> AgentAttempt:
        attempt = await self.session.scalar(
            select(AgentAttempt).where(AgentAttempt.issue_id == issue_id, AgentAttempt.status == "running").order_by(AgentAttempt.attempt_number.desc()).limit(1)
        )
        if attempt is None:
            raise ClaimError("active attempt not found")
        return attempt

    async def _finish_attempt(self, issue_id: str, status: str, *, thread_id: str | None = None) -> None:
        attempt = await self._active_attempt(issue_id)
        attempt.status = status
        attempt.completed_at = utc_now()
        if thread_id:
            attempt.thread_id = thread_id

    @staticmethod
    def _clear_claim(issue: Issue) -> None:
        issue.claim_worker_id = None
        issue.claim_token_hash = None
        issue.claim_expires_at = None

    def _add_event(
        self, issue_id: str, event: str, actor_type: str, actor_id: str | None,
        from_status: str | None, to_status: str | None, payload: dict[str, Any],
    ) -> IssueEvent:
        row = IssueEvent(
            issue_id=issue_id, event=event, actor_type=actor_type, actor_id=actor_id,
            from_status=from_status, to_status=to_status, payload=payload,
        )
        self.session.add(row)
        return row
