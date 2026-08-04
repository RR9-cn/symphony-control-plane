from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
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
            or issue.get("feature_id")
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

    def materialize_input_artifacts(
        self, issue: dict[str, Any], workspace: Path
    ) -> tuple[str, ...]:
        artifacts = issue.get("input_artifacts") or []
        dependencies = issue.get("dependencies") or []
        if not isinstance(artifacts, list) or not isinstance(dependencies, list):
            raise WorkspaceError("WorkItem input artifacts or dependencies are invalid")
        materialized: list[str] = []
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise WorkspaceError("WorkItem input artifact must be an object")
            relative = _artifact_relative_path(artifact.get("path"))
            expected_sha = artifact.get("sha256")
            target = (workspace / relative).resolve()
            if target.is_file() and (
                not expected_sha or _file_sha256(target) == expected_sha
            ):
                materialized.append(relative.as_posix())
                continue

            source = None
            for dependency_id in dependencies:
                dependency_workspace = (
                    self.config.root.resolve() / workspace_key(str(dependency_id))
                )
                candidate = (dependency_workspace / relative).resolve()
                if (
                    candidate.is_file()
                    and candidate.is_relative_to(dependency_workspace.resolve())
                    and (
                        not expected_sha or _file_sha256(candidate) == expected_sha
                    )
                ):
                    source = candidate
                    break
            if source is None:
                raise WorkspaceError(
                    f"input artifact is unavailable: {relative.as_posix()}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if expected_sha and _file_sha256(target) != expected_sha:
                raise WorkspaceError(
                    f"input artifact checksum mismatch: {relative.as_posix()}"
                )
            materialized.append(relative.as_posix())
        return tuple(materialized)

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


def _artifact_relative_path(value: object) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise WorkspaceError("input artifact path must be a relative POSIX path")
    posix = PurePosixPath(value)
    if posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts):
        raise WorkspaceError(f"unsafe input artifact path: {value}")
    return Path(*posix.parts)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
