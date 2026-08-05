from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from control_plane.config import Settings
from control_plane.errors import ConflictError, NotFoundError
from control_plane.models import Project, utc_now
from control_plane.schemas import ProjectRuntimeView, RunnerControlView


@dataclass
class ProjectRuntime:
    project_id: str
    project_key: str
    repository: Path
    workflow: Path
    default_branch: str
    worker_id: str
    process: asyncio.subprocess.Process | None = None
    reader_task: asyncio.Task[None] | None = None
    waiter_task: asyncio.Task[None] | None = None
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=200))
    state: str = "stopped"
    started_at: datetime | None = None
    last_exit_code: int | None = None


class RunnerSupervisor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._runtimes: dict[str, ProjectRuntime] = {}
        self._lock = asyncio.Lock()

    async def start_project(self, project: Project) -> ProjectRuntimeView:
        async with self._lock:
            current = self._runtimes.get(project.id)
            if current is not None and current.process is not None and current.process.returncode is None:
                raise ConflictError(f"project Runner is already running: {project.key}")
            repository = Path(project.repository_path).resolve()
            workflow = (repository / project.workflow_path).resolve()
            if not workflow.is_file() or not workflow.is_relative_to(repository):
                raise ConflictError(f"project WORKFLOW.md is unavailable: {workflow}")
            runtime = ProjectRuntime(
                project_id=project.id,
                project_key=project.key,
                repository=repository,
                workflow=workflow,
                default_branch=project.default_branch,
                worker_id=f"windows-symphony:{project.key}",
                state="starting",
            )
            self._runtimes[project.id] = runtime
            env = os.environ.copy()
            source_root = str(Path(__file__).resolve().parents[1])
            env["PYTHONPATH"] = os.pathsep.join(filter(None, (source_root, env.get("PYTHONPATH"))))
            env["CONTROL_PLANE_TOKEN"] = (
                self.settings.api_token.get_secret_value()
                if self.settings.api_token is not None
                else "local-managed-runner-token"
            )
            env["SYMPHONY_PROJECT_ID"] = project.id
            env["SYMPHONY_PROJECT_REPOSITORY"] = str(repository)
            env["SYMPHONY_PROJECT_DEFAULT_BRANCH"] = project.default_branch
            env["SYMPHONY_WORKER_ID"] = runtime.worker_id
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            try:
                runtime.process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "symphony_windows",
                    str(workflow),
                    "--log-level",
                    "INFO",
                    cwd=repository,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    creationflags=creationflags,
                    limit=1_048_576,
                )
            except OSError as error:
                runtime.state = "stopped"
                raise ConflictError(f"cannot start project Runner: {error}") from error
            runtime.started_at = utc_now()
            runtime.state = "running"
            runtime.reader_task = asyncio.create_task(self._read_output(runtime))
            runtime.waiter_task = asyncio.create_task(self._wait_for_exit(runtime))
            return self._runtime_view(runtime)

    async def stop_project(self, project_id: str, grace_seconds: float = 8.0) -> ProjectRuntimeView:
        async with self._lock:
            runtime = self._runtimes.get(project_id)
            if runtime is None:
                raise NotFoundError(f"project Runtime not found: {project_id}")
            process = runtime.process
            if process is None or process.returncode is not None:
                runtime.state = "stopped"
                return self._runtime_view(runtime)
            runtime.state = "stopping"
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        except TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
        if runtime.waiter_task is not None:
            await runtime.waiter_task
        return self._runtime_view(runtime)

    async def stop(self) -> RunnerControlView:
        for project_id in list(self._runtimes):
            try:
                await self.stop_project(project_id)
            except NotFoundError:
                pass
        return self.view()

    def view(self) -> RunnerControlView:
        runtimes = [self._runtime_view(item) for item in sorted(self._runtimes.values(), key=lambda value: value.project_key)]
        states = {runtime.state for runtime in runtimes}
        state = "running" if "running" in states else "starting" if "starting" in states else "stopping" if "stopping" in states else "stopped"
        return RunnerControlView(state=state, runtimes=runtimes)  # type: ignore[arg-type]

    def project_view(self, project_id: str) -> ProjectRuntimeView:
        runtime = self._runtimes.get(project_id)
        if runtime is None:
            raise NotFoundError(f"project Runtime not found: {project_id}")
        return self._runtime_view(runtime)

    def forget_project(self, project_id: str) -> None:
        runtime = self._runtimes.get(project_id)
        if runtime is not None and runtime.process is not None and runtime.process.returncode is None:
            raise ConflictError("running project Runtime cannot be removed")
        self._runtimes.pop(project_id, None)

    @staticmethod
    def _runtime_view(runtime: ProjectRuntime) -> ProjectRuntimeView:
        process = runtime.process
        return ProjectRuntimeView(
            project_id=runtime.project_id,
            project_key=runtime.project_key,
            state=runtime.state,  # type: ignore[arg-type]
            process_id=process.pid if process is not None and process.returncode is None else None,
            worker_id=runtime.worker_id,
            workflow=str(runtime.workflow),
            started_at=runtime.started_at,
            last_exit_code=runtime.last_exit_code,
            recent_logs=list(runtime.logs)[-40:],
        )

    async def _read_output(self, runtime: ProjectRuntime) -> None:
        process = runtime.process
        if process is None or process.stdout is None:
            return
        while line := await process.stdout.readline():
            runtime.logs.append(line.decode(errors="replace").rstrip())

    async def _wait_for_exit(self, runtime: ProjectRuntime) -> None:
        process = runtime.process
        if process is None:
            return
        code = await process.wait()
        if runtime.reader_task is not None:
            await runtime.reader_task
        runtime.last_exit_code = code
        runtime.state = "stopped"
