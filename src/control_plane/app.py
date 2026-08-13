import asyncio
import contextlib
import hashlib
import hmac
import mimetypes
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Annotated, AsyncIterator

from fastapi import Depends, FastAPI, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.config import Settings
from control_plane.database import Database
from control_plane.errors import ConflictError, ControlPlaneError, NotFoundError
from control_plane.models import Issue, Project
from control_plane.project_service import ProjectService
from control_plane.runner_supervisor import RunnerSupervisor
from control_plane.schemas import (
    AgentAttemptEventCreate, AgentAttemptEventView, AgentAttemptView, AgentRuntimeView,
    ArtifactContentView, ArtifactCreate, ArtifactView, AttemptContextUpdate, ClaimRequest, ClaimResult,
    DecisionCommand, DecisionView, EventCreate, EventView, HeartbeatRequest,
    IssueArchiveCommand, IssueContinueCommand, IssueCreate, IssueDeliveryCommand, IssuePatch, IssueView, MaintenanceResult,
    ProjectCreate, ProjectPatch, ProjectRuntimeView, ProjectView,
    ReleaseRequest, RunnerControlView, WorkflowSnapshotView, WorkspaceReviewView,
    StatusTransitionRequest, WorkerHeartbeat, WorkerRegistration, WorkerView,
)
from control_plane.service import ControlPlaneService
from control_plane.workspace_summary import build_change_summary


UI_ROOT = Path(__file__).with_name("ui")
ARTIFACT_PREVIEW_LIMIT = 512 * 1024
DIFF_PREVIEW_LIMIT = 256 * 1024
TEXT_MEDIA_TYPES = {
    "application/json",
    "application/sql",
    "application/xml",
    "application/yaml",
}


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _artifact_file(issue: IssueView, artifact: ArtifactView) -> Path:
    relative = PurePosixPath(artifact.path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ConflictError("artifact path is not safe to preview")
    try:
        workspace = Path(issue.workspace_path).resolve(strict=True)
        target = workspace.joinpath(*relative.parts).resolve(strict=True)
    except OSError as error:
        raise NotFoundError("artifact file is unavailable in the Issue workspace") from error
    if not target.is_relative_to(workspace) or not target.is_file():
        raise NotFoundError("artifact file is unavailable in the Issue workspace")
    return target


async def _git_output(workspace: Path, *arguments: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "git", "-C", str(workspace), *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        # Cold worktrees on Windows can spend noticeable time warming the Git index
        # (especially while antivirus scans newly-created files). Keep the request
        # bounded, but allow enough headroom for larger repositories.
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    except TimeoutError as error:
        process.kill()
        await process.wait()
        raise ConflictError("workspace Git review timed out") from error
    if process.returncode != 0:
        raise ConflictError(
            stderr.decode("utf-8", errors="replace").strip()
            or "workspace Git review failed"
        )
    return stdout.decode("utf-8", errors="replace").rstrip()


async def _workspace_head(workspace: Path) -> str:
    """Resolve HEAD without allowing Git to fall back to a parent repository."""
    if not (workspace / ".git").exists():
        raise ConflictError(
            "Issue workspace Git checkout is incomplete; wait for initialization or retry the Issue"
        )
    try:
        top_level = Path(
            await _git_output(workspace, "rev-parse", "--show-toplevel")
        ).resolve()
        head = await _git_output(
            workspace, "rev-parse", "--verify", "HEAD^{commit}"
        )
    except ConflictError as error:
        raise ConflictError(
            "Issue workspace Git checkout is incomplete; wait for initialization or retry the Issue"
        ) from error
    if top_level != workspace.resolve():
        raise ConflictError("Issue workspace Git repository root does not match its path")
    return head


def _standard_runtime_state(
    workers: list[WorkerView],
    *,
    offline_after_seconds: float,
) -> tuple[dict[str, object] | None, str | None]:
    now = datetime.now(timezone.utc)
    fresh = [
        worker
        for worker in workers
        if worker.runtime_snapshot_at is not None
        and (now - _utc(worker.runtime_snapshot_at)).total_seconds()
        <= offline_after_seconds
        and worker.runtime_snapshot
    ]
    if not fresh:
        return None, "timeout" if workers else "unavailable"
    fresh.sort(
        key=lambda worker: _utc(worker.runtime_snapshot_at),  # type: ignore[arg-type]
        reverse=True,
    )
    running_by_issue: dict[str, dict[str, object]] = {}
    retrying_by_issue: dict[str, dict[str, object]] = {}
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "seconds_running": 0.0,
    }
    latest_rate_limits: object = None
    for worker in fresh:
        snapshot = worker.runtime_snapshot
        for row in snapshot.get("running", []):
            if isinstance(row, dict) and row.get("issue_id"):
                running_by_issue.setdefault(str(row["issue_id"]), row)
        for row in snapshot.get("retrying", []):
            if isinstance(row, dict) and row.get("issue_id"):
                retrying_by_issue.setdefault(str(row["issue_id"]), row)
        codex_totals = snapshot.get("codex_totals")
        if isinstance(codex_totals, dict):
            for field in ("input_tokens", "output_tokens", "total_tokens"):
                value = codex_totals.get(field)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals[field] += int(value)
            seconds = codex_totals.get("seconds_running")
            if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
                totals["seconds_running"] += float(seconds)
        if latest_rate_limits is None and snapshot.get("rate_limits") is not None:
            latest_rate_limits = snapshot["rate_limits"]
    running = list(running_by_issue.values())
    retrying = list(retrying_by_issue.values())
    return (
        {
            "generated_at": now.isoformat(),
            "counts": {"running": len(running), "retrying": len(retrying)},
            "running": running,
            "retrying": retrying,
            "codex_totals": totals,
            "rate_limits": latest_rate_limits,
        },
        None,
    )


async def _lease_sweeper(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(app.state.settings.lease_sweep_interval_seconds)
        try:
            async with app.state.database.sessions() as session:
                await ControlPlaneService(session, app.state.settings).maintenance_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            app.state.lease_sweeper_failures += 1


async def _refresh_projects_once(app: FastAPI) -> None:
    async with app.state.database.sessions() as session:
        project_ids = list(
            await session.scalars(
                select(Project.id).where(Project.enabled.is_(True))
            )
        )
    for project_id in project_ids:
        try:
            async with app.state.database.sessions() as session:
                await ProjectService(
                    session,
                    app.state.settings.api_token.get_secret_value()
                    if app.state.settings.api_token
                    else None,
                ).refresh_if_changed(project_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            app.state.project_refresh_failures += 1


async def _project_refresher(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(max(2.0, app.state.settings.lease_sweep_interval_seconds))
        try:
            await _refresh_projects_once(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            app.state.project_refresh_failures += 1


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    resolved_settings = settings or Settings()
    resolved_database = database or Database(resolved_settings)
    runner_supervisor = RunnerSupervisor(resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(_lease_sweeper(app)) if resolved_settings.enable_lease_sweeper else None
        project_task = asyncio.create_task(_project_refresher(app))
        if resolved_settings.managed_runner_autostart:
            async with resolved_database.sessions() as session:
                projects = (await session.scalars(select(Project).where(Project.enabled.is_(True), Project.status == "available"))).all()
            for project in projects:
                with contextlib.suppress(ControlPlaneError):
                    await runner_supervisor.start_project(project)
        try:
            yield
        finally:
            await runner_supervisor.stop()
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            project_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await project_task
            await resolved_database.dispose()

    app = FastAPI(title="Symphony Control Plane", version="0.2.0", lifespan=lifespan)
    app.state.settings = resolved_settings
    app.state.database = resolved_database
    app.state.lease_sweeper_failures = 0
    app.state.project_refresh_failures = 0
    app.state.runner_supervisor = runner_supervisor

    @app.middleware("http")
    async def authenticate_api(request: Request, call_next):  # type: ignore[no-untyped-def]
        configured = resolved_settings.api_token
        if configured is not None and request.url.path.startswith("/api/"):
            expected = f"Bearer {configured.get_secret_value()}"
            if not hmac.compare_digest(request.headers.get("authorization", ""), expected):
                return JSONResponse(status_code=401, content={"error": {"code": "unauthorized", "message": "A valid Control Plane bearer token is required."}}, headers={"WWW-Authenticate": "Bearer"})
        return await call_next(request)

    @app.exception_handler(ControlPlaneError)
    async def handle_error(_request: Request, error: ControlPlaneError) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, content={"error": {"code": error.code, "message": str(error)}})

    async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
        async with request.app.state.database.sessions() as session:
            yield session

    Session = Annotated[AsyncSession, Depends(get_session)]
    service = lambda session: ControlPlaneService(session, resolved_settings)  # noqa: E731
    project_service = lambda session: ProjectService(  # noqa: E731
        session,
        resolved_settings.api_token.get_secret_value() if resolved_settings.api_token else None,
    )

    app.mount("/ui/assets", StaticFiles(directory=UI_ROOT), name="ui-assets")

    @app.get("/", include_in_schema=False, response_class=FileResponse)
    async def dashboard() -> FileResponse:
        return FileResponse(UI_ROOT / "index.html")

    @app.get("/ui", include_in_schema=False, response_class=FileResponse)
    async def dashboard_alias() -> FileResponse:
        return FileResponse(UI_ROOT / "index.html")

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "database": "sqlite", "auth_enabled": resolved_settings.api_token is not None, "lease_sweeper_failures": app.state.lease_sweeper_failures, "project_refresh_failures": app.state.project_refresh_failures, "managed_runner": runner_supervisor.view().state}

    @app.post("/api/issues", response_model=IssueView, status_code=status.HTTP_201_CREATED)
    async def create_issue(command: IssueCreate, session: Session) -> IssueView:
        # Bind new work to the latest validated local default-branch commit,
        # rather than whichever snapshot the background refresher last reached.
        async with app.state.database.sessions() as refresh_session:
            await ProjectService(
                refresh_session,
                resolved_settings.api_token.get_secret_value()
                if resolved_settings.api_token
                else None,
            ).refresh_if_changed(command.project_id)
        return await service(session).create_issue(command)

    @app.get("/api/issues", response_model=list[IssueView])
    async def list_issues(session: Session, statuses: list[str] | None = Query(default=None, alias="state"), issue_ids: list[str] | None = Query(default=None, alias="id"), project_id: str | None = None) -> list[IssueView]:
        return await service(session).list_issues(statuses, issue_ids, project_id)

    @app.get("/api/issues/candidates", response_model=list[IssueView])
    async def candidates(session: Session, project_id: str, limit: int = Query(default=100, ge=1, le=500)) -> list[IssueView]:
        return await service(session).candidates(project_id, limit)

    @app.get("/api/issues/{issue_id}", response_model=IssueView)
    async def get_issue(issue_id: str, session: Session) -> IssueView:
        return await service(session).get_issue(issue_id)

    @app.patch("/api/issues/{issue_id}", response_model=IssueView)
    async def patch_issue(issue_id: str, command: IssuePatch, session: Session) -> IssueView:
        return await service(session).patch_issue(issue_id, command)

    @app.post("/api/issues/{issue_id}/delivery", response_model=IssueView)
    async def deliver_issue(issue_id: str, command: IssueDeliveryCommand, session: Session) -> IssueView:
        return await service(session).deliver_issue(issue_id, command)

    @app.post("/api/issues/{issue_id}/continue", response_model=IssueView)
    async def continue_issue(
        issue_id: str, command: IssueContinueCommand, session: Session
    ) -> IssueView:
        return await service(session).continue_issue(issue_id, command)

    @app.post("/api/issues/{issue_id}/archive", response_model=IssueView)
    async def archive_issue(
        issue_id: str, command: IssueArchiveCommand, session: Session
    ) -> IssueView:
        return await service(session).archive_issue(issue_id, command)

    @app.post("/api/projects", response_model=ProjectView, status_code=status.HTTP_201_CREATED)
    async def create_project(command: ProjectCreate, session: Session) -> ProjectView:
        result = await project_service(session).create(command)
        if resolved_settings.managed_runner_autostart and result.enabled and result.status == "available":
            project = await session.get(Project, result.id)
            if project is not None:
                with contextlib.suppress(ControlPlaneError):
                    await runner_supervisor.start_project(project)
        return result

    @app.get("/api/projects", response_model=list[ProjectView])
    async def list_projects(session: Session) -> list[ProjectView]:
        return await project_service(session).list()

    @app.get("/api/projects/{project_id}", response_model=ProjectView)
    async def get_project(project_id: str, session: Session) -> ProjectView:
        return await project_service(session).get(project_id)

    @app.patch("/api/projects/{project_id}", response_model=ProjectView)
    async def patch_project(project_id: str, command: ProjectPatch, session: Session) -> ProjectView:
        restart = command.repository_path is not None or command.workflow_path is not None
        if restart or command.enabled is False:
            workers = await service(session).list_workers(project_id)
            await session.rollback()
            for worker in workers:
                with contextlib.suppress(ControlPlaneError):
                    await service(session).request_worker_stop(worker.id)
            with contextlib.suppress(ControlPlaneError):
                await runner_supervisor.stop_project(project_id)
        result = await project_service(session).patch(project_id, command)
        if restart and resolved_settings.managed_runner_autostart and result.status == "available":
            project = await session.get(Project, project_id)
            assert project is not None
            await runner_supervisor.start_project(project)
        return result

    @app.post("/api/projects/{project_id}/validate", response_model=ProjectView)
    async def validate_project(project_id: str, session: Session) -> ProjectView:
        return await project_service(session).validate(project_id)

    @app.get("/api/projects/{project_id}/workflow-snapshots", response_model=list[WorkflowSnapshotView])
    async def project_snapshots(project_id: str, session: Session) -> list[WorkflowSnapshotView]:
        return await project_service(session).snapshots(project_id)

    @app.delete("/api/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_project(project_id: str, session: Session) -> Response:
        await project_service(session).assert_deletable(project_id)
        await session.rollback()
        workers = await service(session).list_workers(project_id)
        await session.rollback()
        for worker in workers:
            with contextlib.suppress(ControlPlaneError):
                await service(session).request_worker_stop(worker.id)
        with contextlib.suppress(ControlPlaneError):
            await runner_supervisor.stop_project(project_id)
        await project_service(session).delete(project_id)
        runner_supervisor.forget_project(project_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/api/issues/{issue_id}/claim", response_model=ClaimResult)
    async def claim(issue_id: str, command: ClaimRequest, session: Session) -> ClaimResult:
        return await service(session).claim(issue_id, command)

    @app.post("/api/issues/{issue_id}/heartbeat", response_model=IssueView)
    async def heartbeat(issue_id: str, command: HeartbeatRequest, session: Session) -> IssueView:
        return await service(session).heartbeat(issue_id, command)

    @app.post("/api/issues/{issue_id}/release", response_model=IssueView)
    async def release(issue_id: str, command: ReleaseRequest, session: Session) -> IssueView:
        return await service(session).release(issue_id, command)

    @app.post("/api/issues/{issue_id}/status", response_model=IssueView)
    async def transition(issue_id: str, command: StatusTransitionRequest, session: Session) -> IssueView:
        return await service(session).transition(issue_id, command)

    @app.get("/api/issues/{issue_id}/attempts", response_model=list[AgentAttemptView])
    async def list_attempts(issue_id: str, session: Session) -> list[AgentAttemptView]:
        return await service(session).list_attempts(issue_id)

    @app.post("/api/issues/{issue_id}/attempt-context", response_model=AgentAttemptView)
    async def update_attempt_context(issue_id: str, command: AttemptContextUpdate, session: Session) -> AgentAttemptView:
        return await service(session).update_attempt_context(issue_id, command)

    @app.post("/api/issues/{issue_id}/attempts/{attempt_id}/events", response_model=AgentAttemptEventView, status_code=status.HTTP_201_CREATED)
    async def add_attempt_event(issue_id: str, attempt_id: str, command: AgentAttemptEventCreate, session: Session) -> AgentAttemptEventView:
        return await service(session).add_attempt_event(issue_id, attempt_id, command)

    @app.get("/api/issues/{issue_id}/attempts/{attempt_id}/events", response_model=list[AgentAttemptEventView])
    async def list_attempt_events(issue_id: str, attempt_id: str, session: Session, after_sequence: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=1000)] = 500) -> list[AgentAttemptEventView]:
        return await service(session).list_attempt_events(issue_id, attempt_id, after_sequence=after_sequence, limit=limit)

    @app.post("/api/issues/{issue_id}/events", response_model=EventView, status_code=status.HTTP_201_CREATED)
    async def add_event(issue_id: str, command: EventCreate, session: Session) -> EventView:
        return await service(session).add_event(issue_id, command)

    @app.get("/api/issues/{issue_id}/events", response_model=list[EventView])
    async def list_events(issue_id: str, session: Session) -> list[EventView]:
        return await service(session).list_events(issue_id)

    @app.post("/api/issues/{issue_id}/artifacts", response_model=ArtifactView, status_code=status.HTTP_201_CREATED)
    async def add_artifact(issue_id: str, command: ArtifactCreate, session: Session) -> ArtifactView:
        return await service(session).add_artifact(issue_id, command)

    @app.get(
        "/api/issues/{issue_id}/artifacts/{artifact_id}",
        response_model=ArtifactContentView,
    )
    async def artifact_content(
        issue_id: str, artifact_id: str, session: Session
    ) -> ArtifactContentView:
        issue = await service(session).get_issue(issue_id)
        artifact = next(
            (item for item in issue.artifacts if item.id == artifact_id), None
        )
        if artifact is None:
            raise NotFoundError("artifact not found")
        target = _artifact_file(issue, artifact)
        data = target.read_bytes()
        current_sha256 = hashlib.sha256(data).hexdigest()
        media_type = (
            artifact.media_type
            or mimetypes.guess_type(target.name)[0]
            or "application/octet-stream"
        )
        textual = media_type.startswith("text/") or media_type in TEXT_MEDIA_TYPES
        truncated = textual and len(data) > ARTIFACT_PREVIEW_LIMIT
        content = (
            data[:ARTIFACT_PREVIEW_LIMIT].decode("utf-8", errors="replace")
            if textual
            else None
        )
        return ArtifactContentView(
            id=artifact.id,
            path=artifact.path,
            media_type=media_type,
            size_bytes=len(data),
            content=content,
            truncated=truncated,
            current_sha256=current_sha256,
            registered_sha256_matches=(
                current_sha256 == artifact.sha256 if artifact.sha256 else None
            ),
        )

    @app.get(
        "/api/issues/{issue_id}/review",
        response_model=WorkspaceReviewView,
    )
    async def workspace_review(
        issue_id: str, session: Session
    ) -> WorkspaceReviewView:
        control_plane = service(session)
        issue = await control_plane.get_issue(issue_id)
        overview = issue.change_summary.overview
        try:
            workspace = Path(issue.workspace_path).resolve(strict=True)
        except OSError as error:
            raise NotFoundError("Issue workspace is unavailable") from error
        if not workspace.is_dir():
            raise NotFoundError("Issue workspace is unavailable")
        pathspec = ("--", ".", ":(exclude).symphony")
        base_commit = issue.source_commit
        head_commit = await _workspace_head(workspace)
        status_output, commits_output, changed_files_output, numstat_output, diff_stat, diff = await asyncio.gather(
            _git_output(
                workspace,
                "status",
                "--short",
                "--untracked-files=all",
                *pathspec,
            ),
            _git_output(
                workspace,
                "log",
                "--format=%h%x09%s",
                f"{base_commit}..HEAD",
                *pathspec,
            ),
            _git_output(
                workspace,
                "diff",
                "--name-status",
                base_commit,
                *pathspec,
            ),
            _git_output(workspace, "diff", "--numstat", base_commit, *pathspec),
            _git_output(workspace, "diff", "--stat", base_commit, *pathspec),
            _git_output(
                workspace,
                "diff",
                "--no-ext-diff",
                "--unified=3",
                base_commit,
                *pathspec,
            ),
        )
        encoded = diff.encode("utf-8")
        truncated = len(encoded) > DIFF_PREVIEW_LIMIT
        if truncated:
            diff = encoded[:DIFF_PREVIEW_LIMIT].decode("utf-8", errors="replace")
        return WorkspaceReviewView(
            workspace_path=str(workspace),
            base_commit=base_commit,
            head_commit=head_commit,
            commits=commits_output.splitlines() if commits_output else [],
            changed_files=(
                changed_files_output.splitlines() if changed_files_output else []
            ),
            status=status_output.splitlines() if status_output else [],
            diff_stat=diff_stat,
            diff=diff,
            diff_truncated=truncated,
            change_summary=build_change_summary(
                changed_files_output,
                status_output,
                numstat_output,
                commits_output,
                overview=overview,
            ),
        )

    @app.post("/api/issues/{issue_id}/decisions", response_model=DecisionView)
    async def decision(issue_id: str, command: DecisionCommand, session: Session) -> DecisionView:
        return await service(session).decision(issue_id, command)

    @app.get("/api/issues/{issue_id}/decisions", response_model=list[DecisionView])
    async def list_decisions(issue_id: str, session: Session) -> list[DecisionView]:
        return await service(session).list_decisions(issue_id)

    @app.post("/api/workers/register", response_model=WorkerView)
    async def register_worker(command: WorkerRegistration, session: Session) -> WorkerView:
        return await service(session).register_worker(command)

    @app.get("/api/workers", response_model=list[WorkerView])
    async def list_workers(session: Session, project_id: str | None = None) -> list[WorkerView]:
        return await service(session).list_workers(project_id)

    @app.post("/api/workers/{worker_id}/heartbeat", response_model=WorkerView)
    async def heartbeat_worker(worker_id: str, command: WorkerHeartbeat, session: Session) -> WorkerView:
        return await service(session).heartbeat_worker(worker_id, command)

    @app.post("/api/workers/{worker_id}/stopped", response_model=WorkerView)
    async def stop_worker(worker_id: str, session: Session) -> WorkerView:
        return await service(session).stop_worker(worker_id)

    @app.post("/api/workers/{worker_id}/request-stop", response_model=WorkerView)
    async def request_worker_stop(worker_id: str, session: Session) -> WorkerView:
        return await service(session).request_worker_stop(worker_id)

    @app.get("/api/agent-runtimes", response_model=list[AgentRuntimeView])
    async def list_agent_runtimes(session: Session) -> list[AgentRuntimeView]:
        return await service(session).list_agent_runtimes()

    @app.get("/api/v1/state")
    async def standard_runtime_state(
        session: Session, project_id: str | None = None
    ) -> Response:
        workers = await service(session).list_workers(project_id)
        snapshot, error_mode = _standard_runtime_state(
            workers,
            offline_after_seconds=resolved_settings.worker_offline_after_seconds,
        )
        if snapshot is None:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "error": {
                        "code": error_mode,
                        "message": "Runtime snapshot is not currently available.",
                    }
                },
            )
        return JSONResponse(content=snapshot)

    @app.get("/api/v1/{issue_identifier}")
    async def standard_issue_runtime(
        issue_identifier: str, session: Session
    ) -> dict[str, object]:
        issue_row = await session.scalar(
            select(Issue).where(
                or_(
                    Issue.id == issue_identifier,
                    Issue.identifier == issue_identifier,
                )
            )
        )
        if issue_row is None:
            raise NotFoundError(f"issue not found: {issue_identifier}")
        issue = await service(session).get_issue(issue_row.id)
        attempts = await service(session).list_attempts(issue_row.id)
        runtimes = await service(session).list_agent_runtimes()
        runtime = next(
            (row for row in runtimes if row.issue_id == issue_row.id), None
        )
        workers = await service(session).list_workers(issue_row.project_id)
        snapshot, _ = _standard_runtime_state(
            workers,
            offline_after_seconds=resolved_settings.worker_offline_after_seconds,
        )
        retry = None
        if snapshot is not None:
            retry = next(
                (
                    row
                    for row in snapshot["retrying"]  # type: ignore[union-attr]
                    if row.get("issue_id") == issue_row.id
                ),
                None,
            )
        latest = attempts[0] if attempts else None
        execution_events = (
            await service(session).list_attempt_events(
                issue_row.id, latest.id, after_sequence=0, limit=100
            )
            if latest is not None
            else []
        )
        return {
            "issue_identifier": issue.identifier,
            "issue_id": issue.id,
            "status": issue.status,
            "workspace": {"path": issue.workspace_path},
            "attempts": {
                "restart_count": max(0, len(attempts) - 1),
                "current_retry_attempt": (
                    retry.get("attempt") if isinstance(retry, dict) else None
                ),
            },
            "running": (
                runtime.model_dump(mode="json")
                if runtime is not None
                and runtime.runtime_source == "orchestrator"
                else None
            ),
            "retry": retry,
            "logs": {
                "attempt_events": [
                    event.model_dump(mode="json") for event in execution_events
                ]
            },
        }

    @app.get("/api/runner-control", response_model=RunnerControlView)
    async def runner_control_status(session: Session) -> RunnerControlView:
        projects = (await session.scalars(select(Project))).all()
        result = runner_supervisor.view()
        result.registered_projects = len(projects)
        result.available_projects = sum(project.status == "available" for project in projects)
        result.invalid_projects = sum(project.status == "invalid" for project in projects)
        return result

    @app.get("/api/projects/{project_id}/runtime", response_model=ProjectRuntimeView)
    async def project_runtime(project_id: str, session: Session) -> ProjectRuntimeView:
        with contextlib.suppress(NotFoundError):
            return runner_supervisor.project_view(project_id)
        project = await session.get(Project, project_id)
        if project is None:
            raise NotFoundError(f"project not found: {project_id}")
        return ProjectRuntimeView(
            project_id=project.id,
            project_key=project.key,
            state="stopped",
            process_id=None,
            worker_id=f"windows-symphony:{project.key}",
            workflow=str((Path(project.repository_path) / project.workflow_path).resolve()),
            started_at=None,
            last_exit_code=None,
            recent_logs=[],
        )

    @app.post("/api/projects/{project_id}/runtime/start", response_model=ProjectRuntimeView)
    async def start_project_runtime(project_id: str, session: Session) -> ProjectRuntimeView:
        project = await session.get(Project, project_id)
        if project is None:
            raise NotFoundError(f"project not found: {project_id}")
        if not project.enabled or project.status != "available":
            await project_service(session).validate(project_id)
            project = await session.get(Project, project_id)
        assert project is not None
        if not project.enabled or project.status != "available":
            raise ConflictError("project must be enabled and valid before starting its Runtime")
        return await runner_supervisor.start_project(project)

    @app.post("/api/projects/{project_id}/runtime/stop", response_model=ProjectRuntimeView)
    async def stop_project_runtime(project_id: str, session: Session) -> ProjectRuntimeView:
        workers = await service(session).list_workers(project_id)
        await session.rollback()
        for worker in workers:
            with contextlib.suppress(ControlPlaneError):
                await service(session).request_worker_stop(worker.id)
        return await runner_supervisor.stop_project(project_id)

    @app.post("/api/maintenance/tick", response_model=MaintenanceResult)
    async def maintenance_tick(session: Session) -> MaintenanceResult:
        return await service(session).maintenance_tick()

    return app


app = create_app()
