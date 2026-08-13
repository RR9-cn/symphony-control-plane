from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path
from typing import Any


class WorkspaceSummaryError(RuntimeError):
    pass


def empty_change_summary(*, available: bool = False) -> dict[str, Any]:
    return {
        "available": available,
        "overview": None,
        "files_total": 0,
        "files_added": 0,
        "files_modified": 0,
        "files_deleted": 0,
        "files_renamed": 0,
        "files_untracked": 0,
        "additions": 0,
        "deletions": 0,
        "binary_files": 0,
        "commit_count": 0,
        "areas": [],
        "changed_paths": [],
        "commit_subjects": [],
    }


def build_change_summary(
    changed_files_output: str,
    status_output: str,
    numstat_output: str,
    commits_output: str,
    *,
    overview: str | None = None,
) -> dict[str, Any]:
    changes: dict[str, str] = {}
    for line in changed_files_output.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        code = parts[0][:1]
        path = parts[-1]
        changes[path] = code if code in {"A", "D", "R"} else "M"
    for line in status_output.splitlines():
        if line.startswith("?? "):
            changes.setdefault(line[3:], "?")

    counts = Counter(changes.values())
    additions = 0
    deletions = 0
    binary_files = 0
    for line in numstat_output.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        if parts[0] == "-" or parts[1] == "-":
            binary_files += 1
            continue
        try:
            additions += int(parts[0])
            deletions += int(parts[1])
        except ValueError:
            continue

    paths = sorted(changes)
    area_counts = Counter(
        path.replace("\\", "/").split("/", 1)[0]
        if "/" in path.replace("\\", "/")
        else "根目录"
        for path in paths
    )
    commits = [line for line in commits_output.splitlines() if line.strip()]
    normalized_overview = overview.strip()[:4000] if overview and overview.strip() else None
    if normalized_overview is None and commits:
        subjects = [line.split("\t", 1)[-1].strip() for line in commits]
        normalized_overview = "本次改动包含：" + "；".join(subjects[:5]) + "。"
    summary = empty_change_summary(available=True)
    summary.update(
        {
            "overview": normalized_overview,
            "files_total": len(paths),
            "files_added": counts["A"],
            "files_modified": counts["M"],
            "files_deleted": counts["D"],
            "files_renamed": counts["R"],
            "files_untracked": counts["?"],
            "additions": additions,
            "deletions": deletions,
            "binary_files": binary_files,
            "commit_count": len(commits),
            "areas": [area for area, _count in area_counts.most_common(5)],
            "changed_paths": paths[:200],
            "commit_subjects": commits[:20],
        }
    )
    return summary


async def collect_change_summary(
    workspace: Path, base_commit: str, *, overview: str | None = None
) -> dict[str, Any]:
    resolved = workspace.resolve(strict=True)
    if not resolved.is_dir() or not (resolved / ".git").exists():
        raise WorkspaceSummaryError("Issue workspace Git checkout is unavailable")
    top_level = Path(
        await _git_output(resolved, "rev-parse", "--show-toplevel")
    ).resolve()
    if top_level != resolved:
        raise WorkspaceSummaryError(
            "Issue workspace Git repository root does not match its path"
        )
    pathspec = ("--", ".", ":(exclude).symphony")
    changed, status, numstat, commits = await asyncio.gather(
        _git_output(resolved, "diff", "--name-status", base_commit, *pathspec),
        _git_output(
            resolved, "status", "--short", "--untracked-files=all", *pathspec
        ),
        _git_output(resolved, "diff", "--numstat", base_commit, *pathspec),
        _git_output(
            resolved,
            "log",
            "--format=%h%x09%s",
            f"{base_commit}..HEAD",
            *pathspec,
        ),
    )
    return build_change_summary(
        changed, status, numstat, commits, overview=overview
    )


async def _git_output(workspace: Path, *arguments: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(workspace),
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    except TimeoutError as error:
        process.kill()
        await process.wait()
        raise WorkspaceSummaryError("workspace Git summary timed out") from error
    if process.returncode != 0:
        raise WorkspaceSummaryError(
            stderr.decode("utf-8", errors="replace").strip()
            or "workspace Git summary failed"
        )
    return stdout.decode("utf-8", errors="replace").rstrip()
