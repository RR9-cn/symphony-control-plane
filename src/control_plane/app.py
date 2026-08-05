import asyncio
import contextlib
import hmac
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, AsyncIterator

from fastapi import Depends, FastAPI, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.config import Settings
from control_plane.database import Database
from control_plane.errors import ConflictError, ControlPlaneError, NotFoundError
from control_plane.models import Project
from control_plane.project_service import ProjectService
from control_plane.runner_supervisor import RunnerSupervisor
from control_plane.schemas import (
    AgentAttemptEventCreate, AgentAttemptEventView, AgentAttemptView, AgentRuntimeView,
    ArtifactCreate, ArtifactView, AttemptContextUpdate, ClaimRequest, ClaimResult,
    DecisionCommand, DecisionView, EventCreate, EventView, HeartbeatRequest,
    IssueCreate, IssueDeliveryCommand, IssuePatch, IssueView, MaintenanceResult,
    ProjectCreate, ProjectPatch, ProjectRuntimeView, ProjectView,
    ReleaseRequest, RunnerControlView, WorkflowSnapshotView,
    StatusTransitionRequest, WorkerHeartbeat, WorkerRegistration, WorkerView,
)
from control_plane.service import ControlPlaneService


UI_ROOT = Path(__file__).with_name("ui")
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


async def _project_refresher(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(max(2.0, app.state.settings.lease_sweep_interval_seconds))
        try:
            async with app.state.database.sessions() as session:
                projects = (await session.scalars(select(Project).where(Project.enabled.is_(True)))).all()
                for project in projects:
                    await ProjectService(
                        session,
                        app.state.settings.api_token.get_secret_value() if app.state.settings.api_token else None,
                    ).validate(project.id)
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

    app = FastAPI(title="Fshows Symphony Control Plane", version="0.2.0", lifespan=lifespan)
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
