from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections import deque
from datetime import datetime
from pathlib import Path

from control_plane.config import Settings
from control_plane.errors import ConflictError
from control_plane.models import utc_now
from control_plane.schemas import RunnerControlView


class RunnerSupervisor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.workflow = Path(settings.managed_runner_workflow).resolve()
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._waiter_task: asyncio.Task[None] | None = None
        self._logs: deque[str] = deque(maxlen=200)
        self._state = "stopped"
        self._started_at: datetime | None = None
        self._last_exit_code: int | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> RunnerControlView:
        async with self._lock:
            if self._process is not None and self._process.returncode is None:
                raise ConflictError("managed Runner is already running")
            if not self.workflow.is_file():
                raise ConflictError(f"Runner workflow not found: {self.workflow}")
            self._state = "starting"
            env = os.environ.copy()
            source_root = str(Path(__file__).resolve().parents[1])
            existing_pythonpath = env.get("PYTHONPATH")
            env["PYTHONPATH"] = (
                source_root
                if not existing_pythonpath
                else os.pathsep.join((source_root, existing_pythonpath))
            )
            env["CONTROL_PLANE_TOKEN"] = (
                self.settings.api_token.get_secret_value()
                if self.settings.api_token is not None
                else "local-managed-runner-token"
            )
            env["SYMPHONY_WORKER_ID"] = self.settings.managed_runner_worker_id
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            try:
                self._process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "symphony_windows",
                    str(self.workflow),
                    "--log-level",
                    "INFO",
                    cwd=self.workflow.parent,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    creationflags=creationflags,
                    limit=1_048_576,
                )
            except OSError as error:
                self._state = "stopped"
                raise ConflictError(f"cannot start managed Runner: {error}") from error
            self._started_at = utc_now()
            self._last_exit_code = None
            self._state = "running"
            self._reader_task = asyncio.create_task(self._read_output())
            self._waiter_task = asyncio.create_task(self._wait_for_exit())
            return self.view()

    async def stop(self, grace_seconds: float = 8.0) -> RunnerControlView:
        async with self._lock:
            process = self._process
            if process is None or process.returncode is not None:
                self._state = "stopped"
                return self.view()
            self._state = "stopping"
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        except TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
        if self._waiter_task is not None:
            await self._waiter_task
        return self.view()

    def view(self) -> RunnerControlView:
        process = self._process
        process_id = (
            process.pid if process is not None and process.returncode is None else None
        )
        return RunnerControlView(
            state=self._state,  # type: ignore[arg-type]
            process_id=process_id,
            worker_id=self.settings.managed_runner_worker_id,
            workflow=str(self.workflow),
            started_at=self._started_at,
            last_exit_code=self._last_exit_code,
            recent_logs=list(self._logs)[-40:],
        )

    async def _read_output(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        while line := await process.stdout.readline():
            self._logs.append(line.decode(errors="replace").rstrip())

    async def _wait_for_exit(self) -> None:
        process = self._process
        if process is None:
            return
        code = await process.wait()
        if self._reader_task is not None:
            await self._reader_task
        self._last_exit_code = code
        self._state = "stopped"
