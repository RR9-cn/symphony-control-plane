import asyncio
import contextlib
import hmac
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, AsyncIterator

from fastapi import Depends, FastAPI, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.config import Settings
from control_plane.database import Database
from control_plane.errors import ControlPlaneError, RepositoryResolutionError
from control_plane.runner_supervisor import RunnerSupervisor
from control_plane.schemas import (
    AgentAttemptEventCreate, AgentAttemptEventView, AgentAttemptView, AgentRuntimeView,
    ArtifactCreate, ArtifactView, AttemptContextUpdate, ClaimRequest, ClaimResult,
    DecisionCommand, DecisionView, EventCreate, EventView, HeartbeatRequest,
    IssueCreate, IssueDeliveryCommand, IssuePatch, IssueView, MaintenanceResult,
    ReleaseRequest, RepositoryHeadRequest, RepositoryHeadView, RunnerControlView,
    StatusTransitionRequest, WorkerHeartbeat, WorkerRegistration, WorkerView,
)
from control_plane.service import ControlPlaneService


UI_ROOT = Path(__file__).with_name("ui")
COMMIT_PATTERN = re.compile(r"^[a-f0-9]{40}$")


async def _resolve_local_repository_head(path_value: str) -> RepositoryHeadView:
    candidate = Path(path_value.strip())
    if not candidate.is_absolute():
        raise RepositoryResolutionError("repository path must be an absolute local path")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise RepositoryResolutionError("repository path does not exist") from error
    if not resolved.is_dir():
        raise RepositoryResolutionError("repository path must be a directory")
    process = await asyncio.create_subprocess_exec(
        "git", "-C", str(resolved), "rev-parse", "--verify", "HEAD^{commit}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    except TimeoutError as error:
        process.kill()
        await process.communicate()
        raise RepositoryResolutionError("timed out while reading repository HEAD") from error
    commit = stdout.decode(errors="replace").strip().lower()
    if process.returncode != 0 or not COMMIT_PATTERN.fullmatch(commit):
        detail = stderr.decode(errors="replace").strip()
        raise RepositoryResolutionError(detail or "path is not a Git repository with a valid HEAD")
    return RepositoryHeadView(path=str(resolved), commit=commit)


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


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    resolved_settings = settings or Settings()
    resolved_database = database or Database(resolved_settings)
    runner_supervisor = RunnerSupervisor(resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(_lease_sweeper(app)) if resolved_settings.enable_lease_sweeper else None
        if resolved_settings.managed_runner_autostart:
            with contextlib.suppress(ControlPlaneError):
                await runner_supervisor.start()
        try:
            yield
        finally:
            await runner_supervisor.stop()
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await resolved_database.dispose()

    app = FastAPI(title="Fshows Symphony Control Plane", version="0.2.0", lifespan=lifespan)
    app.state.settings = resolved_settings
    app.state.database = resolved_database
    app.state.lease_sweeper_failures = 0
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

    app.mount("/ui/assets", StaticFiles(directory=UI_ROOT), name="ui-assets")

    @app.get("/", include_in_schema=False, response_class=FileResponse)
    async def dashboard() -> FileResponse:
        return FileResponse(UI_ROOT / "index.html")

    @app.get("/ui", include_in_schema=False, response_class=FileResponse)
    async def dashboard_alias() -> FileResponse:
        return FileResponse(UI_ROOT / "index.html")

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "database": "sqlite", "auth_enabled": resolved_settings.api_token is not None, "lease_sweeper_failures": app.state.lease_sweeper_failures, "managed_runner": runner_supervisor.view().state}

    @app.post("/api/issues", response_model=IssueView, status_code=status.HTTP_201_CREATED)
    async def create_issue(command: IssueCreate, session: Session) -> IssueView:
        return await service(session).create_issue(command)

    @app.get("/api/issues", response_model=list[IssueView])
    async def list_issues(session: Session, statuses: list[str] | None = Query(default=None, alias="state"), issue_ids: list[str] | None = Query(default=None, alias="id")) -> list[IssueView]:
        return await service(session).list_issues(statuses, issue_ids)

    @app.get("/api/issues/candidates", response_model=list[IssueView])
    async def candidates(session: Session, limit: int = Query(default=100, ge=1, le=500)) -> list[IssueView]:
        return await service(session).candidates(limit)

    @app.get("/api/issues/{issue_id}", response_model=IssueView)
    async def get_issue(issue_id: str, session: Session) -> IssueView:
        return await service(session).get_issue(issue_id)

    @app.patch("/api/issues/{issue_id}", response_model=IssueView)
    async def patch_issue(issue_id: str, command: IssuePatch, session: Session) -> IssueView:
        return await service(session).patch_issue(issue_id, command)

    @app.post("/api/issues/{issue_id}/delivery", response_model=IssueView)
    async def deliver_issue(issue_id: str, command: IssueDeliveryCommand, session: Session) -> IssueView:
        return await service(session).deliver_issue(issue_id, command)

    @app.post("/api/repositories/resolve-head", response_model=RepositoryHeadView)
    async def resolve_repository_head(command: RepositoryHeadRequest) -> RepositoryHeadView:
        return await _resolve_local_repository_head(command.path)

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
    async def list_workers(session: Session) -> list[WorkerView]:
        return await service(session).list_workers()

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
    async def runner_control_status() -> RunnerControlView:
        return runner_supervisor.view()

    @app.post("/api/runner-control/start", response_model=RunnerControlView)
    async def start_managed_runner() -> RunnerControlView:
        return await runner_supervisor.start()

    @app.post("/api/runner-control/stop", response_model=RunnerControlView)
    async def stop_managed_runner(session: Session) -> RunnerControlView:
        with contextlib.suppress(ControlPlaneError):
            await service(session).request_worker_stop(resolved_settings.managed_runner_worker_id)
        return await runner_supervisor.stop()

    @app.post("/api/maintenance/tick", response_model=MaintenanceResult)
    async def maintenance_tick(session: Session) -> MaintenanceResult:
        return await service(session).maintenance_tick()

    return app


app = create_app()
