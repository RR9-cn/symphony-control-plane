from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import signal
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from symphony_windows.workflow import HookConfig, WorkspaceConfig


class WorkspaceError(RuntimeError):
    """Workspace creation or a lifecycle hook failed."""


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


@dataclass(frozen=True)
class PreparedWorkspace:
    path: Path
    created: bool


class WorkspaceManager:
    def __init__(self, config: WorkspaceConfig) -> None:
        self.config = config

    async def prepare(self, issue: dict[str, Any]) -> PreparedWorkspace:
        identifier = str(
            issue.get("workspace_identifier")
            or issue.get("identifier")
            or issue.get("id")
            or ""
        )
        if not identifier.strip():
            raise WorkspaceError("issue identifier is required")
        root = self.config.root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        key = workspace_key(identifier)
        candidate = root / key
        created = not candidate.exists()
        candidate.mkdir(parents=False, exist_ok=True)
        resolved = candidate.resolve()
        if resolved == root or not resolved.is_relative_to(root):
            raise WorkspaceError(f"workspace escapes configured root: {resolved}")
        prepared = PreparedWorkspace(path=resolved, created=created)
        if created:
            try:
                await self.run_hook("after_create", issue, resolved)
            except BaseException:
                # No Agent has run in a newly created workspace. Remove a failed
                # partial clone so the next dispatch can execute after_create again.
                if resolved != root and resolved.is_relative_to(root):
                    await asyncio.shield(_remove_tree(resolved))
                raise
        return prepared

    async def before_run(self, issue: dict[str, Any], workspace: Path) -> None:
        await self.run_hook("before_run", issue, workspace)

    async def after_run(self, issue: dict[str, Any], workspace: Path) -> None:
        await self.run_hook("after_run", issue, workspace, ignore_failure=True)

    async def remove(self, issue: dict[str, Any]) -> bool:
        """Run before_remove and safely remove one terminal Issue workspace."""
        identifier = str(
            issue.get("workspace_identifier")
            or issue.get("identifier")
            or issue.get("id")
            or ""
        )
        if not identifier.strip():
            raise WorkspaceError("issue identifier is required for cleanup")
        root = self.config.root.resolve()
        workspace = (root / workspace_key(identifier)).resolve()
        if workspace == root or not workspace.is_relative_to(root):
            raise WorkspaceError(f"workspace cleanup escapes configured root: {workspace}")
        if not workspace.exists():
            return False
        await self.run_hook("before_remove", issue, workspace, ignore_failure=True)
        try:
            await _remove_tree(workspace)
        except OSError as error:
            raise WorkspaceError(f"cannot remove Issue workspace {workspace}: {error}") from error
        return True

    async def run_hook(
        self,
        name: str,
        issue: dict[str, Any],
        workspace: Path,
        *,
        ignore_failure: bool = False,
    ) -> None:
        hooks: HookConfig = self.config.hooks
        script = getattr(hooks, name)
        if not script:
            return
        executable = shutil.which("pwsh") or shutil.which("powershell.exe")
        if executable is None:
            if ignore_failure:
                return
            raise WorkspaceError("PowerShell is required for Windows workspace hooks")
        env = os.environ.copy()
        env.update(
            {
                "SYMPHONY_ISSUE_ID": str(issue.get("id", "")),
                "SYMPHONY_ISSUE_IDENTIFIER": str(
                    issue.get("identifier") or issue.get("id") or ""
                ),
                "SYMPHONY_ISSUE_JSON": json.dumps(issue, ensure_ascii=False),
                "SYMPHONY_WORKSPACE": str(workspace),
                "SYMPHONY_PROJECT_ID": str(issue.get("project_id") or os.getenv("SYMPHONY_PROJECT_ID", "")),
                "SYMPHONY_PROJECT_REPOSITORY": os.getenv("SYMPHONY_PROJECT_REPOSITORY", ""),
                "SYMPHONY_PROJECT_DEFAULT_BRANCH": os.getenv("SYMPHONY_PROJECT_DEFAULT_BRANCH", ""),
                "SYMPHONY_SOURCE_COMMIT": str(issue.get("source_commit", "")),
                "SYMPHONY_WORKFLOW_REVISION": str(issue.get("workflow_revision", "")),
            }
        )
        subprocess_options: dict[str, object] = {}
        if os.name != "nt":
            # A separate process group lets cancellation terminate hook children
            # as well as the PowerShell process itself.
            subprocess_options["start_new_session"] = True
        process = await asyncio.create_subprocess_exec(
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
            cwd=workspace,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **subprocess_options,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=hooks.timeout_ms / 1000,
            )
        except TimeoutError as error:
            await asyncio.shield(_terminate_process_tree(process))
            if ignore_failure:
                return
            raise WorkspaceError(f"hook {name} timed out") from error
        except BaseException:
            # asyncio cancellation does not automatically terminate subprocesses.
            # Kill the complete PowerShell/Git tree before prepare() removes the
            # partial workspace, otherwise a surviving clone can recreate files.
            await asyncio.shield(_terminate_process_tree(process))
            raise
        if process.returncode != 0 and not ignore_failure:
            details = (stderr or stdout).decode("utf-8", errors="replace").strip()
            raise WorkspaceError(f"hook {name} failed ({process.returncode}): {details}")


def workspace_key(identifier: str) -> str:
    original = identifier.strip()
    sanitized = _UNSAFE.sub("_", original).rstrip(". ")
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:16]
    changed = sanitized != original or not sanitized
    if not sanitized:
        sanitized = "issue"
    if sanitized.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        changed = True
    if len(sanitized) > 96:
        sanitized = sanitized[:96].rstrip(". ")
        changed = True
    return f"{sanitized}-{digest}" if changed else sanitized


def _remove_readonly(function: Any, path: str, _error: object) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    """Best-effort termination for a hook and every process it spawned."""
    if os.name == "nt" and process.pid is not None:
        with contextlib.suppress(OSError, TimeoutError):
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.communicate(), timeout=10)
    elif process.pid is not None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)

    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    with contextlib.suppress(ProcessLookupError, TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=5)


async def _remove_tree(path: Path, attempts: int = 5) -> None:
    """Remove a workspace after subprocess shutdown, retrying Windows handle races."""
    last_error: OSError | None = None
    for attempt in range(attempts):
        if not path.exists():
            return
        try:
            await asyncio.to_thread(shutil.rmtree, path, onerror=_remove_readonly)
        except FileNotFoundError:
            return
        except OSError as error:
            last_error = error
        if not path.exists():
            return
        if attempt + 1 < attempts:
            await asyncio.sleep(0.1 * (attempt + 1))
    if last_error is not None:
        raise last_error
    raise OSError(f"workspace directory still exists after cleanup: {path}")
