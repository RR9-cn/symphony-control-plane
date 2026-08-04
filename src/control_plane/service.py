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
    Worker,
    WorkItem,
    WorkItemArtifact,
    WorkItemDependency,
    WorkItemEvent,
    utc_now,
)
from control_plane.protocol import PROTOCOL
from control_plane.schemas import (
    AgentAttemptView,
    AgentRuntimeView,
    AgentProfileView,
    AttemptContextUpdate,
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
    ManualIssueCreate,
    ManualIssuePreview,
    ManualIssueResult,
    MaintenanceResult,
    ReleaseRequest,
    RepositoryData,
    StatusTransitionRequest,
    WorkItemCreate,
    WorkItemPatch,
    WorkItemView,
    WorkerHeartbeat,
    WorkerRegistration,
    WorkerView,
)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


_MANUAL_ISSUE_STAGES = (
    (
        "solution_architect",
        "tech_analysis",
        "技术分析",
        "分析需求、现有实现、接口、数据流和风险，生成结构化技术交接。",
        "技术分析与 Handoff 已登记，公开接口、兼容性和安全边界清晰。",
    ),
    (
        "backend_builder",
        "implementation",
        "后端实现",
        "按照已批准技术分析实现需求、补充测试并登记代码交接。",
        "实现满足 Issue 验收标准，相关构建、检查和测试已通过。",
    ),
    (
        "code_reviewer",
        "code_review",
        "代码评审",
        "从 Standards 与 Spec 两个维度只读评审实现并登记发现。",
        "评审报告包含严重级别、证据和处置结论，HIGH 发现已退回。",
    ),
    (
        "test_designer",
        "test_design",
        "测试方案",
        "基于需求、实现和评审结果设计可执行测试方案。",
        "测试方案覆盖验收标准、边界、失败路径和回归场景。",
    ),
    (
        "test_executor",
        "test_execution",
        "测试执行",
        "严格执行测试方案，记录结果、证据和最终交接。",
        "全部测试场景具有结果和证据，业务缺陷已按流程退回。",
    ),
)


_RUNTIME_STATE = {
    "draft": "waiting_dependency",
    "ready": "ready",
    "running": "running",
    "needs_human": "waiting_human",
    "stage_review": "reviewing",
    "rework": "rework",
    "retry_queued": "retrying",
    "blocked": "blocked",
    "done": "completed",
    "cancelled": "cancelled",
}


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

    async def list_features(self) -> list[FeatureView]:
        features = (
            await self.session.scalars(
                select(Feature).order_by(Feature.updated_at.desc(), Feature.id)
            )
        ).all()
        return [FeatureView.model_validate(feature) for feature in features]

    async def preview_manual_issue(
        self, command: ManualIssueCreate
    ) -> ManualIssuePreview:
        feature = FeatureCreate(
            id=command.feature_id,
            title=command.title,
            description=command.description,
        )
        feature_number = command.feature_id.removeprefix("FEATURE-")
        work_items: list[WorkItemCreate] = []
        previous_id: str | None = None
        for index, (role, stage, label, description, stage_criterion) in enumerate(
            _MANUAL_ISSUE_STAGES, start=1
        ):
            item_id = f"WI-{feature_number}{index:02d}"
            criteria = [*command.acceptance_criteria, stage_criterion]
            work_items.append(
                WorkItemCreate(
                    id=item_id,
                    feature_id=command.feature_id,
                    title=f"{label}：{command.title}",
                    description=f"{description}\n\n原始 Issue：{command.description}",
                    stage=stage,
                    agent_role=role,
                    status="ready" if index == 1 else "draft",
                    priority=command.priority,
                    repository=command.repository,
                    dependencies=[previous_id] if previous_id else [],
                    acceptance_criteria=criteria,
                )
            )
            previous_id = item_id
        return ManualIssuePreview(feature=feature, work_items=work_items)

    async def create_manual_issue(
        self, command: ManualIssueCreate
    ) -> ManualIssueResult:
        plan = await self.preview_manual_issue(command)
        item_ids = [item.id for item in plan.work_items]
        async with self.session.begin():
            if await self.session.get(Feature, plan.feature.id):
                raise ConflictError(f"feature already exists: {plan.feature.id}")
            existing_ids = (
                await self.session.scalars(
                    select(WorkItem.id).where(WorkItem.id.in_(item_ids))
                )
            ).all()
            if existing_ids:
                raise ConflictError(
                    f"generated work item already exists: {sorted(existing_ids)[0]}"
                )

            feature = Feature(**plan.feature.model_dump())
            self.session.add(feature)
            await self.session.flush()

            for command_item in plan.work_items:
                self.session.add(
                    WorkItem(
                        id=command_item.id,
                        feature_id=command_item.feature_id,
                        parent_id=None,
                        title=command_item.title,
                        description=command_item.description,
                        stage=command_item.stage,
                        agent_role=command_item.agent_role,
                        status=command_item.status,
                        priority=command_item.priority,
                        version=1,
                        repository=command_item.repository.model_dump(),
                        acceptance_criteria=command_item.acceptance_criteria,
                    )
                )
            await self.session.flush()

            for command_item in plan.work_items:
                self.session.add_all(
                    WorkItemDependency(
                        work_item_id=command_item.id,
                        depends_on_id=dependency_id,
                    )
                    for dependency_id in command_item.dependencies
                )
                self._add_event(
                    command_item.id,
                    "created",
                    "user",
                    "manual-issue-intake",
                    None,
                    command_item.status,
                    {
                        "version": 1,
                        "source": "manual_issue",
                        "template": plan.template,
                    },
                )

        return ManualIssueResult(
            feature=FeatureView.model_validate(feature),
            work_items=[await self.get_work_item(item_id) for item_id in item_ids],
        )

    async def create_work_item(self, command: WorkItemCreate) -> WorkItemView:
        async with self.session.begin():
            if await self.session.get(WorkItem, command.id):
                raise ConflictError(f"work item already exists: {command.id}")
            if await self.session.get(Feature, command.feature_id) is None:
                raise NotFoundError(f"feature not found: {command.feature_id}")
            if (
                command.parent_id
                and await self.session.get(WorkItem, command.parent_id) is None
            ):
                raise NotFoundError(f"parent work item not found: {command.parent_id}")
            dependencies = await self._require_dependencies(
                command.dependencies, command.feature_id
            )
            if command.id in command.dependencies:
                raise ConflictError("work item cannot depend on itself")
            if command.status == "ready" and any(
                item.status != "done" for item in dependencies
            ):
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

    async def patch_work_item(
        self, item_id: str, command: WorkItemPatch
    ) -> WorkItemView:
        async with self.session.begin():
            current = await self.session.get(WorkItem, item_id)
            if current is None:
                raise NotFoundError(f"work item not found: {item_id}")
            changes = command.model_dump(
                exclude={"expected_version", "dependencies"}, exclude_none=True
            )
            if "repository" in changes:
                changes["repository"] = command.repository.model_dump()  # type: ignore[union-attr]
            if command.dependencies is not None:
                if current.status != "draft":
                    raise ConflictError(
                        "dependencies may only be changed while status=draft"
                    )
                if item_id in command.dependencies:
                    raise ConflictError("work item cannot depend on itself")
                await self._require_dependencies(
                    command.dependencies, current.feature_id
                )
                if await self._would_create_dependency_cycle(
                    item_id, command.dependencies
                ):
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
                    delete(WorkItemDependency).where(
                        WorkItemDependency.work_item_id == item_id
                    )
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
            previous_attempt = await self.session.scalar(
                select(AgentAttempt)
                .where(AgentAttempt.work_item_id == item_id)
                .order_by(AgentAttempt.attempt_number.desc())
                .limit(1)
            )
            attempt_number = (
                previous_attempt.attempt_number + 1
                if previous_attempt is not None
                else 1
            )
            resume_thread_id: str | None = None
            continuation_turn_count = 0
            if (
                previous_attempt is not None
                and previous_attempt.status
                in {"retry_queued", "stage_review", "needs_human"}
                and previous_attempt.thread_id
                and command.profile is not None
                and previous_attempt.profile_snapshot.get("profile_name")
                == command.profile.name
                and previous_attempt.profile_snapshot.get("profile_version")
                == command.profile.version
            ):
                resume_thread_id = previous_attempt.thread_id
                continuation_turn_count = int(
                    await self.session.scalar(
                        select(func.count(AgentAttempt.id)).where(
                            AgentAttempt.work_item_id == item_id,
                            AgentAttempt.thread_id == resume_thread_id,
                            AgentAttempt.status == "retry_queued",
                        )
                    )
                    or 0
                )
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
                    "profile_version": command.profile.version
                    if command.profile
                    else None,
                },
            )
        return ClaimResult(
            work_item=await self.get_work_item(item_id),
            claim_token=token,
            attempt=AgentAttemptView.model_validate(attempt),
            resume_thread_id=resume_thread_id,
            continuation_turn_count=continuation_turn_count,
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

    async def register_worker(self, command: WorkerRegistration) -> WorkerView:
        now = utc_now()
        async with self.session.begin():
            worker = await self.session.get(Worker, command.worker_id)
            if worker is None:
                worker = Worker(
                    id=command.worker_id,
                    hostname=command.hostname,
                    process_id=command.process_id,
                    version=command.version,
                    capacity=command.capacity,
                    profiles=command.profiles,
                    active_work_items=[],
                    active_profiles={},
                    state="starting",
                    started_at=now,
                    last_seen_at=now,
                )
                self.session.add(worker)
            else:
                worker.hostname = command.hostname
                worker.process_id = command.process_id
                worker.version = command.version
                worker.capacity = command.capacity
                worker.profiles = command.profiles
                worker.active_work_items = []
                worker.active_profiles = {}
                worker.state = "starting"
                worker.stop_requested = False
                worker.started_at = now
                worker.last_seen_at = now
                worker.stopped_at = None
        return self._worker_view(worker, now)

    async def heartbeat_worker(
        self, worker_id: str, command: WorkerHeartbeat
    ) -> WorkerView:
        now = utc_now()
        async with self.session.begin():
            worker = await self.session.get(Worker, worker_id)
            if worker is None:
                raise NotFoundError(f"worker not found: {worker_id}")
            worker.state = command.state
            worker.active_work_items = command.active_work_items
            worker.active_profiles = command.active_profiles
            worker.last_seen_at = now
        return self._worker_view(worker, now)

    async def stop_worker(self, worker_id: str) -> WorkerView:
        now = utc_now()
        async with self.session.begin():
            worker = await self.session.get(Worker, worker_id)
            if worker is None:
                raise NotFoundError(f"worker not found: {worker_id}")
            worker.state = "stopped"
            worker.active_work_items = []
            worker.active_profiles = {}
            worker.last_seen_at = now
            worker.stopped_at = now
            worker.stop_requested = False
        return self._worker_view(worker, now)

    async def request_worker_stop(self, worker_id: str) -> WorkerView:
        now = utc_now()
        async with self.session.begin():
            worker = await self.session.get(Worker, worker_id)
            if worker is None:
                raise NotFoundError(f"worker not found: {worker_id}")
            if self._worker_state(worker, now) in {"offline", "stopped"}:
                raise ConflictError(f"worker is not running: {worker_id}")
            worker.stop_requested = True
        return self._worker_view(worker, now)

    async def list_workers(self) -> list[WorkerView]:
        now = utc_now()
        workers = (
            await self.session.scalars(
                select(Worker).order_by(Worker.last_seen_at.desc(), Worker.id)
            )
        ).all()
        return [self._worker_view(worker, now) for worker in workers]

    async def list_agent_runtimes(
        self, feature_id: str | None = None
    ) -> list[AgentRuntimeView]:
        query = select(WorkItem).order_by(WorkItem.created_at, WorkItem.id)
        if feature_id is not None:
            query = query.where(WorkItem.feature_id == feature_id)
        items = (await self.session.scalars(query)).all()
        if not items:
            return []
        attempts = (
            await self.session.scalars(
                select(AgentAttempt)
                .where(AgentAttempt.work_item_id.in_([item.id for item in items]))
                .order_by(
                    AgentAttempt.work_item_id,
                    AgentAttempt.attempt_number.desc(),
                )
            )
        ).all()
        latest: dict[str, AgentAttempt] = {}
        for attempt in attempts:
            latest.setdefault(attempt.work_item_id, attempt)
        result: list[AgentRuntimeView] = []
        for item in items:
            latest_attempt = latest.get(item.id)
            state = _RUNTIME_STATE.get(item.status, item.status)
            if (
                item.status == "running"
                and latest_attempt is not None
                and not latest_attempt.thread_id
            ):
                state = "starting"
            snapshot = (
                latest_attempt.profile_snapshot if latest_attempt is not None else {}
            )
            result.append(
                AgentRuntimeView(
                    work_item_id=item.id,
                    feature_id=item.feature_id,
                    title=item.title,
                    agent_role=item.agent_role,
                    stage=item.stage,
                    state=state,
                    worker_id=item.claim_worker_id
                    or (
                        latest_attempt.worker_id
                        if latest_attempt is not None
                        else None
                    ),
                    attempt_id=latest_attempt.id if latest_attempt is not None else None,
                    attempt_number=(
                        latest_attempt.attempt_number
                        if latest_attempt is not None
                        else None
                    ),
                    profile_name=snapshot.get("profile_name"),
                    profile_version=snapshot.get("profile_version"),
                    thread_id=(
                        latest_attempt.thread_id
                        if latest_attempt is not None
                        else None
                    ),
                    turn_id=(
                        latest_attempt.turn_id
                        if latest_attempt is not None
                        else None
                    ),
                    started_at=(
                        latest_attempt.started_at
                        if latest_attempt is not None
                        else None
                    ),
                    updated_at=item.updated_at,
                )
            )
        return result

    async def update_attempt_context(
        self, item_id: str, command: AttemptContextUpdate
    ) -> AgentAttemptView:
        async with self.session.begin():
            item = await self.session.get(WorkItem, item_id)
            if item is None:
                raise NotFoundError(f"work item not found: {item_id}")
            self._verify_claim(item, command.claim_token)
            attempt = await self.session.scalar(
                select(AgentAttempt)
                .where(
                    AgentAttempt.work_item_id == item_id,
                    AgentAttempt.completed_at.is_(None),
                )
                .order_by(AgentAttempt.attempt_number.desc())
                .limit(1)
            )
            if attempt is None:
                raise ConflictError("running work item has no active attempt")
            attempt.thread_id = command.thread_id
            if command.turn_id is not None:
                attempt.turn_id = command.turn_id
        return AgentAttemptView.model_validate(attempt)

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
                raise ClaimError(
                    "claim is missing, expired, or owned by another worker"
                )
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
        if command.thread_id is not None:
            payload["thread_id"] = command.thread_id
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
                definition = PROTOCOL.transition(
                    from_status, command.to_status, command.event
                )
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
                values["retry_at"] = utc_now() + timedelta(seconds=retry_delay)
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
                await self._finish_latest_attempt(
                    item_id,
                    command.to_status,
                    command.payload.get("thread_id"),
                )
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
                await self._finish_latest_attempt(
                    item_id, "needs_human", command.thread_id
                )
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
                .values(
                    status="ready", version=WorkItem.version + 1, updated_at=utc_now()
                )
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
        if command.to_status == "ready" and await self._has_incomplete_dependencies(
            item.id
        ):
            raise InvalidTransitionError("dependencies are not done")
        if command.event == "agent_completed":
            handoff_path = f"orchestration/handoffs/{item.id}.yaml"
            artifact_paths = (
                await self.session.scalars(
                    select(WorkItemArtifact.path).where(
                        WorkItemArtifact.work_item_id == item.id,
                        WorkItemArtifact.direction == "output",
                    )
                )
            ).all()
            nested_suffix = f"/{handoff_path}"
            has_handoff = any(
                path == handoff_path
                or (
                    path.endswith(nested_suffix)
                    and "\\" not in path
                    and ".." not in path.split("/")
                )
                for path in artifact_paths
            )
            if not has_handoff:
                raise InvalidTransitionError(
                    f"missing Handoff artifact: {handoff_path}"
                )
        if command.to_status == "blocked" and not command.payload.get("blocker"):
            raise InvalidTransitionError("blocked transition requires payload.blocker")
        if command.event == "blocker_resolved" and not command.payload.get(
            "resolution"
        ):
            raise InvalidTransitionError("blocker_resolved requires payload.resolution")
        if command.event == "stage_rejected" and not command.payload.get(
            "rework_reason"
        ):
            raise InvalidTransitionError(
                "stage_rejected requires payload.rework_reason"
            )
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
                raise InvalidTransitionError(
                    "human transition requires an open decision"
                )
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
            await self.session.scalars(
                select(WorkItem).where(WorkItem.id.in_(dependency_ids))
            )
        ).all()
        if len(items) != len(set(dependency_ids)):
            found = {item.id for item in items}
            raise NotFoundError(
                f"dependencies not found: {sorted(set(dependency_ids) - found)}"
            )
        if any(item.feature_id != feature_id for item in items):
            raise ConflictError("dependencies must belong to the same feature")
        return list(items)

    async def _would_create_dependency_cycle(
        self, item_id: str, proposed_dependencies: list[str]
    ) -> bool:
        pairs = (
            await self.session.execute(
                select(
                    WorkItemDependency.work_item_id,
                    WorkItemDependency.depends_on_id,
                )
            )
        ).all()
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
            WorkItemArtifact(
                work_item_id=item_id, direction=direction, **artifact.model_dump()
            )
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

    async def _finish_latest_attempt(
        self,
        item_id: str,
        status: str,
        thread_id: object = None,
    ) -> None:
        attempt = await self.session.scalar(
            select(AgentAttempt)
            .where(
                AgentAttempt.work_item_id == item_id,
                AgentAttempt.completed_at.is_(None),
            )
            .order_by(AgentAttempt.attempt_number.desc())
            .limit(1)
        )
        if attempt:
            attempt.status = status
            if isinstance(thread_id, str) and thread_id:
                attempt.thread_id = thread_id
            attempt.completed_at = utc_now()

    def _worker_state(self, worker: Worker, now: datetime) -> str:
        if worker.state == "stopped":
            return "stopped"
        age = (now - _as_utc(worker.last_seen_at)).total_seconds()
        if age > self.settings.worker_offline_after_seconds:
            return "offline"
        return worker.state

    def _worker_view(self, worker: Worker, now: datetime) -> WorkerView:
        return WorkerView.model_validate(worker).model_copy(
            update={"state": self._worker_state(worker, now)}
        )

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
                    .join(
                        prerequisite,
                        prerequisite.id == WorkItemDependency.depends_on_id,
                    )
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
        inputs = [
            ArtifactData.model_validate(value)
            for value in artifacts
            if value.direction == "input"
        ]
        outputs = [
            ArtifactData.model_validate(value)
            for value in artifacts
            if value.direction == "output"
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
