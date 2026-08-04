import asyncio
import contextlib
import hmac
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator

from fastapi import Depends, FastAPI, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.config import Settings
from control_plane.database import Database
from control_plane.errors import ControlPlaneError
from control_plane.schemas import (
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
    MaintenanceResult,
    ReleaseRequest,
    StatusTransitionRequest,
    WorkItemCreate,
    WorkItemPatch,
    WorkItemView,
)
from control_plane.service import ControlPlaneService


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

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task: asyncio.Task[None] | None = None
        if resolved_settings.enable_lease_sweeper:
            task = asyncio.create_task(_lease_sweeper(app))
        try:
            yield
        finally:
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

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "database": "sqlite",
            "auth_enabled": resolved_settings.api_token is not None,
            "lease_sweeper_failures": app.state.lease_sweeper_failures,
        }

    @app.post(
        "/api/features", response_model=FeatureView, status_code=status.HTTP_201_CREATED
    )
    async def create_feature(command: FeatureCreate, session: Session) -> FeatureView:
        return await service(session).create_feature(command)

    @app.get("/api/features/{feature_id}", response_model=FeatureView)
    async def get_feature(feature_id: str, session: Session) -> FeatureView:
        return await service(session).get_feature(feature_id)

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

    @app.post("/api/maintenance/tick", response_model=MaintenanceResult)
    async def maintenance_tick(session: Session) -> MaintenanceResult:
        return await service(session).maintenance_tick()

    return app


app = create_app()
