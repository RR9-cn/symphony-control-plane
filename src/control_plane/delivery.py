from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from symphony_windows.workspace import workspace_key


class DeliveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class GitLabRepository:
    api_base: str
    project_path: str
    credential_protocol: str
    credential_host: str


class IssueDeliveryManager:
    def __init__(self, workspace_root: Path, *, gitlab_token: str | None = None) -> None:
        self.workspace_root = workspace_root.resolve()
        self.gitlab_token = gitlab_token

    def workspace(self, issue_id: str) -> Path:
        return self.workspace_root / workspace_key(issue_id)

    async def repository(self, issue_id: str) -> Path:
        workspace = self.workspace(issue_id).resolve()
        if not workspace.is_dir():
            raise DeliveryError(f"Issue workspace does not exist: {workspace}")
        if not workspace.is_relative_to(self.workspace_root):
            raise DeliveryError("Issue workspace escapes the configured workspace root")
        candidates = [workspace, workspace / "repo"]
        repositories: list[Path] = []
        for candidate in candidates:
            if not candidate.is_dir() or not (candidate / ".git").exists():
                continue
            top_level = Path(
                (await _run("git", "rev-parse", "--show-toplevel", cwd=candidate)).strip()
            ).resolve()
            if top_level != candidate.resolve():
                raise DeliveryError(f"Git repository root does not match delivery path: {candidate}")
            repositories.append(candidate.resolve())
        if not repositories:
            raise DeliveryError(
                "Issue workspace does not contain a Git repository at its root or repo/"
            )
        if len(repositories) > 1:
            raise DeliveryError("Issue workspace contains multiple delivery repositories")
        return repositories[0]

    async def prepare_local_commit(self, issue_id: str, title: str) -> tuple[str, str]:
        issue_workspace = self.workspace(issue_id)
        repository = await self.repository(issue_id)
        backup = issue_workspace / ".symphony" / "agent-assets-backup"
        if backup.exists():
            raise DeliveryError("Agent assets are still installed in the workspace")
        branch = f"codex/{issue_id.lower()}"
        await _run("git", "checkout", "-B", branch, cwd=repository)
        await _run("git", "add", "-A", cwd=repository)
        await _run(
            "git",
            "reset",
            "--",
            ".agents",
            ".symphony",
            cwd=repository,
            allow_failure=True,
        )
        staged = await _run_result("git", "diff", "--cached", "--quiet", cwd=repository)
        if staged.returncode == 1:
            await _run("git", "commit", "-m", f"feat: {title}", cwd=repository)
        elif staged.returncode != 0:
            raise DeliveryError(_process_error(staged, "git diff --cached failed"))
        commit = (await _run("git", "rev-parse", "HEAD", cwd=repository)).strip()
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
        repository = await self.repository(issue_id)
        actual = (await _run("git", "rev-parse", "HEAD", cwd=repository)).strip()
        if actual != commit:
            raise DeliveryError("Issue workspace HEAD does not match local delivery commit")
        remote_url = await _resolve_remote(repository_url)
        remote_name = "symphony-origin"
        remotes = (await _run("git", "remote", cwd=repository)).splitlines()
        if remote_name in remotes:
            await _run("git", "remote", "set-url", remote_name, remote_url, cwd=repository)
        else:
            await _run("git", "remote", "add", remote_name, remote_url, cwd=repository)
        if _is_github_remote(remote_url):
            await _run(
                "git",
                "push",
                "--set-upstream",
                remote_name,
                f"HEAD:{branch}",
                cwd=repository,
            )
            return await _create_github_pull_request(
                repository, remote_url, base_branch, branch, title, body
            )
        if self.gitlab_token is None:
            return await _push_gitlab_merge_request(
                repository,
                remote_name,
                base_branch,
                branch,
                title,
                body,
            )
        await _run(
            "git",
            "push",
            "--set-upstream",
            remote_name,
            f"HEAD:{branch}",
            cwd=repository,
        )
        return await self._create_gitlab_merge_request(
            repository, remote_url, base_branch, branch, title, body
        )

    async def _create_gitlab_merge_request(
        self,
        repository: Path,
        remote_url: str,
        base_branch: str,
        branch: str,
        title: str,
        body: str,
    ) -> str:
        target = _gitlab_repository(remote_url)
        if self.gitlab_token is None:
            raise DeliveryError("GitLab API delivery requires ACP_GITLAB_TOKEN")
        endpoint = (
            f"{target.api_base}/api/v4/projects/"
            f"{quote(target.project_path, safe='')}/merge_requests"
        )
        existing = await _gitlab_api(
            "GET",
            endpoint,
            self.gitlab_token,
            params={
                "state": "opened",
                "source_branch": branch,
                "target_branch": base_branch,
            },
        )
        if isinstance(existing, list):
            for item in existing:
                if isinstance(item, dict) and isinstance(item.get("web_url"), str):
                    return item["web_url"]
        created = await _gitlab_api(
            "POST",
            endpoint,
            self.gitlab_token,
            json_body={
                "source_branch": branch,
                "target_branch": base_branch,
                "title": title,
                "description": body,
                "remove_source_branch": False,
            },
        )
        if not isinstance(created, dict) or not isinstance(created.get("web_url"), str):
            raise DeliveryError("GitLab Merge Request response is missing web_url")
        return created["web_url"]

    async def verify_merged(self, repository_url: str, review_request: str) -> None:
        remote_url = await _resolve_remote(repository_url)
        if not _is_github_remote(remote_url):
            target = _gitlab_repository(remote_url)
            iid = _gitlab_merge_request_iid(target, review_request)
            if self.gitlab_token is None:
                raise DeliveryError(
                    "GitLab merge verification requires ACP_GITLAB_TOKEN"
                )
            result = await _gitlab_api(
                "GET",
                f"{target.api_base}/api/v4/projects/"
                f"{quote(target.project_path, safe='')}/merge_requests/{iid}",
                self.gitlab_token,
            )
            if not isinstance(result, dict) or result.get("state") != "merged":
                raise DeliveryError("Merge Request has not been merged")
            return
        result = await _run(
            "gh",
            "pr",
            "view",
            review_request,
            "--repo",
            _github_repo(remote_url),
            "--json",
            "state",
            "--jq",
            ".state",
            cwd=self.workspace_root,
        )
        if result.strip().upper() != "MERGED":
            raise DeliveryError("Pull Request has not been merged")


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


def _is_github_remote(remote: str) -> bool:
    if remote.startswith("git@github.com:"):
        return True
    return urlparse(remote).hostname == "github.com"


async def _create_github_pull_request(
    repository: Path,
    remote_url: str,
    base_branch: str,
    branch: str,
    title: str,
    body: str,
) -> str:
    existing = await _run_result(
        "gh",
        "pr",
        "view",
        branch,
        "--repo",
        _github_repo(remote_url),
        "--json",
        "url",
        "--jq",
        ".url",
        cwd=repository,
    )
    if existing.returncode == 0 and existing.stdout.strip():
        return existing.stdout.strip()
    return (
        await _run(
            "gh",
            "pr",
            "create",
            "--repo",
            _github_repo(remote_url),
            "--base",
            base_branch,
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
            cwd=repository,
        )
    ).strip()


async def _push_gitlab_merge_request(
    repository: Path,
    remote_name: str,
    base_branch: str,
    branch: str,
    title: str,
    body: str,
) -> str:
    result = await _run_result(
        "git",
        "push",
        "--set-upstream",
        "-o",
        "merge_request.create",
        "-o",
        f"merge_request.target={base_branch}",
        "-o",
        f"merge_request.title={title}",
        "-o",
        f"merge_request.description={body}",
        remote_name,
        f"HEAD:{branch}",
        cwd=repository,
    )
    if result.returncode != 0:
        raise DeliveryError(_process_error(result, "GitLab push failed"))
    merge_request = _gitlab_merge_request_url(f"{result.stdout}\n{result.stderr}")
    if merge_request is None:
        raise DeliveryError(
            "GitLab branch was pushed but no Merge Request URL was returned; "
            "configure ACP_GITLAB_TOKEN or create the Merge Request manually"
        )
    return merge_request


def _gitlab_merge_request_url(output: str) -> str | None:
    match = re.search(r"https?://[^\s]+/-/merge_requests/\d+", output)
    return match.group(0).rstrip(".,;)") if match is not None else None


def _gitlab_repository(remote: str) -> GitLabRepository:
    if remote.startswith("git@"):
        match = re.fullmatch(r"git@([^:]+):(.+)", remote)
        if match is None:
            raise DeliveryError("cannot determine GitLab repository")
        host, project_path = match.groups()
        api_base = f"https://{host}"
        protocol = "https"
        credential_host = host
    else:
        parsed = urlparse(remote)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise DeliveryError("GitLab delivery requires an HTTP(S) or git@ remote")
        project_path = parsed.path.lstrip("/")
        api_base = f"{parsed.scheme}://{parsed.netloc}"
        protocol = parsed.scheme
        credential_host = parsed.netloc
    project_path = re.sub(r"\.git$", "", project_path).strip("/")
    if "/" not in project_path:
        raise DeliveryError("cannot determine GitLab namespace/repository")
    return GitLabRepository(
        api_base=api_base,
        project_path=project_path,
        credential_protocol=protocol,
        credential_host=credential_host,
    )


def _gitlab_merge_request_iid(
    expected: GitLabRepository, merge_request_url: str
) -> int:
    parsed = urlparse(merge_request_url)
    actual_base = f"{parsed.scheme}://{parsed.netloc}"
    marker = "/-/merge_requests/"
    if actual_base != expected.api_base or marker not in parsed.path:
        raise DeliveryError("Merge Request URL does not match the Issue repository")
    project_path, iid_value = parsed.path.split(marker, 1)
    if project_path.strip("/") != expected.project_path:
        raise DeliveryError("Merge Request project does not match the Issue repository")
    iid = iid_value.strip("/")
    if not iid.isdigit() or int(iid) < 1:
        raise DeliveryError("cannot determine GitLab Merge Request IID")
    return int(iid)


async def _gitlab_api(
    method: str,
    url: str,
    token: str,
    *,
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> Any:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(
                method,
                url,
                headers={"PRIVATE-TOKEN": token},
                params=params,
                json=json_body,
            )
    except httpx.HTTPError as error:
        raise DeliveryError(f"GitLab API request failed: {error}") from error
    if response.status_code < 200 or response.status_code >= 300:
        detail = response.text.strip()[:500]
        raise DeliveryError(
            f"GitLab API returned HTTP {response.status_code}: {detail or 'request failed'}"
        )
    try:
        return response.json()
    except ValueError as error:
        raise DeliveryError("GitLab API returned invalid JSON") from error


async def _run(*args: str, cwd: Path, allow_failure: bool = False) -> str:
    result = await _run_result(*args, cwd=cwd)
    if result.returncode != 0 and not allow_failure:
        raise DeliveryError(_process_error(result, "command failed"))
    return result.stdout


async def _run_result(*args: str, cwd: Path) -> CommandResult:
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
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
