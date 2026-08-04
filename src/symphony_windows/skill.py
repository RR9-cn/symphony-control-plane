from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from symphony_windows.workflow import (
    AgentProfileConfig,
    SkillRepositoryConfig,
    WorkflowError,
)


class SkillError(WorkflowError):
    """A pinned Skill repository or Skill package is unsafe or incompatible."""


@dataclass(frozen=True)
class SkillInfo:
    name: str
    source: Path
    content_hash: str
    referenced_files: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    human_confirmation: tuple[str, ...]
    external_writes: tuple[str, ...]
    required_tools: tuple[str, ...]
    required_credentials: tuple[str, ...]
    required_skills: tuple[str, ...]

    def snapshot(self, revision: str) -> dict[str, Any]:
        return {
            "revision": revision,
            "content_hash": self.content_hash,
            "required_tools": list(self.required_tools),
            "required_credentials": list(self.required_credentials),
            "external_writes": list(self.external_writes),
            "human_confirmation": list(self.human_confirmation),
        }


class SkillManager:
    def __init__(
        self,
        config: SkillRepositoryConfig,
        profiles: tuple[AgentProfileConfig, ...],
    ) -> None:
        self.config = config
        self.profiles = profiles
        self._checkout: Path | None = None
        self._skills: dict[str, SkillInfo] = {}
        self._initialize_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._checkout is not None:
            return
        async with self._initialize_lock:
            if self._checkout is not None:
                return
            checkout = await self._checkout_revision()
            skills_root = _contained_directory(checkout, self.config.skills_path)
            requested = sorted(
                {skill for profile in self.profiles for skill in profile.skills}
            )
            skills: dict[str, SkillInfo] = {}
            for name in requested:
                source = _contained_directory(skills_root, name)
                skills[name] = validate_skill_package(name, source)
            for profile in self.profiles:
                allowed = set(profile.skills)
                for name in profile.skills:
                    missing_dependencies = set(skills[name].required_skills) - allowed
                    if missing_dependencies:
                        missing = ", ".join(sorted(missing_dependencies))
                        raise SkillError(
                            f"skill {name} requires skills outside profile "
                            f"{profile.name} allowlist: {missing}"
                        )
            self._validate_runtime_requirements(skills.values())
            self._skills = skills
            self._checkout = checkout

    def profile_snapshot(self, profile: AgentProfileConfig) -> dict[str, Any]:
        self._require_initialized()
        snapshot = profile.snapshot()
        snapshot["skill_repository"] = {
            "revision": self.config.revision,
            "source_hash": hashlib.sha256(
                self.config.url.encode("utf-8")
            ).hexdigest(),
        }
        snapshot["skills"] = {
            name: self._skills[name].snapshot(self.config.revision)
            for name in profile.skills
        }
        return snapshot

    def claim_profile(self, profile: AgentProfileConfig) -> dict[str, Any]:
        return {
            "name": profile.name,
            "version": profile.version,
            "config": self.profile_snapshot(profile),
        }

    def secret_environment_names(self, profile: AgentProfileConfig) -> tuple[str, ...]:
        self._require_initialized()
        return tuple(
            sorted(
                {
                    name
                    for skill_name in profile.skills
                    for name in self._skills[skill_name].required_credentials
                }
            )
        )

    def install(self, profile: AgentProfileConfig, workspace: Path) -> dict[str, Any]:
        self._require_initialized()
        root = workspace.resolve()
        agents_root = root / ".agents"
        if agents_root.exists() and agents_root.is_symlink():
            raise SkillError(f"workspace .agents directory must not be a symlink: {agents_root}")
        agents_root.mkdir(parents=True, exist_ok=True)
        if not agents_root.resolve().is_relative_to(root):
            raise SkillError("workspace .agents directory escapes the workspace")

        target = agents_root / "skills"
        staging = agents_root / f"skills.staging-{uuid.uuid4().hex}"
        backup = agents_root / f"skills.backup-{uuid.uuid4().hex}"
        try:
            staging.mkdir()
            for name in profile.skills:
                shutil.copytree(self._skills[name].source, staging / name)
            if target.exists():
                if target.is_symlink() or not target.resolve().is_relative_to(root):
                    raise SkillError("workspace Skill target is unsafe")
                target.replace(backup)
            staging.replace(target)
            if backup.exists():
                shutil.rmtree(backup)
        except Exception:
            if not target.exists() and backup.exists():
                backup.replace(target)
            if staging.exists():
                shutil.rmtree(staging)
            raise

        lock = self.profile_snapshot(profile)
        lock_path = agents_root / "skills.lock.json"
        lock_path.write_text(
            json.dumps(lock, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return lock

    async def _checkout_revision(self) -> Path:
        source_key = hashlib.sha256(
            f"{self.config.url}\0{self.config.revision}".encode("utf-8")
        ).hexdigest()[:20]
        cache_root = self.config.cache_root.resolve()
        cache_root.mkdir(parents=True, exist_ok=True)
        target = cache_root / f"repository-{source_key}"
        if target.exists():
            await self._verify_checkout(target)
            return target

        staging = cache_root / f"repository-{source_key}.staging-{uuid.uuid4().hex}"
        try:
            await _run_git(
                "clone",
                "--no-checkout",
                "--filter=blob:none",
                "--quiet",
                self.config.url,
                str(staging),
            )
            await _run_git(
                "-C",
                str(staging),
                "checkout",
                "--detach",
                "--quiet",
                self.config.revision,
            )
            await self._verify_checkout(staging)
            try:
                staging.replace(target)
            except FileExistsError:
                shutil.rmtree(staging)
                await self._verify_checkout(target)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        return target

    async def _verify_checkout(self, checkout: Path) -> None:
        if checkout.is_symlink() or not (checkout / ".git").is_dir():
            raise SkillError(f"invalid Skill repository cache: {checkout}")
        actual = (
            await _run_git("-C", str(checkout), "rev-parse", "HEAD")
        ).strip().lower()
        if actual != self.config.revision:
            raise SkillError(
                f"Skill repository cache revision mismatch: expected "
                f"{self.config.revision}, got {actual}"
            )

    @staticmethod
    def _validate_runtime_requirements(skills: Any) -> None:
        for skill in skills:
            for tool in skill.required_tools:
                if shutil.which(tool) is None:
                    raise SkillError(f"skill {skill.name} requires missing tool: {tool}")
            for variable in skill.required_credentials:
                if not os.getenv(variable):
                    raise SkillError(
                        f"skill {skill.name} requires missing credential environment: "
                        f"{variable}"
                    )

    def _require_initialized(self) -> None:
        if self._checkout is None:
            raise SkillError("SkillManager is not initialized")


async def _run_git(*arguments: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise SkillError("git is required to resolve the pinned Skill repository")
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    process = await asyncio.create_subprocess_exec(
        executable,
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
    except TimeoutError as error:
        process.kill()
        await process.wait()
        raise SkillError(f"git command timed out: {' '.join(arguments[:3])}") from error
    if process.returncode != 0:
        details = stderr.decode("utf-8", errors="replace").strip()
        raise SkillError(f"git command failed ({process.returncode}): {details}")
    return stdout.decode("utf-8", errors="replace")


def validate_skill_package(name: str, source: Path) -> SkillInfo:
    if source.is_symlink():
        raise SkillError(f"skill directory must not be a symlink: {name}")
    for entry in source.rglob("*"):
        if entry.is_symlink():
            raise SkillError(f"skill {name} contains a symlink: {entry.relative_to(source)}")
    instruction = source / "SKILL.md"
    if not instruction.is_file():
        raise SkillError(f"skill {name} is missing SKILL.md")
    text = instruction.read_text(encoding="utf-8")
    front_matter, body = _parse_skill_front_matter(name, text)
    declared_name = front_matter.get("name")
    description = front_matter.get("description")
    if declared_name != name:
        raise SkillError(f"skill {name} front matter name must match its directory")
    if not isinstance(description, str) or not description.strip():
        raise SkillError(f"skill {name} front matter description is required")

    metadata = _optional_mapping(front_matter.get("metadata"), f"skill {name} metadata")
    fshows = _optional_mapping(metadata.get("fshows"), f"skill {name} metadata.fshows")
    artifact_paths = _string_list(fshows, "artifact_paths", name)
    human_confirmation = _string_list(fshows, "human_confirmation", name)
    external_writes = _string_list(fshows, "external_writes", name)
    required_tools = _string_list(fshows, "required_tools", name)
    required_credentials = _string_list(fshows, "required_credentials", name)
    required_skills = _string_list(fshows, "required_skills", name)

    for path in artifact_paths:
        _validate_artifact_path(name, path)
    if external_writes and not human_confirmation:
        raise SkillError(
            f"skill {name} declares external_writes without human_confirmation"
        )
    for tool in required_tools:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", tool):
            raise SkillError(f"skill {name} has invalid required tool name: {tool}")
    for variable in required_credentials:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", variable):
            raise SkillError(
                f"skill {name} required credential must be an environment name: {variable}"
            )
    for dependency in required_skills:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", dependency):
            raise SkillError(f"skill {name} has invalid required skill: {dependency}")

    references = _discover_references(body)
    for reference in references:
        _contained_file(source, reference, name)
    _reject_deprecated_artifact_aliases(name, body)
    return SkillInfo(
        name=name,
        source=source,
        content_hash=_directory_hash(source),
        referenced_files=references,
        artifact_paths=artifact_paths,
        human_confirmation=human_confirmation,
        external_writes=external_writes,
        required_tools=required_tools,
        required_credentials=required_credentials,
        required_skills=required_skills,
    )


def _parse_skill_front_matter(name: str, text: str) -> tuple[dict[str, Any], str]:
    normalized = text.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        raise SkillError(f"skill {name} SKILL.md requires YAML front matter")
    end = normalized.find("\n---\n", 4)
    if end < 0:
        raise SkillError(f"skill {name} SKILL.md front matter is not terminated")
    try:
        decoded = yaml.safe_load(normalized[4:end]) or {}
    except yaml.YAMLError as error:
        raise SkillError(f"skill {name} has invalid YAML front matter: {error}") from error
    if not isinstance(decoded, dict):
        raise SkillError(f"skill {name} front matter must be an object")
    return decoded, normalized[end + 5 :]


def _optional_mapping(value: Any, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SkillError(f"{field_name} must be an object")
    return value


def _string_list(data: dict[str, Any], key: str, skill_name: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise SkillError(f"skill {skill_name} metadata.fshows.{key} must be a string list")
    normalized = tuple(item.strip() for item in value)
    if len(set(normalized)) != len(normalized):
        raise SkillError(f"skill {skill_name} metadata.fshows.{key} must be unique")
    return normalized


def _discover_references(body: str) -> tuple[str, ...]:
    resource_directories = r"(?:references|scripts|assets|templates?|template)"
    candidates = set(
        re.findall(
            rf"(?<![A-Za-z0-9_./-]){resource_directories}/[A-Za-z0-9_./-]+",
            body,
        )
    )
    candidates.update(
        re.findall(
            rf"<skill_dir>/({resource_directories}/[A-Za-z0-9_./-]+)", body
        )
    )
    for target in re.findall(r"\[[^\]]*\]\(([^)\s]+)", body):
        path = target.split("#", 1)[0]
        if path and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", path) and not path.startswith("#"):
            candidates.add(path)
    return tuple(sorted(candidate.rstrip(".,:;)") for candidate in candidates))


def _contained_directory(root: Path, relative: str) -> Path:
    candidate = (root / Path(PurePosixPath(relative))).resolve()
    if not candidate.is_relative_to(root.resolve()) or not candidate.is_dir():
        raise SkillError(f"Skill directory does not exist at pinned revision: {relative}")
    return candidate


def _contained_file(root: Path, relative: str, skill_name: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts:
        raise SkillError(f"skill {skill_name} has unsafe reference: {relative}")
    candidate = (root / Path(path)).resolve()
    if not candidate.is_relative_to(root.resolve()) or not candidate.is_file():
        raise SkillError(f"skill {skill_name} references missing file: {relative}")
    return candidate


def _validate_artifact_path(skill_name: str, value: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise SkillError(f"skill {skill_name} has unsafe artifact path: {value}")
    allowed = (
        "prd/",
        "analysis/",
        "ddl/design.md",
        "task-split/",
        "tech-analysis/",
        "reviews/",
        "test/",
        "orchestration/handoffs/",
    )
    if not any(value == prefix.rstrip("/") or value.startswith(prefix) for prefix in allowed):
        raise SkillError(f"skill {skill_name} uses unsupported artifact path: {value}")
    _reject_deprecated_artifact_aliases(skill_name, value)


def _reject_deprecated_artifact_aliases(skill_name: str, text: str) -> None:
    deprecated = {
        "ddl/ddl.md": r"(?<![A-Za-z0-9_-])ddl/ddl\.md",
        "ddl/*-ddl.md": r"(?<![A-Za-z0-9_-])ddl/[^\s`]+-ddl\.md",
        "task/*.md": r"(?<![A-Za-z0-9_-])task/[^\s`]+\.md",
        "review/**": r"(?<![A-Za-z0-9_-])review/[^\s`]+",
    }
    for alias, pattern in deprecated.items():
        if re.search(pattern, text):
            raise SkillError(f"skill {skill_name} references deprecated artifact alias: {alias}")


def _directory_hash(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*") if path.is_file())
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()
