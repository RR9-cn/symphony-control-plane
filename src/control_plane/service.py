from __future__ import annotations

import asyncio
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
    Project,
    ProjectWorkflowSnapshot,
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
    IssueContinueCommand,
    IssueArchiveCommand,
    IssueCreate,
    IssueDeliveryCommand,
    IssuePatch,
    IssueView,
    MaintenanceResult,
    ReleaseRequest,
    ProjectRef,
    StatusTransitionRequest,
    WorkerHeartbeat,
    WorkerRegistration,
    WorkerView,
)
from control_plane.workspace_summary import (
    WorkspaceSummaryError,
    collect_change_summary,
    empty_change_summary,
)
from symphony_windows.workflow import WorkspaceConfig
from symphony_windows.workspace import WorkspaceError, WorkspaceManager, workspace_key


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
FOLLOW_UP_REQUESTED_BY = "control-plane-followup"
FOLLOW_UP_QUESTION = (
    "Authoritative follow-up execution instruction. Apply this instruction to the "
    "existing Issue workspace before completing the Issue again."
)
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


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ControlPlaneService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or Settings()
        root = Path(self.settings.issue_workspace_root)
        if not root.is_absolute():
            root = Path(self.settings.managed_runner_workflow).resolve().parent / root
        self.gitlab_token = (
            self.settings.gitlab_token.get_secret_value()
            if self.settings.gitlab_token is not None
            else None
        )
        self.delivery = IssueDeliveryManager(root, gitlab_token=self.gitlab_token)

    async def create_issue(self, command: IssueCreate) -> IssueView:
        async with self.session.begin():
            if await self.session.get(Issue, command.id):
                raise ConflictError(f"issue already exists: {command.id}")
            existing_identifier = await self.session.scalar(
                select(Issue.id).where(Issue.identifier == command.identifier)
            )
            if existing_identifier is not None:
                raise ConflictError(
                    f"issue identifier already exists: {command.identifier}"
                )
            project = await self.session.get(Project, command.project_id)
            if project is None:
                raise NotFoundError(f"project not found: {command.project_id}")
            if not project.enabled or project.status != "available" or not project.current_snapshot_id:
                raise ConflictError("project must be enabled with a valid WORKFLOW.md before creating Issues")
            snapshot = await self.session.get(ProjectWorkflowSnapshot, project.current_snapshot_id)
            if snapshot is None or snapshot.status != "valid":
                raise ConflictError("project workflow snapshot is unavailable")
            workspace_root = Path(str(snapshot.parsed_config["workspace"]["root"]))
            issue = Issue(
                **command.model_dump(), status="ready", version=1,
                workflow_snapshot_id=snapshot.id,
                source_commit=snapshot.source_commit,
                workspace_path=str((workspace_root / workspace_key(str(command.identifier))).resolve()),
            )
            self.session.add(issue)
            await self.session.flush()
            self._add_event(issue.id, "created", "user", "manual-intake", None, "ready", {})
        return await self.get_issue(command.id)

    async def list_issues(self, statuses: list[str] | None = None, issue_ids: list[str] | None = None, project_id: str | None = None) -> list[IssueView]:
        statement = select(Issue)
        if statuses:
            statement = statement.where(Issue.status.in_(statuses))
        if issue_ids:
            statement = statement.where(Issue.id.in_(issue_ids))
        if project_id:
            statement = statement.where(Issue.project_id == project_id)
        rows = (await self.session.scalars(statement.order_by(Issue.priority, Issue.created_at))).all()
        return [await self._issue_view(row) for row in rows]

    async def candidates(self, project_id: str, limit: int = 100) -> list[IssueView]:
        rows = (
            await self.session.scalars(
                select(Issue).where(Issue.project_id == project_id, Issue.status == "ready").order_by(Issue.priority, Issue.created_at).limit(limit)
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
            values = command.model_dump(exclude={"expected_version"}, exclude_none=True)
            routing_fields = {
                "url",
                "assignee_id",
                "labels",
                "blocked_by",
                "native_ref",
                "dispatchable",
                "branch_name",
            }
            if issue.status == "running" and not set(values) <= routing_fields:
                raise ConflictError(
                    "only routing metadata can be edited while an Issue is running"
                )
            if issue.status not in {
                "ready",
                "running",
                "blocked",
                "needs_human",
                "reviewing",
            }:
                raise ConflictError(f"issue cannot be edited while status={issue.status}")
            if "identifier" in values and issue.status != "ready":
                raise ConflictError("issue identifier can only be changed while ready")
            identifier = values.get("identifier")
            if identifier is not None:
                existing_identifier = await self.session.scalar(
                    select(Issue.id).where(
                        Issue.identifier == identifier, Issue.id != issue_id
                    )
                )
                if existing_identifier is not None:
                    raise ConflictError(
                        f"issue identifier already exists: {identifier}"
                    )
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
            candidate = await self._require_issue(issue_id)
            project = await self.session.get(Project, candidate.project_id)
            snapshot = await self.session.get(ProjectWorkflowSnapshot, candidate.workflow_snapshot_id)
            if project is None or not project.enabled or project.status != "available":
                raise ClaimError("issue project is not available for new Claims")
            if snapshot is None or snapshot.status != "valid":
                raise ClaimError("issue workflow snapshot is unavailable")
            result = await self.session.execute(
                update(Issue)
                .where(
                    Issue.id == issue_id,
                    Issue.project_id == command.project_id,
                    Issue.status == "ready",
                    Issue.version == command.expected_version,
                )
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
                config_snapshot={
                    **command.agent.config,
                    "project_id": project.id,
                    "project_key": project.key,
                    "repository_path_hash": hashlib.sha256(project.repository_path.encode("utf-8")).hexdigest(),
                    "source_commit": candidate.source_commit,
                    "workflow_snapshot_id": snapshot.id,
                    "workflow_revision": snapshot.workflow_revision,
                    "workspace_path": candidate.workspace_path,
                    "project_assets": snapshot.parsed_config.get("project_assets", {}),
                },
                status="running",
            )
            self.session.add(attempt)
            self._add_event(issue_id, "claimed", "worker", command.worker_id, "ready", "running", {"attempt": number})
            await self.session.flush()
        latest_previous = await self.session.scalar(
            select(AgentAttempt).where(AgentAttempt.issue_id == issue_id, AgentAttempt.id != attempt.id, AgentAttempt.thread_id.is_not(None)).order_by(AgentAttempt.attempt_number.desc()).limit(1)
        )
        decisions = list(
            await self.session.scalars(
                select(HumanDecision).where(HumanDecision.issue_id == issue_id, HumanDecision.status == "resolved").order_by(HumanDecision.created_at)
            )
        )
        # Follow-up decisions are also sent through the long-standing
        # resume_decisions field so already-running, older Workers do not silently
        # drop a newly introduced continuation field. Only the decisions created
        # after the previous Attempt completed belong to this resume.
        if latest_previous is not None and latest_previous.completed_at is not None:
            decisions = [
                row
                for row in decisions
                if row.requested_by != FOLLOW_UP_REQUESTED_BY
                or row.created_at > latest_previous.completed_at
            ]
        follow_up_payloads = (
            await self.session.scalars(
                select(IssueEvent.payload)
                .where(
                    IssueEvent.issue_id == issue_id,
                    IssueEvent.event == "human_followup_requested",
                    *(
                        (IssueEvent.created_at > latest_previous.completed_at,)
                        if latest_previous is not None
                        and latest_previous.completed_at is not None
                        else ()
                    ),
                )
                .order_by(IssueEvent.created_at)
            )
        ).all()
        resume_instructions = [
            instruction
            for payload in follow_up_payloads
            if isinstance(payload, dict)
            for instruction in [payload.get("instruction")]
            if isinstance(instruction, str) and instruction.strip()
        ]
        return ClaimResult(
            issue=await self.get_issue(issue_id), claim_token=token,
            attempt=AgentAttemptView.model_validate(attempt),
            resume_thread_id=latest_previous.thread_id if latest_previous else None,
            resume_decisions=[DecisionView.model_validate(row) for row in decisions],
            resume_instructions=resume_instructions,
            continuation_turn_count=latest_previous.turn_count if latest_previous else 0,
            workflow_content=snapshot.workflow_content,
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
            await self._finish_attempt(
                issue_id,
                "retry_queued",
                thread_id=command.thread_id,
                status_reason=command.reason,
            )
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

    async def continue_issue(
        self, issue_id: str, command: IssueContinueCommand
    ) -> IssueView:
        async with self.session.begin():
            issue = await self._require_issue(issue_id)
            if issue.status != "reviewing":
                raise ConflictError(
                    f"issue cannot continue from status={issue.status}"
                )
            if issue.version != command.expected_version:
                raise ConflictError("issue version changed")
            issue.status = "ready"
            issue.retry_at = None
            issue.blocker = None
            issue.version += 1
            issue.updated_at = utc_now()
            # Persist the instruction as an already-resolved human decision as
            # well as an Issue event. Older Workers already understand
            # resume_decisions, which makes continuation backward compatible
            # while Workers are upgraded independently from the Control Plane.
            self.session.add(
                HumanDecision(
                    issue_id=issue_id,
                    question=FOLLOW_UP_QUESTION,
                    options=[],
                    status="resolved",
                    response=command.instruction,
                    requested_by=FOLLOW_UP_REQUESTED_BY,
                    resolved_by="control-plane-ui",
                    resolved_at=utc_now(),
                )
            )
            self._add_event(
                issue_id,
                "human_followup_requested",
                "human",
                "control-plane-ui",
                "reviewing",
                "ready",
                {"instruction": command.instruction},
            )
        return await self.get_issue(issue_id)

    async def deliver_issue(self, issue_id: str, command: IssueDeliveryCommand) -> IssueView:
        issue = await self._require_issue(issue_id)
        if issue.version != command.expected_version:
            raise ConflictError("issue version changed")
        project = await self.session.get(Project, issue.project_id)
        if project is None:
            raise ConflictError("issue project is unavailable")
        repository_url = project.repository_path
        base_branch = project.default_branch
        delivery_identifier = issue.identifier
        source_commit = issue.source_commit
        workspace_path = Path(issue.workspace_path)
        delivery = IssueDeliveryManager(workspace_path.parent, gitlab_token=self.gitlab_token)
        status = issue.status
        title = issue.title
        head_branch = issue.head_branch
        local_commit = issue.local_commit
        pull_request_url = issue.pull_request
        overview = (
            await self.latest_change_overview(issue_id)
            if command.action == "approve_result"
            else None
        )
        # SQLAlchemy starts a transaction for the read above. End that read
        # transaction before invoking git/GitHub, then use a fresh transaction
        # to compare-and-set the delivery state after the side effect succeeds.
        await self.session.rollback()
        if command.action == "approve_result":
            if status != "reviewing":
                raise ConflictError(f"issue cannot approve result while status={status}")
            try:
                await delivery.assert_base_unchanged(
                    source_repository=repository_url,
                    base_branch=base_branch,
                    expected_commit=source_commit,
                )
                branch, commit = await delivery.prepare_local_commit(delivery_identifier, title)
            except DeliveryError as error:
                raise ConflictError(str(error)) from error
            try:
                change_summary = await collect_change_summary(
                    workspace_path, source_commit, overview=overview
                )
            except (OSError, WorkspaceSummaryError):
                change_summary = empty_change_summary()
                change_summary["overview"] = overview
            async with self.session.begin():
                current = await self._require_issue(issue_id)
                if current.status != "reviewing" or current.version != command.expected_version:
                    raise ConflictError("issue changed while preparing delivery")
                current.status = "awaiting_publish"
                current.head_branch = branch
                current.local_commit = commit
                current.change_summary = change_summary
                current.version += 1
                current.updated_at = utc_now()
                self._add_event(issue_id, "result_approved", "human", "control-plane-ui", "reviewing", "awaiting_publish", {"branch": branch, "commit": commit})
        elif command.action == "authorize_publish":
            if status != "awaiting_publish" or not head_branch or not local_commit:
                raise ConflictError(f"issue cannot publish while status={status}")
            try:
                await delivery.assert_base_is_ancestor(
                    delivery_identifier,
                    source_repository=repository_url,
                    base_branch=base_branch,
                    commit=local_commit,
                )
                review_request = await delivery.publish(
                    delivery_identifier, repository_url=repository_url, base_branch=base_branch,
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
                current.pull_request = review_request
                current.version += 1
                current.updated_at = utc_now()
                self._add_event(issue_id, "review_request_created", "human", "control-plane-ui", "awaiting_publish", "pr_open", {"review_request": review_request})
        else:
            if status != "pr_open" or not pull_request_url:
                raise ConflictError(f"issue cannot confirm merge while status={status}")
            try:
                await delivery.verify_merged(repository_url, pull_request_url)
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
                self._add_event(issue_id, "review_request_merged", "human", "control-plane-ui", "pr_open", "done", {"review_request": current.pull_request})
        return await self.get_issue(issue_id)

    async def archive_issue(
        self, issue_id: str, command: IssueArchiveCommand
    ) -> IssueView:
        issue = await self._require_issue(issue_id)
        if issue.version != command.expected_version:
            raise ConflictError("issue version changed")
        if issue.archived_at is not None:
            return await self._issue_view(issue)
        identifier = issue.identifier
        project_id = issue.project_id
        source_commit = issue.source_commit
        workspace = Path(issue.workspace_path).resolve()
        root = workspace.parent
        expected_workspace = (root / workspace_key(identifier)).resolve()
        if workspace != expected_workspace:
            raise ConflictError("Issue workspace path does not match its identifier")
        archive_version = command.expected_version
        original_status = issue.status
        change_summary = dict(issue.change_summary or empty_change_summary())
        overview = await self.latest_change_overview(issue_id)
        await self.session.rollback()
        try:
            change_summary = await collect_change_summary(
                workspace, source_commit, overview=overview
            )
        except (OSError, WorkspaceSummaryError):
            if overview:
                change_summary["overview"] = overview
        if original_status not in {"done", "cancelled"}:
            async with self.session.begin():
                current = await self._require_issue(issue_id)
                if current.version != command.expected_version:
                    raise ConflictError("issue changed while requesting archive")
                if current.status == "running":
                    await self._finish_attempt(issue_id, "cancelled")
                    self._clear_claim(current)
                current.status = "cancelled"
                current.retry_at = None
                current.blocker = None
                current.change_summary = change_summary
                current.version += 1
                current.updated_at = utc_now()
                self._add_event(
                    issue_id,
                    "force_archive_cancelled",
                    "human",
                    "control-plane-ui",
                    original_status,
                    "cancelled",
                    {"reason": "force_archive"},
                )
                archive_version = current.version

        for _attempt in range(30):
            if not await self._live_worker_owns_issue(
                project_id, issue_id, identifier
            ):
                break
            await self.session.rollback()
            await asyncio.sleep(0.5)
        else:
            raise ConflictError(
                "Issue was cancelled but its Runner is still stopping; retry force archive shortly"
            )
        await self.session.rollback()
        try:
            removed = await WorkspaceManager(WorkspaceConfig(root=root)).remove(
                {"id": issue_id, "identifier": identifier}
            )
        except WorkspaceError as error:
            raise ConflictError(str(error)) from error
        async with self.session.begin():
            current = await self._require_issue(issue_id)
            if current.version != archive_version:
                raise ConflictError("issue changed while archiving workspace")
            if current.status not in {"done", "cancelled"}:
                raise ConflictError("issue is no longer terminal")
            current.archived_at = utc_now()
            current.change_summary = change_summary
            current.version += 1
            current.updated_at = current.archived_at
            self._add_event(
                issue_id,
                "workspace_force_archived",
                "human",
                "control-plane-ui",
                current.status,
                current.status,
                {"workspace": str(workspace), "removed": removed},
            )
        return await self.get_issue(issue_id)

    async def _live_worker_owns_issue(
        self, project_id: str, issue_id: str, identifier: str
    ) -> bool:
        workers = (
            await self.session.scalars(
                select(Worker).where(
                    Worker.project_id == project_id,
                    Worker.state.in_(("starting", "running", "stopping")),
                )
            )
        ).all()
        now = utc_now()
        return any(
            (now - _as_utc(worker.last_seen_at)).total_seconds()
            <= self.settings.worker_offline_after_seconds
            and (issue_id in worker.active_issues or identifier in worker.active_issues)
            for worker in workers
        )

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
            if await self.session.get(Project, command.project_id) is None:
                raise NotFoundError(f"project not found: {command.project_id}")
            worker = await self.session.get(Worker, command.worker_id)
            values = command.model_dump()
            values["id"] = values.pop("worker_id")
            if worker is None:
                worker = Worker(
                    **values,
                    state="starting",
                    runtime_snapshot={},
                    runtime_snapshot_at=None,
                )
                self.session.add(worker)
            else:
                for key, value in values.items():
                    setattr(worker, key, value)
                worker.state = "starting"
                worker.runtime_snapshot = {}
                worker.runtime_snapshot_at = None
                worker.stop_requested = False
                worker.stopped_at = None
                worker.last_seen_at = utc_now()
        return WorkerView.model_validate(worker)

    async def heartbeat_worker(self, worker_id: str, command: WorkerHeartbeat) -> WorkerView:
        async with self.session.begin():
            worker = await self.session.get(Worker, worker_id)
            if worker is None:
                raise NotFoundError(f"worker not found: {worker_id}")
            snapshot = command.runtime_snapshot
            if snapshot.get("worker_id") != worker_id:
                raise ConflictError("runtime snapshot worker_id does not match")
            if snapshot.get("project_id") != worker.project_id:
                raise ConflictError("runtime snapshot project_id does not match")
            running = snapshot.get("running")
            if not isinstance(running, list):
                raise ConflictError("runtime snapshot running must be a list")
            snapshot_issue_ids = {
                str(row.get("issue_id"))
                for row in running
                if isinstance(row, dict) and row.get("issue_id")
            }
            if snapshot_issue_ids != set(command.active_issues):
                raise ConflictError(
                    "runtime snapshot running Issues do not match active_issues"
                )
            worker.state = command.state
            worker.active_issues = command.active_issues
            worker.runtime_snapshot = snapshot
            worker.runtime_snapshot_at = utc_now()
            worker.last_seen_at = worker.runtime_snapshot_at
        return WorkerView.model_validate(worker)

    async def list_workers(self, project_id: str | None = None) -> list[WorkerView]:
        statement = select(Worker)
        if project_id:
            statement = statement.where(Worker.project_id == project_id)
        rows = (await self.session.scalars(statement.order_by(Worker.started_at.desc()))).all()
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
            worker.runtime_snapshot = {}
            worker.runtime_snapshot_at = utc_now()
            worker.stopped_at = utc_now()
            worker.last_seen_at = utc_now()
        return WorkerView.model_validate(worker)

    async def list_agent_runtimes(self) -> list[AgentRuntimeView]:
        issues = (await self.session.scalars(select(Issue).order_by(Issue.updated_at.desc()))).all()
        workers = (await self.session.scalars(select(Worker))).all()
        now = utc_now()
        live_rows: dict[str, tuple[Worker, dict[str, Any]]] = {}
        for worker in workers:
            if worker.runtime_snapshot_at is None:
                continue
            age = (now - _as_utc(worker.runtime_snapshot_at)).total_seconds()
            if age > self.settings.worker_offline_after_seconds:
                continue
            rows = worker.runtime_snapshot.get("running")
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict) or not row.get("issue_id"):
                    continue
                issue_id = str(row["issue_id"])
                previous = live_rows.get(issue_id)
                if previous is None or _as_utc(
                    worker.runtime_snapshot_at
                ) > _as_utc(previous[0].runtime_snapshot_at):  # type: ignore[arg-type]
                    live_rows[issue_id] = (worker, row)
        result: list[AgentRuntimeView] = []
        for issue in issues:
            attempt = await self.session.scalar(select(AgentAttempt).where(AgentAttempt.issue_id == issue.id).order_by(AgentAttempt.attempt_number.desc()).limit(1))
            live = live_rows.get(issue.id)
            if live is not None:
                worker, row = live
                tokens = row.get("tokens") if isinstance(row.get("tokens"), dict) else {}
                result.append(
                    AgentRuntimeView(
                        issue_id=issue.id,
                        title=issue.title,
                        state=RUNTIME_STATE[issue.status],
                        worker_id=worker.id,
                        attempt_id=_optional_text(row.get("attempt_id")),
                        attempt_number=_optional_int(row.get("attempt_number")),
                        session_id=_optional_text(row.get("session_id")),
                        thread_id=_optional_text(row.get("thread_id")),
                        turn_id=_optional_text(row.get("turn_id")),
                        turn_count=_optional_int(row.get("turn_count")) or 0,
                        phase=_optional_text(row.get("phase")),
                        codex_app_server_pid=_optional_int(
                            row.get("codex_app_server_pid")
                        ),
                        last_event=_optional_text(row.get("last_event")),
                        last_message=_optional_text(row.get("last_message")),
                        last_event_at=row.get("last_event_at"),
                        duration_seconds=_optional_number(
                            row.get("duration_seconds")
                        ),
                        workspace_path=_optional_text(row.get("workspace_path")),
                        tokens={
                            "input_tokens": _optional_int(tokens.get("input_tokens")) or 0,
                            "output_tokens": _optional_int(tokens.get("output_tokens")) or 0,
                            "total_tokens": _optional_int(tokens.get("total_tokens")) or 0,
                        },
                        runtime_source="orchestrator",
                        snapshot_at=worker.runtime_snapshot_at,
                        started_at=row.get("started_at"),
                        updated_at=issue.updated_at,
                    )
                )
                continue
            result.append(
                AgentRuntimeView(
                    issue_id=issue.id, title=issue.title, state=RUNTIME_STATE[issue.status],
                    worker_id=issue.claim_worker_id, attempt_id=attempt.id if attempt else None,
                    attempt_number=attempt.attempt_number if attempt else None,
                    session_id=attempt.session_id if attempt else None,
                    thread_id=attempt.thread_id if attempt else None, turn_id=attempt.turn_id if attempt else None,
                    turn_count=attempt.turn_count if attempt else 0,
                    phase="snapshot_unavailable" if issue.status == "running" else None,
                    codex_app_server_pid=None,
                    last_event=None,
                    last_message=attempt.status_reason if attempt else None,
                    last_event_at=None,
                    duration_seconds=attempt.duration_seconds if attempt else None,
                    workspace_path=issue.workspace_path,
                    tokens={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                    runtime_source="database",
                    snapshot_at=None,
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
        artifacts = (await self.session.scalars(select(IssueArtifact).where(IssueArtifact.issue_id == issue.id).order_by(IssueArtifact.created_at.desc()))).all()
        snapshot = await self.session.get(ProjectWorkflowSnapshot, issue.workflow_snapshot_id)
        project = await self.session.get(Project, issue.project_id)
        if snapshot is None:
            raise ConflictError("issue workflow snapshot is unavailable")
        if project is None:
            raise ConflictError("issue project is unavailable")
        change_summary = dict(issue.change_summary or empty_change_summary())
        if not change_summary.get("overview"):
            change_summary["overview"] = await self.latest_change_overview(issue.id)
        return IssueView(
            id=issue.id, identifier=issue.identifier, title=issue.title,
            description=issue.description, state=issue.status, status=issue.status,
            priority=issue.priority, version=issue.version,
            project_id=issue.project_id, workflow_snapshot_id=issue.workflow_snapshot_id,
            project=ProjectRef(id=project.id, key=project.key, name=project.name),
            workflow_revision=snapshot.workflow_revision,
            source_commit=issue.source_commit, workspace_path=issue.workspace_path,
            acceptance_criteria=issue.acceptance_criteria, url=issue.url,
            assignee_id=issue.assignee_id, labels=issue.labels,
            blocked_by=issue.blocked_by, native_ref=issue.native_ref,
            dispatchable=issue.dispatchable, branch_name=issue.branch_name,
            blocker=issue.blocker,
            claim=ClaimView(worker_id=issue.claim_worker_id, expires_at=issue.claim_expires_at),
            retry_at=issue.retry_at, head_branch=issue.head_branch, local_commit=issue.local_commit,
            pull_request=issue.pull_request, merged_at=issue.merged_at,
            archived_at=_as_utc(issue.archived_at) if issue.archived_at else None,
            change_summary=change_summary,
            artifacts=[ArtifactView.model_validate(row) for row in artifacts],
            created_at=issue.created_at, updated_at=issue.updated_at,
        )

    async def latest_change_overview(self, issue_id: str) -> str | None:
        """Return the latest completed Agent message as the human change overview."""
        detail = await self.session.scalar(
            select(AgentAttemptEvent.detail)
            .where(
                AgentAttemptEvent.issue_id == issue_id,
                AgentAttemptEvent.event_type == "agent_message_completed",
                AgentAttemptEvent.detail.is_not(None),
            )
            .order_by(
                AgentAttemptEvent.created_at.desc(),
                AgentAttemptEvent.sequence.desc(),
            )
            .limit(1)
        )
        if not isinstance(detail, str) or not detail.strip():
            return None
        return detail.strip()[:4000]

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

    async def _finish_attempt(
        self,
        issue_id: str,
        status: str,
        *,
        thread_id: str | None = None,
        status_reason: str | None = None,
    ) -> None:
        attempt = await self._active_attempt(issue_id)
        attempt.status = status
        attempt.status_reason = status_reason
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
