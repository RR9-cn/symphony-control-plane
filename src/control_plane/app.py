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
    AgentAttemptView,
    AgentRuntimeView,
    AgentProfileView,
    AttemptContextUpdate,
    ArtifactCreate,
    ArtifactView,
    ClaimRequest,
    ClaimResult,
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
    RepositoryHeadRequest,
    RepositoryHeadView,
    RunnerControlView,
    ReleaseRequest,
    StatusTransitionRequest,
    WorkItemCreate,
    WorkItemPatch,
    WorkItemView,
    WorkerHeartbeat,
    WorkerRegistration,
    WorkerView,
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

    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(resolved),
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    except FileNotFoundError as error:
        raise RepositoryResolutionError("git is not installed or is not on PATH") from error
    except TimeoutError as error:
        process.kill()
        await process.communicate()
        raise RepositoryResolutionError("timed out while reading repository HEAD") from error

    commit = stdout.decode(errors="replace").strip().lower()
    if process.returncode != 0 or not COMMIT_PATTERN.fullmatch(commit):
        detail = stderr.decode(errors="replace").strip()
        message = "path is not a Git repository with a valid HEAD"
        if detail:
            message = f"{message}: {detail.splitlines()[-1]}"
        raise RepositoryResolutionError(message)
    return RepositoryHeadView(path=str(resolved), commit=commit)


async def _lease_sweeper(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    while True:
        await asyncio.sleep(settings.lease_sweep_interval_seconds)
        try:
            async with app.state.database.sessions() as session:
                await ControlPlaneService(session, settings).maintenance_tick()
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
        task: asyncio.Task[None] | None = None
        if resolved_settings.enable_lease_sweeper:
            task = asyncio.create_task(_lease_sweeper(app))
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

    app = FastAPI(
        title="Fshows Agent Control Plane",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.database = resolved_database
    app.state.lease_sweeper_failures = 0
    app.state.runner_supervisor = runner_supervisor

    @app.middleware("http")
    async def authenticate_api(request: Request, call_next):  # type: ignore[no-untyped-def]
        configured = resolved_settings.api_token
        if configured is not None and request.url.path.startswith("/api/"):
            authorization = request.headers.get("authorization", "")
            expected = f"Bearer {configured.get_secret_value()}"
            if not hmac.compare_digest(authorization, expected):
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": {
                            "code": "unauthorized",
                            "message": "A valid Control Plane bearer token is required.",
                        }
                    },
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return await call_next(request)

    @app.exception_handler(ControlPlaneError)
    async def control_plane_error_handler(
        _request: Request, error: ControlPlaneError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={"error": {"code": error.code, "message": str(error)}},
        )

    async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
        async with request.app.state.database.sessions() as session:
            yield session

    Session = Annotated[AsyncSession, Depends(get_session)]

    def service(session: AsyncSession) -> ControlPlaneService:
        return ControlPlaneService(session, resolved_settings)

    app.mount("/ui/assets", StaticFiles(directory=UI_ROOT), name="ui-assets")

    @app.get("/", include_in_schema=False, response_class=FileResponse)
    async def dashboard() -> FileResponse:
        return FileResponse(UI_ROOT / "index.html")

    @app.get("/ui", include_in_schema=False, response_class=FileResponse)
    async def dashboard_alias() -> FileResponse:
        return FileResponse(UI_ROOT / "index.html")

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "database": "sqlite",
            "auth_enabled": resolved_settings.api_token is not None,
            "lease_sweeper_failures": app.state.lease_sweeper_failures,
            "managed_runner": runner_supervisor.view().state,
        }

    @app.post(
        "/api/features", response_model=FeatureView, status_code=status.HTTP_201_CREATED
    )
    async def create_feature(command: FeatureCreate, session: Session) -> FeatureView:
        return await service(session).create_feature(command)

    @app.get("/api/features", response_model=list[FeatureView])
    async def list_features(session: Session) -> list[FeatureView]:
        return await service(session).list_features()

    @app.get("/api/features/{feature_id}", response_model=FeatureView)
    async def get_feature(feature_id: str, session: Session) -> FeatureView:
        return await service(session).get_feature(feature_id)

    @app.post("/api/repositories/resolve-head", response_model=RepositoryHeadView)
    async def resolve_repository_head(command: RepositoryHeadRequest) -> RepositoryHeadView:
        return await _resolve_local_repository_head(command.path)

    @app.post("/api/intake/manual/issues/preview", response_model=ManualIssuePreview)
    async def preview_manual_issue(
        command: ManualIssueCreate, session: Session
    ) -> ManualIssuePreview:
        return await service(session).preview_manual_issue(command)

    @app.post(
        "/api/intake/manual/issues",
        response_model=ManualIssueResult,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_manual_issue(
        command: ManualIssueCreate, session: Session
    ) -> ManualIssueResult:
        return await service(session).create_manual_issue(command)

    @app.post(
        "/api/work-items", response_model=WorkItemView, status_code=status.HTTP_201_CREATED
    )
    async def create_work_item(command: WorkItemCreate, session: Session) -> WorkItemView:
        return await service(session).create_work_item(command)

    @app.get("/api/work-items", response_model=list[WorkItemView])
    async def list_work_items(
        session: Session,
        feature_id: str | None = None,
        work_item_statuses: list[str] | None = Query(default=None, alias="status"),
        item_ids: list[str] | None = Query(default=None, alias="id"),
    ) -> list[WorkItemView]:
        return await service(session).list_work_items(
            feature_id, work_item_statuses, item_ids
        )

    @app.get("/api/work-items/candidates", response_model=list[WorkItemView])
    async def candidates(
        session: Session, limit: int = Query(default=100, ge=1, le=500)
    ) -> list[WorkItemView]:
        return await service(session).candidates(limit)

    @app.get("/api/agent-profiles", response_model=list[AgentProfileView])
    async def list_agent_profiles(session: Session) -> list[AgentProfileView]:
        return await service(session).list_agent_profiles()

    @app.post("/api/workers/register", response_model=WorkerView)
    async def register_worker(
        command: WorkerRegistration, session: Session
    ) -> WorkerView:
        return await service(session).register_worker(command)

    @app.get("/api/workers", response_model=list[WorkerView])
    async def list_workers(session: Session) -> list[WorkerView]:
        return await service(session).list_workers()

    @app.get("/api/runner-control", response_model=RunnerControlView)
    async def runner_control_status() -> RunnerControlView:
        return runner_supervisor.view()

    @app.post("/api/runner-control/start", response_model=RunnerControlView)
    async def start_managed_runner() -> RunnerControlView:
        return await runner_supervisor.start()

    @app.post("/api/runner-control/stop", response_model=RunnerControlView)
    async def stop_managed_runner(session: Session) -> RunnerControlView:
        with contextlib.suppress(ControlPlaneError):
            await service(session).request_worker_stop(
                resolved_settings.managed_runner_worker_id
            )
        return await runner_supervisor.stop()

    @app.post("/api/workers/{worker_id}/heartbeat", response_model=WorkerView)
    async def heartbeat_worker(
        worker_id: str, command: WorkerHeartbeat, session: Session
    ) -> WorkerView:
        return await service(session).heartbeat_worker(worker_id, command)

    @app.post("/api/workers/{worker_id}/stopped", response_model=WorkerView)
    async def stop_worker(worker_id: str, session: Session) -> WorkerView:
        return await service(session).stop_worker(worker_id)

    @app.post("/api/workers/{worker_id}/request-stop", response_model=WorkerView)
    async def request_worker_stop(worker_id: str, session: Session) -> WorkerView:
        return await service(session).request_worker_stop(worker_id)

    @app.get("/api/agent-runtimes", response_model=list[AgentRuntimeView])
    async def list_agent_runtimes(
        session: Session, feature_id: str | None = None
    ) -> list[AgentRuntimeView]:
        return await service(session).list_agent_runtimes(feature_id)

    @app.get(
        "/api/work-items/{item_id}/attempts", response_model=list[AgentAttemptView]
    )
    async def list_attempts(item_id: str, session: Session) -> list[AgentAttemptView]:
        return await service(session).list_attempts(item_id)

    @app.post(
        "/api/work-items/{item_id}/attempt-context",
        response_model=AgentAttemptView,
    )
    async def update_attempt_context(
        item_id: str, command: AttemptContextUpdate, session: Session
    ) -> AgentAttemptView:
        return await service(session).update_attempt_context(item_id, command)

    @app.get("/api/work-items/{item_id}", response_model=WorkItemView)
    async def get_work_item(item_id: str, session: Session) -> WorkItemView:
        return await service(session).get_work_item(item_id)

    @app.patch("/api/work-items/{item_id}", response_model=WorkItemView)
    async def patch_work_item(
        item_id: str, command: WorkItemPatch, session: Session
    ) -> WorkItemView:
        return await service(session).patch_work_item(item_id, command)

    @app.post("/api/work-items/{item_id}/claim", response_model=ClaimResult)
    async def claim(item_id: str, command: ClaimRequest, session: Session) -> ClaimResult:
        return await service(session).claim(item_id, command)

    @app.post("/api/work-items/{item_id}/heartbeat", response_model=WorkItemView)
    async def heartbeat(
        item_id: str, command: HeartbeatRequest, session: Session
    ) -> WorkItemView:
        return await service(session).heartbeat(item_id, command)

    @app.post("/api/work-items/{item_id}/release", response_model=WorkItemView)
    async def release(
        item_id: str, command: ReleaseRequest, session: Session
    ) -> WorkItemView:
        return await service(session).release(item_id, command)

    @app.post("/api/work-items/{item_id}/status", response_model=WorkItemView)
    async def transition(
        item_id: str, command: StatusTransitionRequest, session: Session
    ) -> WorkItemView:
        return await service(session).transition(item_id, command)

    @app.post(
        "/api/work-items/{item_id}/events",
        response_model=EventView,
        status_code=status.HTTP_201_CREATED,
    )
    async def add_event(
        item_id: str, command: EventCreate, session: Session
    ) -> EventView:
        return await service(session).add_event(item_id, command)

    @app.get("/api/work-items/{item_id}/events", response_model=list[EventView])
    async def list_events(item_id: str, session: Session) -> list[EventView]:
        return await service(session).list_events(item_id)

    @app.post(
        "/api/work-items/{item_id}/artifacts",
        response_model=ArtifactView,
        status_code=status.HTTP_201_CREATED,
    )
    async def add_artifact(
        item_id: str, command: ArtifactCreate, session: Session
    ) -> ArtifactView:
        return await service(session).add_artifact(item_id, command)

    @app.post("/api/work-items/{item_id}/decisions", response_model=DecisionView)
    async def decision(
        item_id: str, command: DecisionCommand, session: Session
    ) -> DecisionView:
        return await service(session).decision(item_id, command)

    @app.get("/api/work-items/{item_id}/decisions", response_model=list[DecisionView])
    async def list_decisions(item_id: str, session: Session) -> list[DecisionView]:
        return await service(session).list_decisions(item_id)

    @app.post("/api/maintenance/tick", response_model=MaintenanceResult)
    async def maintenance_tick(session: Session) -> MaintenanceResult:
        return await service(session).maintenance_tick()

    return app


app = create_app()
