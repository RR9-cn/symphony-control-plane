from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from symphony_windows.workspace import workspace_key


class DeliveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class IssueDeliveryManager:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()

    def workspace(self, issue_id: str) -> Path:
        return self.workspace_root / workspace_key(issue_id)

    async def prepare_local_commit(self, issue_id: str, title: str) -> tuple[str, str]:
        workspace = self.workspace(issue_id)
        await self._require_repository(workspace)
        backup = workspace / ".symphony" / "agent-assets-backup"
        if backup.exists():
            raise DeliveryError("Agent assets are still installed in the workspace")
        branch = f"codex/{issue_id.lower()}"
        await _run("git", "checkout", "-B", branch, cwd=workspace)
        await _run("git", "add", "-A", cwd=workspace)
        await _run("git", "reset", "--", ".agents", ".symphony", cwd=workspace, allow_failure=True)
        staged = await _run_result("git", "diff", "--cached", "--quiet", cwd=workspace)
        if staged.returncode == 1:
            await _run("git", "commit", "-m", f"feat: {title}", cwd=workspace)
        elif staged.returncode != 0:
            raise DeliveryError(_process_error(staged, "git diff --cached failed"))
        commit = (await _run("git", "rev-parse", "HEAD", cwd=workspace)).strip()
        return branch, commit

    async def publish(
        self,
        issue_id: str,
        *,
        repository_url: str,
        base_branch: str,
        branch: str,
        commit: str,
        title: str,
        body: str,
    ) -> str:
        workspace = self.workspace(issue_id)
        await self._require_repository(workspace)
        actual = (await _run("git", "rev-parse", "HEAD", cwd=workspace)).strip()
        if actual != commit:
            raise DeliveryError("Issue workspace HEAD does not match local delivery commit")
        remote_url = await _resolve_remote(repository_url)
        remote_name = "symphony-origin"
        remotes = (await _run("git", "remote", cwd=workspace)).splitlines()
        if remote_name in remotes:
            await _run("git", "remote", "set-url", remote_name, remote_url, cwd=workspace)
        else:
            await _run("git", "remote", "add", remote_name, remote_url, cwd=workspace)
        await _run("git", "push", "--set-upstream", remote_name, f"HEAD:{branch}", cwd=workspace)
        existing = await _run_result(
            "gh", "pr", "view", branch, "--repo", _github_repo(remote_url), "--json", "url", "--jq", ".url", cwd=workspace
        )
        if existing.returncode == 0 and existing.stdout.strip():
            return existing.stdout.strip()
        return (
            await _run(
                "gh", "pr", "create", "--repo", _github_repo(remote_url), "--base", base_branch,
                "--head", branch, "--title", title, "--body", body, cwd=workspace
            )
        ).strip()

    async def verify_merged(self, repository_url: str, pull_request: str) -> None:
        remote_url = await _resolve_remote(repository_url)
        result = await _run(
            "gh", "pr", "view", pull_request, "--repo", _github_repo(remote_url), "--json", "state", "--jq", ".state",
            cwd=self.workspace_root,
        )
        if result.strip().upper() != "MERGED":
            raise DeliveryError("Pull Request has not been merged")

    @staticmethod
    async def _require_repository(workspace: Path) -> None:
        if not workspace.is_dir():
            raise DeliveryError(f"Issue workspace does not exist: {workspace}")
        inside = (await _run("git", "rev-parse", "--is-inside-work-tree", cwd=workspace)).strip()
        if inside != "true":
            raise DeliveryError("Issue workspace is not a Git repository")


async def _resolve_remote(value: str) -> str:
    candidate = Path(value)
    if candidate.is_absolute() and candidate.is_dir():
        return (await _run("git", "remote", "get-url", "origin", cwd=candidate)).strip()
    return value


def _github_repo(remote: str) -> str:
    if remote.startswith("git@github.com:"):
        value = remote.removeprefix("git@github.com:")
    else:
        parsed = urlparse(remote)
        if parsed.hostname != "github.com":
            raise DeliveryError("Pull Request delivery currently supports GitHub repositories")
        value = parsed.path.lstrip("/")
    value = re.sub(r"\.git$", "", value)
    if value.count("/") != 1:
        raise DeliveryError("cannot determine GitHub owner/repository")
    return value


async def _run(*args: str, cwd: Path, allow_failure: bool = False) -> str:
    result = await _run_result(*args, cwd=cwd)
    if result.returncode != 0 and not allow_failure:
        raise DeliveryError(_process_error(result, "command failed"))
    return result.stdout


async def _run_result(*args: str, cwd: Path) -> CommandResult:
    process = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    return CommandResult(
        returncode=process.returncode or 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


def _process_error(result: CommandResult, fallback: str) -> str:
    stderr = result.stderr.strip()
    stdout = result.stdout.strip()
    return stderr or stdout or fallback
