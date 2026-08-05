from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from symphony_windows.workflow import WorkflowError


class SkillError(WorkflowError):
    """A repository-owned Skill package is unsafe or invalid."""


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


def validate_skill_package(name: str, source: Path) -> SkillInfo:
    """Validate one checked-in Skill without copying or injecting it."""
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
    if front_matter.get("name") != name:
        raise SkillError(f"skill {name} front matter name must match its directory")
    description = front_matter.get("description")
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
        raise SkillError(f"skill {name} declares external_writes without human_confirmation")
    for tool in required_tools:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", tool):
            raise SkillError(f"skill {name} has invalid required tool name: {tool}")
    for variable in required_credentials:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", variable):
            raise SkillError(f"skill {name} required credential must be an environment name: {variable}")
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
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise SkillError(f"skill {skill_name} metadata.fshows.{key} must be a string list")
    normalized = tuple(item.strip() for item in value)
    if len(set(normalized)) != len(normalized):
        raise SkillError(f"skill {skill_name} metadata.fshows.{key} must be unique")
    return normalized


def _discover_references(body: str) -> tuple[str, ...]:
    resource_directories = r"(?:references|scripts|assets|templates?|template)"
    candidates = set(re.findall(rf"(?<![A-Za-z0-9_./-]){resource_directories}/[A-Za-z0-9_./-]+", body))
    candidates.update(re.findall(rf"<skill_dir>/({resource_directories}/[A-Za-z0-9_./-]+)", body))
    for target in re.findall(r"\[[^\]]*\]\(([^)\s]+)", body):
        path = target.split("#", 1)[0]
        if path and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", path) and not path.startswith("#"):
            candidates.add(path)
    return tuple(sorted(candidate.rstrip(".,:;)") for candidate in candidates))


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
    allowed = ("prd/", "analysis/", "ddl/design.md", "task-split/", "tech-analysis/", "reviews/", "test/")
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
    for path in sorted(path for path in root.rglob("*") if path.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()
