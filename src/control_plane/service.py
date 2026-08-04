from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Select, delete, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from control_plane.config import Settings
from control_plane.errors import (
    AgentProfileConflictError,
    ClaimError,
    ConflictError,
    InvalidTransitionError,
    NotFoundError,
)
from control_plane.models import (
    AgentAttempt,
    AgentProfile,
    Feature,
    HumanDecision,
    WorkItem,
    WorkItemArtifact,
    WorkItemDependency,
    WorkItemEvent,
    utc_now,
)
from control_plane.protocol import PROTOCOL
from control_plane.schemas import (
    AgentAttemptView,
    AgentProfileView,
    ArtifactCreate,
    ArtifactData,
    ArtifactView,
    ClaimRequest,
    ClaimResult,
    ClaimView,
    DecisionCommand,
    DecisionView,
    EventCreate,
    EventView,
    FeatureCreate,
    FeatureView,
    HeartbeatRequest,
    MaintenanceResult,
    ReleaseRequest,
    RepositoryData,
    StatusTransitionRequest,
    WorkItemCreate,
    WorkItemPatch,
    WorkItemView,
)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ControlPlaneService:
    def __init__(self, session: AsyncSession, settings: Settings):
        self.session = session
        self.settings = settings

    async def create_feature(self, command: FeatureCreate) -> FeatureView:
        async with self.session.begin():
            if await self.session.get(Feature, command.id):
                raise ConflictError(f"feature already exists: {command.id}")
            feature = Feature(**command.model_dump())
            self.session.add(feature)
        return FeatureView.model_validate(feature)

    async def get_feature(self, feature_id: str) -> FeatureView:
        feature = await self.session.get(Feature, feature_id)
        if feature is None:
            raise NotFoundError(f"feature not found: {feature_id}")
        return FeatureView.model_validate(feature)

    async def create_work_item(self, command: WorkItemCreate) -> WorkItemView:
        async with self.session.begin():
            if await self.session.get(WorkItem, command.id):
                raise ConflictError(f"work item already exists: {command.id}")
            if await self.session.get(Feature, command.feature_id) is None:
                raise NotFoundError(f"feature not found: {command.feature_id}")
            if command.parent_id and await self.session.get(WorkItem, command.parent_id) is None:
                raise NotFoundError(f"parent work item not found: {command.parent_id}")
            dependencies = await self._require_dependencies(command.dependencies, command.feature_id)
            if command.id in command.dependencies:
                raise ConflictError("work item cannot depend on itself")
            if command.status == "ready" and any(item.status != "done" for item in dependencies):
                raise ConflictError("ready work item has incomplete dependencies")

            work_item = WorkItem(
                id=command.id,
                feature_id=command.feature_id,
                parent_id=command.parent_id,
                title=command.title,
                description=command.description,
                stage=command.stage,
                agent_role=command.agent_role,
                status=command.status,
                priority=command.priority,
                version=1,
                repository=command.repository.model_dump(),
                acceptance_criteria=command.acceptance_criteria,
            )
            self.session.add(work_item)
            await self.session.flush()
            self.session.add_all(
                WorkItemDependency(work_item_id=command.id, depends_on_id=item_id)
                for item_id in command.dependencies
            )
            self._add_artifacts(command.id, "input", command.input_artifacts)
            self._add_artifacts(command.id, "output", command.output_artifacts)
            self._add_event(
                command.id,
                "created",
                "user",
                None,
                None,
                command.status,
                {"version": 1},
            )
        return await self.get_work_item(command.id)

    async def get_work_item(self, item_id: str) -> WorkItemView:
        item = await self.session.get(WorkItem, item_id)
        if item is None:
            raise NotFoundError(f"work item not found: {item_id}")
        return await self._view(item)

    async def list_work_items(
        self,
        feature_id: str | None = None,
        statuses: list[str] | None = None,
        item_ids: list[str] | None = None,
    ) -> list[WorkItemView]:
        statement: Select[tuple[WorkItem]] = select(WorkItem).order_by(
            WorkItem.priority, WorkItem.created_at, WorkItem.id
        )
        if feature_id:
            statement = statement.where(WorkItem.feature_id == feature_id)
        if statuses:
            statement = statement.where(WorkItem.status.in_(statuses))
        if item_ids:
            statement = statement.where(WorkItem.id.in_(item_ids))
        items = (await self.session.scalars(statement)).all()
        return [await self._view(item) for item in items]

    async def patch_work_item(self, item_id: str, command: WorkItemPatch) -> WorkItemView:
        async with self.session.begin():
            current = await self.session.get(WorkItem, item_id)
            if current is None:
                raise NotFoundError(f"work item not found: {item_id}")
            changes = command.model_dump(exclude={"expected_version", "dependencies"}, exclude_none=True)
            if "repository" in changes:
                changes["repository"] = command.repository.model_dump()  # type: ignore[union-attr]
            if command.dependencies is not None:
                if current.status != "draft":
                    raise ConflictError("dependencies may only be changed while status=draft")
                if item_id in command.dependencies:
                    raise ConflictError("work item cannot depend on itself")
                await self._require_dependencies(command.dependencies, current.feature_id)
                if await self._would_create_dependency_cycle(item_id, command.dependencies):
                    raise ConflictError("dependency update would create a cycle")
            result = await self.session.execute(
                update(WorkItem)
                .where(
                    WorkItem.id == item_id,
                    WorkItem.version == command.expected_version,
                )
                .values(**changes, version=WorkItem.version + 1, updated_at=utc_now())
            )
            if result.rowcount != 1:
                raise ConflictError("work item version changed")
            if command.dependencies is not None:
                await self.session.execute(
                    delete(WorkItemDependency).where(WorkItemDependency.work_item_id == item_id)
                )
                self.session.add_all(
                    WorkItemDependency(work_item_id=item_id, depends_on_id=dependency)
                    for dependency in command.dependencies
                )
            self._add_event(
                item_id,
                "updated",
                "user",
                None,
                current.status,
                current.status,
                {"fields": sorted(changes)},
            )
        return await self.get_work_item(item_id)

    async def candidates(self, limit: int = 100) -> list[WorkItemView]:
        incomplete = self._incomplete_dependency_exists()
        items = (
            await self.session.scalars(
                select(WorkItem)
                .where(WorkItem.status == "ready", ~incomplete)
                .order_by(WorkItem.priority, WorkItem.created_at, WorkItem.id)
                .limit(limit)
            )
        ).all()
        return [await self._view(item) for item in items]

    async def claim(self, item_id: str, command: ClaimRequest) -> ClaimResult:
        token = secrets.token_urlsafe(32)
        token_hash = _token_hash(token)
        expires_at = utc_now() + timedelta(seconds=command.lease_seconds)
        incomplete = self._incomplete_dependency_exists()
        async with self.session.begin():
            profile: AgentProfile | None = None
            if command.profile is not None:
                profile = await self.session.scalar(
                    select(AgentProfile).where(
                        AgentProfile.name == command.profile.name,
                        AgentProfile.version == command.profile.version,
                    )
                )
                if profile is None:
                    profile = AgentProfile(
                        name=command.profile.name,
                        version=command.profile.version,
                        config=command.profile.config,
                    )
                    self.session.add(profile)
                    await self.session.flush()
                elif not profile.active:
                    raise AgentProfileConflictError(
                        f"agent profile is inactive: {profile.name} v{profile.version}"
                    )
                elif profile.config != command.profile.config:
                    raise AgentProfileConflictError(
                        f"agent profile version already has different config: "
                        f"{profile.name} v{profile.version}"
                    )
            result = await self.session.execute(
                update(WorkItem)
                .where(
                    WorkItem.id == item_id,
                    WorkItem.status == "ready",
                    WorkItem.version == command.expected_version,
                    ~incomplete,
                )
                .values(
                    status="running",
                    version=WorkItem.version + 1,
                    claim_worker_id=command.worker_id,
                    claim_token_hash=token_hash,
                    claim_expires_at=expires_at,
                    retry_at=None,
                    updated_at=utc_now(),
                )
            )
            if result.rowcount != 1:
                await self._raise_claim_conflict(item_id, command.expected_version)
            latest_attempt_number = await self.session.scalar(
                select(func.coalesce(func.max(AgentAttempt.attempt_number), 0)).where(
                    AgentAttempt.work_item_id == item_id
                )
            )
            attempt_number = (latest_attempt_number or 0) + 1
            attempt = AgentAttempt(
                work_item_id=item_id,
                attempt_number=attempt_number,
                worker_id=command.worker_id,
                profile_id=profile.id if profile is not None else None,
                profile_snapshot=command.profile.config if command.profile else {},
            )
            self.session.add(attempt)
            await self.session.flush()
            self._add_event(
                item_id,
                "claimed",
                "worker",
                command.worker_id,
                "ready",
                "running",
                {
                    "lease_seconds": command.lease_seconds,
                    "attempt": attempt_number,
                    "profile_name": command.profile.name if command.profile else None,
                    "profile_version": command.profile.version if command.profile else None,
                },
            )
        return ClaimResult(
            work_item=await self.get_work_item(item_id),
            claim_token=token,
            attempt=AgentAttemptView.model_validate(attempt),
        )

    async def list_agent_profiles(self) -> list[AgentProfileView]:
        profiles = (
            await self.session.scalars(
                select(AgentProfile).order_by(
                    AgentProfile.name, AgentProfile.version.desc()
                )
            )
        ).all()
        return [AgentProfileView.model_validate(profile) for profile in profiles]

    async def list_attempts(self, item_id: str) -> list[AgentAttemptView]:
        if await self.session.get(WorkItem, item_id) is None:
            raise NotFoundError(f"work item not found: {item_id}")
        attempts = (
            await self.session.scalars(
                select(AgentAttempt)
                .where(AgentAttempt.work_item_id == item_id)
                .order_by(AgentAttempt.attempt_number)
            )
        ).all()
        return [AgentAttemptView.model_validate(attempt) for attempt in attempts]

    async def heartbeat(self, item_id: str, command: HeartbeatRequest) -> WorkItemView:
        now = utc_now()
        token_hash = _token_hash(command.claim_token)
        async with self.session.begin():
            result = await self.session.execute(
                update(WorkItem)
                .where(
                    WorkItem.id == item_id,
                    WorkItem.status == "running",
                    WorkItem.claim_token_hash == token_hash,
                    WorkItem.claim_expires_at > now,
                )
                .values(
                    claim_expires_at=now + timedelta(seconds=command.lease_seconds),
                    version=WorkItem.version + 1,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                raise ClaimError("claim is missing, expired, or owned by another worker")
            self._add_event(
                item_id,
                "heartbeat",
                "worker",
                None,
                "running",
                "running",
                {"lease_seconds": command.lease_seconds},
            )
        return await self.get_work_item(item_id)

    async def release(self, item_id: str, command: ReleaseRequest) -> WorkItemView:
        payload: dict[str, Any] = {"reason": command.reason}
        if command.retry_delay_seconds is not None:
            payload["retry_delay_seconds"] = command.retry_delay_seconds
        transition = StatusTransitionRequest(
            to_status="retry_queued",
            event="retry_scheduled",
            actor_type="control_plane",
            claim_token=command.claim_token,
            payload=payload,
        )
        return await self.transition(item_id, transition)

    async def transition(
        self, item_id: str, command: StatusTransitionRequest
    ) -> WorkItemView:
        async with self.session.begin():
            item = await self.session.get(WorkItem, item_id)
            if item is None:
                raise NotFoundError(f"work item not found: {item_id}")
            from_status = item.status
            try:
                definition = PROTOCOL.transition(from_status, command.to_status, command.event)
            except ValueError as error:
                raise InvalidTransitionError(str(error)) from error
            self._verify_transition_actor(definition.actor, command.actor_type)
            if from_status == "running" and command.event != "work_item_cancelled":
                self._verify_claim(item, command.claim_token)
            await self._validate_transition_guards(item, command)

            values: dict[str, Any] = {
                "status": command.to_status,
                "version": WorkItem.version + 1,
                "updated_at": utc_now(),
            }
            if "clear_claim" in definition.effects:
                values.update(
                    claim_worker_id=None, claim_token_hash=None, claim_expires_at=None
                )
            if command.to_status == "retry_queued":
                retry_delay = command.payload.get(
                    "retry_delay_seconds",
                    self.settings.default_retry_delay_seconds,
                )
                if not isinstance(retry_delay, int) or not 0 <= retry_delay <= 86_400:
                    raise InvalidTransitionError("invalid retry_delay_seconds")
                values["retry_at"] = utc_now() + timedelta(
                    seconds=retry_delay
                )
            if command.to_status == "blocked":
                values["blocker"] = command.payload["blocker"]
            if command.to_status == "ready":
                values.update(blocker=None, retry_at=None)

            result = await self.session.execute(
                update(WorkItem)
                .where(
                    WorkItem.id == item_id,
                    WorkItem.status == from_status,
                    WorkItem.version == item.version,
                )
                .values(**values)
            )
            if result.rowcount != 1:
                raise ConflictError("work item changed during transition")
            self._add_event(
                item_id,
                command.event,
                command.actor_type,
                command.actor_id,
                from_status,
                command.to_status,
                command.payload,
            )
            if command.to_status == "done":
                await self._record_dependency_satisfied(item_id)
            if from_status == "running" and command.to_status != "running":
                await self._finish_latest_attempt(item_id, command.to_status)
        return await self.get_work_item(item_id)

    async def add_event(self, item_id: str, command: EventCreate) -> EventView:
        async with self.session.begin():
            item = await self.session.get(WorkItem, item_id)
            if item is None:
                raise NotFoundError(f"work item not found: {item_id}")
            if item.status == "running":
                self._verify_claim(item, command.claim_token)
            event = self._add_event(
                item_id,
                command.event_type,
                command.actor_type,
                command.actor_id,
                item.status,
                item.status,
                command.payload,
            )
        return EventView.model_validate(event)

    async def list_events(self, item_id: str) -> list[EventView]:
        if await self.session.get(WorkItem, item_id) is None:
            raise NotFoundError(f"work item not found: {item_id}")
        events = (
            await self.session.scalars(
                select(WorkItemEvent)
                .where(WorkItemEvent.work_item_id == item_id)
                .order_by(WorkItemEvent.created_at, WorkItemEvent.id)
            )
        ).all()
        return [EventView.model_validate(event) for event in events]

    async def add_artifact(self, item_id: str, command: ArtifactCreate) -> ArtifactView:
        async with self.session.begin():
            item = await self.session.get(WorkItem, item_id)
            if item is None:
                raise NotFoundError(f"work item not found: {item_id}")
            if item.status == "running":
                self._verify_claim(item, command.claim_token)
            artifact = WorkItemArtifact(
                work_item_id=item_id,
                **command.model_dump(exclude={"claim_token"}),
            )
            self.session.add(artifact)
            await self.session.flush()
            self._add_event(
                item_id,
                "artifact_created",
                "agent",
                None,
                None,
                None,
                {"artifact_id": artifact.id, "path": artifact.path},
            )
        return ArtifactView.model_validate(artifact)

    async def decision(self, item_id: str, command: DecisionCommand) -> DecisionView:
        if command.action == "request":
            return await self._request_decision(item_id, command)
        return await self._resolve_decision(item_id, command)

    async def list_decisions(self, item_id: str) -> list[DecisionView]:
        if await self.session.get(WorkItem, item_id) is None:
            raise NotFoundError(f"work item not found: {item_id}")
        decisions = (
            await self.session.scalars(
                select(HumanDecision)
                .where(HumanDecision.work_item_id == item_id)
                .order_by(HumanDecision.created_at, HumanDecision.id)
            )
        ).all()
        return [DecisionView.model_validate(decision) for decision in decisions]

    async def maintenance_tick(self) -> MaintenanceResult:
        expired = 0
        readied = 0
        now = utc_now()
        async with self.session.begin():
            expired_ids = (
                await self.session.scalars(
                    select(WorkItem.id).where(
                        WorkItem.status == "running", WorkItem.claim_expires_at <= now
                    )
                )
            ).all()
            for item_id in expired_ids:
                result = await self.session.execute(
                    update(WorkItem)
                    .where(
                        WorkItem.id == item_id,
                        WorkItem.status == "running",
                        WorkItem.claim_expires_at <= now,
                    )
                    .values(
                        status="retry_queued",
                        version=WorkItem.version + 1,
                        claim_worker_id=None,
                        claim_token_hash=None,
                        claim_expires_at=None,
                        retry_at=now
                        + timedelta(seconds=self.settings.default_retry_delay_seconds),
                        updated_at=now,
                    )
                )
                if result.rowcount == 1:
                    expired += 1
                    self._add_event(
                        item_id,
                        "retry_scheduled",
                        "control_plane",
                        None,
                        "running",
                        "retry_queued",
                        {"reason": "lease_expired"},
                    )
                    await self._finish_latest_attempt(item_id, "lease_expired")

            incomplete = self._incomplete_dependency_exists()
            retry_ids = (
                await self.session.scalars(
                    select(WorkItem.id).where(
                        WorkItem.status == "retry_queued",
                        WorkItem.retry_at <= now,
                        ~incomplete,
                    )
                )
            ).all()
            for item_id in retry_ids:
                result = await self.session.execute(
                    update(WorkItem)
                    .where(
                        WorkItem.id == item_id,
                        WorkItem.status == "retry_queued",
                        WorkItem.retry_at <= now,
                    )
                    .values(
                        status="ready",
                        version=WorkItem.version + 1,
                        retry_at=None,
                        updated_at=now,
                    )
                )
                if result.rowcount == 1:
                    readied += 1
                    self._add_event(
                        item_id,
                        "retry_due",
                        "control_plane",
                        None,
                        "retry_queued",
                        "ready",
                        {},
                    )
        return MaintenanceResult(expired=expired, readied=readied)

    async def _request_decision(
        self, item_id: str, command: DecisionCommand
    ) -> DecisionView:
        async with self.session.begin():
            item = await self.session.get(WorkItem, item_id)
            if item is None:
                raise NotFoundError(f"work item not found: {item_id}")
            from_status = item.status
            if from_status == "running":
                event = "human_input_requested"
                self._verify_claim(item, command.claim_token)
            elif from_status == "stage_review":
                event = "human_review_requested"
            else:
                raise InvalidTransitionError(
                    f"cannot request human input while status={item.status}"
                )
            try:
                PROTOCOL.transition(from_status, "needs_human", event)
            except ValueError as error:
                raise InvalidTransitionError(str(error)) from error
            decision = HumanDecision(
                work_item_id=item_id,
                question=command.question,
                options=command.options,
                requested_by=command.actor_id,
            )
            self.session.add(decision)
            await self.session.flush()
            result = await self.session.execute(
                update(WorkItem)
                .where(
                    WorkItem.id == item_id,
                    WorkItem.status == from_status,
                    WorkItem.version == item.version,
                )
                .values(
                    status="needs_human",
                    version=WorkItem.version + 1,
                    claim_worker_id=None,
                    claim_token_hash=None,
                    claim_expires_at=None,
                    updated_at=utc_now(),
                )
            )
            if result.rowcount != 1:
                raise ConflictError("work item changed while requesting human input")
            self._add_event(
                item_id,
                event,
                "worker" if from_status == "running" else "policy",
                command.actor_id,
                from_status,
                "needs_human",
                {"decision_id": decision.id},
            )
            if from_status == "running":
                await self._finish_latest_attempt(item_id, "needs_human")
        return DecisionView.model_validate(decision)

    async def _resolve_decision(
        self, item_id: str, command: DecisionCommand
    ) -> DecisionView:
        async with self.session.begin():
            item = await self.session.get(WorkItem, item_id)
            if item is None:
                raise NotFoundError(f"work item not found: {item_id}")
            decision = await self.session.get(HumanDecision, command.decision_id)
            if decision is None or decision.work_item_id != item_id:
                raise NotFoundError("decision not found")
            if decision.status != "open":
                raise ConflictError("decision is already resolved")
            PROTOCOL.transition(item.status, "ready", "human_decision_resolved")
            if await self._has_incomplete_dependencies(item_id):
                raise ConflictError("dependencies are not done")
            decision.status = "resolved"
            decision.response = command.response
            decision.resolved_by = command.actor_id
            decision.resolved_at = utc_now()
            result = await self.session.execute(
                update(WorkItem)
                .where(
                    WorkItem.id == item_id,
                    WorkItem.status == "needs_human",
                    WorkItem.version == item.version,
                )
                .values(status="ready", version=WorkItem.version + 1, updated_at=utc_now())
            )
            if result.rowcount != 1:
                raise ConflictError("work item changed while resolving decision")
            self._add_event(
                item_id,
                "human_decision_resolved",
                "human",
                command.actor_id,
                "needs_human",
                "ready",
                {"decision_id": decision.id, "response": command.response},
            )
        return DecisionView.model_validate(decision)

    async def _validate_transition_guards(
        self, item: WorkItem, command: StatusTransitionRequest
    ) -> None:
        if command.to_status == "ready" and await self._has_incomplete_dependencies(item.id):
            raise InvalidTransitionError("dependencies are not done")
        if command.event == "agent_completed":
            handoff_path = f"orchestration/handoffs/{item.id}.yaml"
            handoff = await self.session.scalar(
                select(WorkItemArtifact.id).where(
                    WorkItemArtifact.work_item_id == item.id,
                    WorkItemArtifact.direction == "output",
                    WorkItemArtifact.path == handoff_path,
                )
            )
            if handoff is None:
                raise InvalidTransitionError(f"missing Handoff artifact: {handoff_path}")
        if command.to_status == "blocked" and not command.payload.get("blocker"):
            raise InvalidTransitionError("blocked transition requires payload.blocker")
        if command.event == "blocker_resolved" and not command.payload.get("resolution"):
            raise InvalidTransitionError("blocker_resolved requires payload.resolution")
        if command.event == "stage_rejected" and not command.payload.get("rework_reason"):
            raise InvalidTransitionError("stage_rejected requires payload.rework_reason")
        if command.event == "rework_queued" and not command.payload.get("rework_scope"):
            raise InvalidTransitionError("rework_queued requires payload.rework_scope")
        if command.event == "retry_due":
            if item.retry_at is None or _as_utc(item.retry_at) > utc_now():
                raise InvalidTransitionError("retry time has not been reached")
        if command.event in {"human_input_requested", "human_review_requested"}:
            open_decision = await self.session.scalar(
                select(HumanDecision.id).where(
                    HumanDecision.work_item_id == item.id,
                    HumanDecision.status == "open",
                )
            )
            if open_decision is None:
                raise InvalidTransitionError("human transition requires an open decision")
        if command.event == "human_decision_resolved":
            resolved_decision = await self.session.scalar(
                select(HumanDecision.id).where(
                    HumanDecision.work_item_id == item.id,
                    HumanDecision.status == "resolved",
                )
            )
            if resolved_decision is None:
                raise InvalidTransitionError("no resolved human decision is recorded")

    def _verify_claim(self, item: WorkItem, token: str | None) -> None:
        if not token or not item.claim_token_hash:
            raise ClaimError("claim token is required")
        if not hmac.compare_digest(item.claim_token_hash, _token_hash(token)):
            raise ClaimError("claim token is invalid")
        if item.claim_expires_at is None or _as_utc(item.claim_expires_at) <= utc_now():
            raise ClaimError("claim has expired")

    def _verify_transition_actor(self, expected: str, actual: str) -> None:
        allowed = {
            "worker": {"worker", "agent"},
            "control_plane": {"control_plane"},
            "human": {"human"},
            "admin": {"admin"},
            "policy": {"policy"},
            "policy_or_human": {"policy", "human"},
        }.get(expected, {expected})
        if actual not in allowed:
            raise InvalidTransitionError(
                f"transition requires actor {expected}, got {actual}"
            )

    async def _raise_claim_conflict(self, item_id: str, expected_version: int) -> None:
        item = await self.session.get(WorkItem, item_id)
        if item is None:
            raise NotFoundError(f"work item not found: {item_id}")
        if item.status != "ready":
            raise ClaimError(f"work item is {item.status}, expected ready")
        if item.version != expected_version:
            raise ClaimError(f"version is {item.version}, expected {expected_version}")
        if await self._has_incomplete_dependencies(item_id):
            raise ClaimError("work item has incomplete dependencies")
        raise ClaimError("work item could not be claimed")

    async def _require_dependencies(
        self, dependency_ids: list[str], feature_id: str
    ) -> list[WorkItem]:
        if not dependency_ids:
            return []
        items = (
            await self.session.scalars(select(WorkItem).where(WorkItem.id.in_(dependency_ids)))
        ).all()
        if len(items) != len(set(dependency_ids)):
            found = {item.id for item in items}
            raise NotFoundError(f"dependencies not found: {sorted(set(dependency_ids) - found)}")
        if any(item.feature_id != feature_id for item in items):
            raise ConflictError("dependencies must belong to the same feature")
        return list(items)

    async def _would_create_dependency_cycle(
        self, item_id: str, proposed_dependencies: list[str]
    ) -> bool:
        pairs = (await self.session.execute(select(
            WorkItemDependency.work_item_id,
            WorkItemDependency.depends_on_id,
        ))).all()
        graph: dict[str, set[str]] = {}
        for source, target in pairs:
            if source != item_id:
                graph.setdefault(source, set()).add(target)
        graph[item_id] = set(proposed_dependencies)

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> bool:
            if node in visiting:
                return True
            if node in visited:
                return False
            visiting.add(node)
            if any(visit(target) for target in graph.get(node, set())):
                return True
            visiting.remove(node)
            visited.add(node)
            return False

        return any(visit(node) for node in graph)

    def _incomplete_dependency_exists(self):
        dependency = aliased(WorkItemDependency)
        prerequisite = aliased(WorkItem)
        return exists(
            select(1)
            .select_from(dependency)
            .join(prerequisite, prerequisite.id == dependency.depends_on_id)
            .where(
                dependency.work_item_id == WorkItem.id,
                prerequisite.status != "done",
            )
        )

    async def _has_incomplete_dependencies(self, item_id: str) -> bool:
        dependency = aliased(WorkItemDependency)
        prerequisite = aliased(WorkItem)
        value = await self.session.scalar(
            select(
                exists(
                    select(1)
                    .select_from(dependency)
                    .join(prerequisite, prerequisite.id == dependency.depends_on_id)
                    .where(
                        dependency.work_item_id == item_id,
                        prerequisite.status != "done",
                    )
                )
            )
        )
        return bool(value)

    def _add_artifacts(
        self, item_id: str, direction: str, artifacts: list[ArtifactData]
    ) -> None:
        self.session.add_all(
            WorkItemArtifact(work_item_id=item_id, direction=direction, **artifact.model_dump())
            for artifact in artifacts
        )

    def _add_event(
        self,
        item_id: str,
        event_type: str,
        actor_type: str,
        actor_id: str | None,
        from_status: str | None,
        to_status: str | None,
        payload: dict[str, Any],
    ) -> WorkItemEvent:
        event = WorkItemEvent(
            work_item_id=item_id,
            event_type=event_type,
            actor_type=actor_type,
            actor_id=actor_id,
            from_status=from_status,
            to_status=to_status,
            payload=payload,
        )
        self.session.add(event)
        return event

    async def _record_dependency_satisfied(self, completed_id: str) -> None:
        dependent_ids = (
            await self.session.scalars(
                select(WorkItemDependency.work_item_id).where(
                    WorkItemDependency.depends_on_id == completed_id
                )
            )
        ).all()
        for dependent_id in dependent_ids:
            self._add_event(
                dependent_id,
                "dependency_satisfied",
                "control_plane",
                None,
                None,
                None,
                {"dependency_id": completed_id},
            )

    async def _finish_latest_attempt(self, item_id: str, status: str) -> None:
        attempt = await self.session.scalar(
            select(AgentAttempt)
            .where(AgentAttempt.work_item_id == item_id, AgentAttempt.completed_at.is_(None))
            .order_by(AgentAttempt.attempt_number.desc())
            .limit(1)
        )
        if attempt:
            attempt.status = status
            attempt.completed_at = utc_now()

    async def _view(self, item: WorkItem) -> WorkItemView:
        dependencies = list(
            (
                await self.session.scalars(
                    select(WorkItemDependency.depends_on_id)
                    .where(WorkItemDependency.work_item_id == item.id)
                    .order_by(WorkItemDependency.depends_on_id)
                )
            ).all()
        )
        prerequisite = aliased(WorkItem)
        blocked_by = list(
            (
                await self.session.scalars(
                    select(WorkItemDependency.depends_on_id)
                    .join(prerequisite, prerequisite.id == WorkItemDependency.depends_on_id)
                    .where(
                        WorkItemDependency.work_item_id == item.id,
                        prerequisite.status != "done",
                    )
                    .order_by(WorkItemDependency.depends_on_id)
                )
            ).all()
        )
        artifacts = (
            await self.session.scalars(
                select(WorkItemArtifact)
                .where(WorkItemArtifact.work_item_id == item.id)
                .order_by(WorkItemArtifact.created_at, WorkItemArtifact.id)
            )
        ).all()
        inputs = [ArtifactData.model_validate(value) for value in artifacts if value.direction == "input"]
        outputs = [
            ArtifactData.model_validate(value) for value in artifacts if value.direction == "output"
        ]
        return WorkItemView(
            id=item.id,
            feature_id=item.feature_id,
            parent_id=item.parent_id,
            title=item.title,
            description=item.description,
            stage=item.stage,
            agent_role=item.agent_role,
            status=item.status,
            priority=item.priority,
            version=item.version,
            repository=RepositoryData.model_validate(item.repository),
            dependencies=dependencies,
            blocked_by=blocked_by,
            input_artifacts=inputs,
            output_artifacts=outputs,
            acceptance_criteria=item.acceptance_criteria,
            blocker=item.blocker,
            claim=ClaimView(
                worker_id=item.claim_worker_id,
                expires_at=item.claim_expires_at,
            ),
            retry_at=item.retry_at,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )
