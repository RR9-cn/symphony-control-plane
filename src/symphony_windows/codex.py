from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import shutil
import subprocess
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from symphony_windows.tracker import ClaimLease, ControlPlaneTracker
from symphony_windows.workflow import CodexConfig


logger = logging.getLogger(__name__)


class CodexError(RuntimeError):
    """Codex app-server exited or violated its protocol contract."""


@dataclass(frozen=True)
class CodexRunResult:
    status: str
    thread_id: str
    turn_id: str


EventHandler = Callable[[dict[str, Any]], Awaitable[None] | None]


class CodexAppServer:
    def __init__(
        self,
        config: CodexConfig,
        *,
        secret_environment_names: tuple[str, ...] = (),
        on_event: EventHandler | None = None,
    ) -> None:
        self.config = config
        self.secret_environment_names = secret_environment_names
        self.on_event = on_event
        self._request_id = 0
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._stderr_task: asyncio.Task[None] | None = None

    async def run(
        self,
        workspace: Path,
        prompt: str,
        issue: dict[str, Any],
        tracker: ControlPlaneTracker,
        lease: ClaimLease,
    ) -> CodexRunResult:
        await self._start(workspace)
        thread_id = ""
        turn_id = ""
        try:
            await self._initialize()
            await self._validate_skills(workspace)
            thread_id = await self._start_thread(workspace, tracker)
            turn_id = await self._start_turn(workspace, prompt, issue, thread_id)
            status = await self._receive_turn(tracker, lease)
            return CodexRunResult(status=status, thread_id=thread_id, turn_id=turn_id)
        finally:
            await self.stop()

    async def stop(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            await _terminate_process_tree(process)
        if self._stderr_task is not None:
            if not self._stderr_task.done():
                self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
            self._stderr_task = None

    async def _start(self, workspace: Path) -> None:
        if self._process is not None:
            raise CodexError("Codex app-server is already running")
        original_home = Path.home().resolve()
        env = os.environ.copy()
        for name in self.secret_environment_names:
            env.pop(name, None)
        if self.config.isolate_user_home:
            isolated_home = workspace / ".symphony" / "user-home"
            isolated_home.mkdir(parents=True, exist_ok=True)
            codex_home = Path(
                env.get("CODEX_HOME", str(original_home / ".codex"))
            ).expanduser()
            if codex_home.is_dir():
                env["CODEX_HOME"] = str(codex_home.resolve())
            else:
                isolated_codex_home = isolated_home / ".codex"
                isolated_codex_home.mkdir()
                env["CODEX_HOME"] = str(isolated_codex_home)
            env["HOME"] = str(isolated_home)
            env["USERPROFILE"] = str(isolated_home)
            if os.name == "nt" and isolated_home.drive:
                env["HOMEDRIVE"] = isolated_home.drive
                env["HOMEPATH"] = str(isolated_home)[len(isolated_home.drive) :]
        command = _shell_command(self.config.command)
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                cwd=workspace,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
                limit=1_048_576,
            )
        except OSError as error:
            raise CodexError(f"cannot launch Codex app-server: {error}") from error
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _validate_skills(self, workspace: Path) -> None:
        result = await self._request(
            "skills/list",
            {
                "cwds": [str(workspace)],
                "forceReload": True,
            },
        )
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list):
            raise CodexError(f"invalid skills/list response: {result!r}")
        entries: list[dict[str, Any]] = []
        for group in data:
            if not isinstance(group, dict):
                continue
            skills = group.get("skills")
            if isinstance(skills, list):
                entries.extend(skill for skill in skills if isinstance(skill, dict))

        allowed = set(self.config.allowed_skills)
        expected_root = (workspace / ".agents" / "skills").resolve()
        found: set[str] = set()
        unexpected_fshows: set[str] = set()
        for entry in entries:
            name = entry.get("name")
            raw_path = entry.get("path")
            if not isinstance(name, str):
                continue
            if name.startswith("fskill-") and name not in allowed:
                unexpected_fshows.add(name)
            if name not in allowed or not isinstance(raw_path, str):
                continue
            path = Path(raw_path).resolve()
            expected = (expected_root / name / "SKILL.md").resolve()
            if path == expected:
                found.add(name)
        missing = allowed - found
        if missing or unexpected_fshows:
            details: list[str] = []
            if missing:
                details.append(f"missing workspace skills: {', '.join(sorted(missing))}")
            if unexpected_fshows:
                details.append(
                    f"unexpected Fshows skills: {', '.join(sorted(unexpected_fshows))}"
                )
            raise CodexError("Codex Skill allowlist validation failed: " + "; ".join(details))

    async def _initialize(self) -> None:
        await self._request(
            "initialize",
            {
                "capabilities": {"experimentalApi": True},
                "clientInfo": {
                    "name": "fshows-windows-symphony",
                    "title": "Fshows Windows Symphony",
                    "version": "0.1.0",
                },
            },
        )
        await self._send({"method": "initialized", "params": {}})

    async def _start_thread(
        self,
        workspace: Path,
        tracker: ControlPlaneTracker,
    ) -> str:
        params: dict[str, Any] = {
            "approvalPolicy": self.config.approval_policy,
            "sandbox": self.config.thread_sandbox,
            "cwd": str(workspace),
            "dynamicTools": tracker.tool_specs(),
        }
        if self.config.model is not None:
            params["model"] = self.config.model
        result = await self._request(
            "thread/start",
            params,
        )
        thread = result.get("thread") if isinstance(result, dict) else None
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            raise CodexError(f"invalid thread/start response: {result!r}")
        return thread_id

    async def _start_turn(
        self,
        workspace: Path,
        prompt: str,
        issue: dict[str, Any],
        thread_id: str,
    ) -> str:
        identifier = issue.get("identifier") or issue.get("id")
        result = await self._request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
                "cwd": str(workspace),
                "title": f"{identifier}: {issue.get('title', '')}",
                "approvalPolicy": self.config.approval_policy,
                "sandboxPolicy": self.config.turn_sandbox_policy,
            },
        )
        turn = result.get("turn") if isinstance(result, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            raise CodexError(f"invalid turn/start response: {result!r}")
        return turn_id

    async def _receive_turn(
        self,
        tracker: ControlPlaneTracker,
        lease: ClaimLease,
    ) -> str:
        silence_timeout_ms = (
            self.config.stall_timeout_ms
            if self.config.stall_timeout_ms > 0
            else self.config.turn_timeout_ms
        )
        while True:
            message = await self._read_message(silence_timeout_ms)
            await self._emit(message)
            method = message.get("method")
            if method == "turn/completed":
                return "turn_completed"
            if method in {"turn/failed", "turn/cancelled"}:
                raise CodexError(f"Codex {method}: {message.get('params')!r}")
            if method == "item/tool/call" and "id" in message:
                name, arguments = _tool_call(message.get("params"))
                execution = await tracker.execute_tool(lease, name, arguments)
                await self._send({"id": message["id"], "result": execution.response})
                if execution.stop_agent:
                    return "work_item_released"
                continue
            if method in _APPROVAL_METHODS and "id" in message:
                question = _input_question(method, message.get("params"))
                await tracker.request_runtime_input(lease, question)
                return "input_required"
            if isinstance(method, str) and _looks_like_input_request(method):
                question = _input_question(method, message.get("params"))
                await tracker.request_runtime_input(lease, question)
                return "input_required"

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        self._request_id += 1
        request_id = self._request_id
        await self._send({"method": method, "id": request_id, "params": params})
        while True:
            message = await self._read_message(self.config.read_timeout_ms)
            await self._emit(message)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise CodexError(f"Codex {method} failed: {message['error']!r}")
            return message.get("result")

    async def _send(self, message: dict[str, Any]) -> None:
        process = self._require_process()
        if process.stdin is None or process.returncode is not None:
            raise CodexError("Codex app-server stdin is unavailable")
        encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        process.stdin.write(encoded.encode("utf-8") + b"\n")
        try:
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as error:
            raise CodexError(self._exit_details("Codex app-server closed stdin")) from error

    async def _read_message(self, timeout_ms: int) -> dict[str, Any]:
        process = self._require_process()
        if process.stdout is None:
            raise CodexError("Codex app-server stdout is unavailable")
        try:
            line = await asyncio.wait_for(
                process.stdout.readline(),
                timeout=timeout_ms / 1000,
            )
        except TimeoutError as error:
            raise CodexError(f"Codex app-server was silent for {timeout_ms}ms") from error
        if not line:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=1)
            raise CodexError(self._exit_details("Codex app-server exited"))
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            text = line.decode("utf-8", errors="replace").strip()
            raise CodexError(f"invalid JSON from Codex app-server: {text[:500]}") from error
        if not isinstance(payload, dict):
            raise CodexError("Codex app-server message must be an object")
        return payload

    async def _drain_stderr(self) -> None:
        process = self._require_process()
        if process.stderr is None:
            return
        while line := await process.stderr.readline():
            text = line.decode("utf-8", errors="replace").rstrip()
            self._stderr_tail.append(text)
            logger.debug("codex stderr: %s", text)

    async def _emit(self, message: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        result = self.on_event(message)
        if inspect.isawaitable(result):
            await result

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise CodexError("Codex app-server is not running")
        return self._process

    def _exit_details(self, prefix: str) -> str:
        process = self._process
        code = process.returncode if process else None
        stderr = "\n".join(self._stderr_tail)
        suffix = f" (exit={code})"
        if stderr:
            suffix += f": {stderr[-2000:]}"
        return prefix + suffix


_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/tool/requestUserInput",
    "execCommandApproval",
    "applyPatchApproval",
}

_INPUT_METHODS = {
    "mcpServer/elicitation/request",
    "turn/input_required",
    "turn/needs_input",
    "turn/need_input",
    "turn/request_input",
    "turn/request_response",
    "turn/provide_input",
    "turn/approval_required",
}


def _shell_command(command: str) -> tuple[str, ...]:
    if os.name == "nt":
        executable = os.environ.get("ComSpec") or shutil.which("cmd.exe")
        if not executable:
            raise CodexError("cmd.exe is required to launch codex.command")
        return executable, "/d", "/s", "/c", command
    executable = shutil.which("bash") or shutil.which("sh")
    if not executable:
        raise CodexError("a POSIX shell is required to launch codex.command")
    return executable, "-lc", command


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        killer = shutil.which("taskkill.exe") or shutil.which("taskkill")
        if killer:
            cleanup = await asyncio.create_subprocess_exec(
                killer,
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await cleanup.wait()
        else:
            process.kill()
    else:
        process.terminate()
    with contextlib.suppress(ProcessLookupError, TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=5)
    if process.returncode is None:
        process.kill()
        await process.wait()


def _tool_call(params: Any) -> tuple[str | None, dict[str, Any]]:
    if not isinstance(params, dict):
        return None, {}
    name = params.get("tool") or params.get("name")
    arguments = params.get("arguments") or params.get("input") or {}
    if isinstance(arguments, str):
        with contextlib.suppress(json.JSONDecodeError):
            arguments = json.loads(arguments)
    return (name if isinstance(name, str) else None, arguments if isinstance(arguments, dict) else {})


def _looks_like_input_request(method: str) -> bool:
    lowered = method.lower()
    return (
        method in _INPUT_METHODS
        or "requestapproval" in lowered
        or "requestuserinput" in lowered
        or lowered.endswith("/elicitation")
    )


def _input_question(method: str, params: Any) -> str:
    if isinstance(params, dict):
        for key in ("question", "reason", "message"):
            value = params.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return f"Codex requires operator input for {method}."
