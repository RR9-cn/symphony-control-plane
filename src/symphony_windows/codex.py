from __future__ import annotations

import asyncio
import contextlib
import hashlib
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

from symphony_windows.tracker import ClaimLease, TrackerAdapter
from symphony_windows.workflow import CodexConfig


logger = logging.getLogger(__name__)


class CodexError(RuntimeError):
    """Codex app-server exited or violated its protocol contract."""


@dataclass(frozen=True)
class CodexRunResult:
    status: str
    thread_id: str
    turn_id: str
    turn_count: int = 1


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
        tracker: TrackerAdapter,
        lease: ClaimLease,
        resume_thread_id: str | None = None,
        max_turns: int = 1,
    ) -> CodexRunResult:
        if max_turns < 1:
            raise CodexError("max_turns must be positive")
        await self._start(workspace)
        thread_id = ""
        turn_id = ""
        try:
            await self._initialize()
            skills = await self._validate_skills(workspace)
            if add_attempt_event := getattr(tracker, "add_attempt_event", None):
                await add_attempt_event(
                    lease,
                    {
                        "event_type": "skills_discovered",
                        "item_type": "project_skills",
                        "status": "completed",
                        "summary": f"Discovered {len(skills)} repository Skills",
                        "payload": {"skills": skills},
                    },
                )
            thread_id = await self._open_thread(
                workspace,
                tracker,
                resume_thread_id=resume_thread_id,
            )
            await tracker.update_attempt_context(lease, thread_id=thread_id)
            turn_count = 0
            status = "turn_completed"
            while lease.active and turn_count < max_turns:
                turn_prompt = (
                    prompt
                    if turn_count == 0
                    else _continuation_prompt(issue, turn_count + 1, max_turns)
                )
                turn_id = await self._start_turn(
                    workspace, turn_prompt, issue, thread_id
                )
                turn_count += 1
                await tracker.update_attempt_context(
                    lease, thread_id=thread_id, turn_id=turn_id, turn_count=turn_count
                )
                status = await self._receive_turn(tracker, lease, thread_id)
                if status != "turn_completed":
                    break
                if lease.active and not await tracker.refresh_claim(lease):
                    status = "issue_released"
                    break
            return CodexRunResult(
                status=status,
                thread_id=thread_id,
                turn_id=turn_id,
                turn_count=turn_count,
            )
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
            isolated_codex_home = isolated_home / ".codex"
            isolated_codex_home.mkdir(exist_ok=True)
            if codex_home.is_dir():
                for auth_name in ("auth.json", ".cockpit_codex_auth.json"):
                    source = codex_home / auth_name
                    if source.is_file():
                        shutil.copy2(source, isolated_codex_home / auth_name)
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

    async def _validate_skills(self, workspace: Path) -> list[dict[str, str]]:
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

        expected_root = (workspace / ".codex" / "skills").resolve()
        declared: dict[str, Path] = {}
        if expected_root.is_dir():
            for skill_file in expected_root.glob("*/SKILL.md"):
                resolved = skill_file.resolve(strict=True)
                if not resolved.is_relative_to(expected_root):
                    raise CodexError(f"repository Skill escapes .codex/skills: {skill_file}")
                declared[skill_file.parent.name] = resolved
        found: set[str] = set()
        for entry in entries:
            name = entry.get("name")
            raw_path = entry.get("path")
            if not isinstance(name, str) or not isinstance(raw_path, str):
                continue
            path = Path(raw_path).resolve()
            if name in declared and path == declared[name]:
                found.add(name)
        missing = set(declared) - found
        if missing:
            raise CodexError("Codex did not discover repository Skills: " + ", ".join(sorted(missing)))
        return [
            {"name": name, "path": str(declared[name]), "sha256": _skill_hash(declared[name].parent)}
            for name in sorted(found)
        ]

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

    async def _open_thread(
        self,
        workspace: Path,
        tracker: TrackerAdapter,
        *,
        resume_thread_id: str | None,
    ) -> str:
        if resume_thread_id is not None:
            logger.info("resuming Codex thread %s", resume_thread_id)
            return await self._resume_thread(workspace, resume_thread_id)
        workspace_root = str(workspace.resolve())
        params: dict[str, Any] = {
            "approvalPolicy": self.config.approval_policy,
            "sandbox": self.config.thread_sandbox,
            "cwd": workspace_root,
            "runtimeWorkspaceRoots": [workspace_root],
            "dynamicTools": tracker.tool_specs(),
        }
        if self.config.model is not None:
            params["model"] = self.config.model
        if self.config.effort is not None:
            params["effort"] = self.config.effort
        result = await self._request(
            "thread/start",
            params,
        )
        thread = result.get("thread") if isinstance(result, dict) else None
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            raise CodexError(f"invalid thread/start response: {result!r}")
        return thread_id

    async def _resume_thread(self, workspace: Path, thread_id: str) -> str:
        workspace_root = str(workspace.resolve())
        params: dict[str, Any] = {
            "threadId": thread_id,
            "approvalPolicy": self.config.approval_policy,
            "sandbox": self.config.thread_sandbox,
            "cwd": workspace_root,
            "runtimeWorkspaceRoots": [workspace_root],
        }
        if self.config.model is not None:
            params["model"] = self.config.model
        result = await self._request("thread/resume", params)
        thread = result.get("thread") if isinstance(result, dict) else None
        resumed_id = thread.get("id") if isinstance(thread, dict) else None
        if resumed_id != thread_id:
            raise CodexError(f"invalid thread/resume response: {result!r}")
        return resumed_id

    async def _start_turn(
        self,
        workspace: Path,
        prompt: str,
        issue: dict[str, Any],
        thread_id: str,
    ) -> str:
        identifier = issue.get("identifier") or issue.get("id")
        sandbox_policy = dict(self.config.turn_sandbox_policy)
        if sandbox_policy.get("type") == "workspaceWrite":
            sandbox_policy["writableRoots"] = [str(workspace.resolve())]
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
            "cwd": str(workspace),
            "title": f"{identifier}: {issue.get('title', '')}",
            "approvalPolicy": self.config.approval_policy,
            "sandboxPolicy": sandbox_policy,
        }
        if self.config.effort is not None:
            params["effort"] = self.config.effort
        result = await self._request("turn/start", params)
        turn = result.get("turn") if isinstance(result, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            raise CodexError(f"invalid turn/start response: {result!r}")
        return turn_id

    async def _receive_turn(
        self,
        tracker: TrackerAdapter,
        lease: ClaimLease,
        thread_id: str,
    ) -> str:
        while True:
            # turn_timeout_ms is a silence timeout. Every successfully read
            # App Server message resets it; it is not a total Turn duration.
            message = await self._read_message(self.config.turn_timeout_ms)
            await self._emit(message)
            method = message.get("method")
            if method == "turn/completed":
                return "turn_completed"
            if method in {"turn/failed", "turn/cancelled"}:
                raise CodexError(f"Codex {method}: {message.get('params')!r}")
            if method == "item/tool/call" and "id" in message:
                name, arguments = _tool_call(message.get("params"))
                execution = await tracker.execute_tool(
                    lease,
                    name,
                    arguments,
                    thread_id=thread_id,
                )
                await self._emit(
                    {
                        "method": "control_plane/tool/completed",
                        "params": {
                            "tool": name,
                            "success": execution.response.get("success", False),
                            "result": execution.response.get("result"),
                        },
                    }
                )
                await self._send({"id": message["id"], "result": execution.response})
                if execution.stop_agent:
                    return "issue_released"
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
            raise CodexError(
                self._exit_details("Codex app-server closed stdin")
            ) from error

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
            raise CodexError(
                f"Codex app-server was silent for {timeout_ms}ms"
            ) from error
        if not line:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=1)
            raise CodexError(self._exit_details("Codex app-server exited"))
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            text = line.decode("utf-8", errors="replace").strip()
            raise CodexError(
                f"invalid JSON from Codex app-server: {text[:500]}"
            ) from error
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
        method = message.get("method")
        params = message.get("params")
        if method in {"item/started", "item/completed"} and isinstance(params, dict):
            item = params.get("item")
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type == "commandExecution":
                    command = str(item.get("command") or "").replace("\n", " ")[:300]
                    logger.info(
                        "codex %s type=%s status=%s exit=%s command=%s",
                        method,
                        item_type,
                        item.get("status"),
                        item.get("exitCode"),
                        command,
                    )
                elif item_type == "agentMessage" and method == "item/completed":
                    text = str(item.get("text") or "").replace("\n", " ")[:500]
                    logger.info("codex agentMessage=%s", text)
                else:
                    logger.info(
                        "codex %s type=%s status=%s",
                        method,
                        item_type,
                        item.get("status"),
                    )
        elif method in {"turn/started", "turn/completed", "turn/failed"}:
            logger.info("codex %s", method)
        else:
            logger.debug("codex event method=%s id=%s", method, message.get("id"))
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


def _continuation_prompt(
    issue: dict[str, Any], turn_number: int, max_turns: int
) -> str:
    identifier = issue.get("identifier") or issue.get("id") or "current Issue"
    return (
        f"Continue working on Issue {identifier} in this same live session "
        f"(turn {turn_number} of {max_turns}). The previous turn ended without "
        "submitting a final Issue result. Reuse the analysis, file changes, and sub-agent "
        "results already present in this thread and workspace; do not restart "
        "discovery. Complete the remaining scoped work, validate it, and call "
        "the appropriate Control Plane tool to complete the Issue, request human input, "
        "or record a blocker."
    )


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
    return (
        name if isinstance(name, str) else None,
        arguments if isinstance(arguments, dict) else {},
    )


def _skill_hash(skill_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in skill_root.rglob("*") if item.is_file()):
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(skill_root.resolve()):
            raise CodexError(f"repository Skill file escapes its directory: {path}")
        digest.update(path.relative_to(skill_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


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
