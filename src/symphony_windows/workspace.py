from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
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
                    shutil.rmtree(resolved, ignore_errors=True)
                raise
        return prepared

    async def before_run(self, issue: dict[str, Any], workspace: Path) -> None:
        await self.run_hook("before_run", issue, workspace)

    async def after_run(self, issue: dict[str, Any], workspace: Path) -> None:
        await self.run_hook("after_run", issue, workspace, ignore_failure=True)

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
            }
        )
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
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=hooks.timeout_ms / 1000,
            )
        except TimeoutError as error:
            process.kill()
            await process.wait()
            if ignore_failure:
                return
            raise WorkspaceError(f"hook {name} timed out") from error
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
