from __future__ import annotations

import os
import re
import socket
import tempfile
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

import yaml
from liquid import Environment, StrictUndefined
from liquid.exceptions import LiquidError


class WorkflowError(ValueError):
    """The repository-owned WORKFLOW.md cannot be used safely."""


@dataclass(frozen=True)
class TrackerConfig:
    endpoint: str
    token: str
    worker_id: str
    lease_seconds: int = 300
    request_timeout_seconds: float = 30.0
    secret_environment_names: tuple[str, ...] = (
        "ACP_API_TOKEN",
        "CONTROL_PLANE_TOKEN",
    )


@dataclass(frozen=True)
class HookConfig:
    after_create: str | None = None
    before_run: str | None = None
    after_run: str | None = None
    before_remove: str | None = None
    timeout_ms: int = 60_000


@dataclass(frozen=True)
class WorkspaceConfig:
    root: Path
    hooks: HookConfig = field(default_factory=HookConfig)


@dataclass(frozen=True)
class SkillRepositoryConfig:
    url: str
    revision: str
    skills_path: str
    cache_root: Path


@dataclass(frozen=True)
class AgentConfig:
    max_concurrent_agents: int = 4
    max_retry_backoff_ms: int = 300_000


@dataclass(frozen=True)
class CodexConfig:
    command: str = "codex app-server"
    approval_policy: str | dict[str, Any] = "never"
    thread_sandbox: str = "danger-full-access"
    turn_sandbox_policy: dict[str, Any] = field(
        default_factory=lambda: {"type": "dangerFullAccess"}
    )
    turn_timeout_ms: int = 3_600_000
    read_timeout_ms: int = 5_000
    stall_timeout_ms: int = 300_000
    model: str | None = None
    effort: str | None = None
    allowed_skills: tuple[str, ...] = ()
    isolate_user_home: bool = True


@dataclass(frozen=True)
class AgentProfileConfig:
    name: str
    version: int
    agent_role: str
    prompt_file: str
    prompt_template: str
    skills: tuple[str, ...]
    sandbox: str
    network_access: bool
    max_concurrent_agents: int
    max_turns: int
    model: str | None = None
    effort: str | None = None

    @property
    def prompt_hash(self) -> str:
        return sha256(self.prompt_template.encode("utf-8")).hexdigest()

    def snapshot(self) -> dict[str, Any]:
        return {
            "profile_name": self.name,
            "profile_version": self.version,
            "agent_role": self.agent_role,
            "prompt_file": self.prompt_file,
            "prompt_hash": self.prompt_hash,
            "skills": list(self.skills),
            "model": self.model,
            "effort": self.effort,
            "sandbox": self.sandbox,
            "network_access": self.network_access,
            "max_concurrent_agents": self.max_concurrent_agents,
            "max_turns": self.max_turns,
        }

    def claim_profile(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "config": self.snapshot(),
        }

    def codex_config(self, base: CodexConfig) -> CodexConfig:
        if self.sandbox == "read-only":
            turn_policy: dict[str, Any] = {"type": "readOnly"}
        elif self.sandbox == "danger-full-access":
            turn_policy = {"type": "dangerFullAccess"}
        else:
            turn_policy = {
                **base.turn_sandbox_policy,
                "type": "workspaceWrite",
                "networkAccess": self.network_access,
            }
        return replace(
            base,
            thread_sandbox=self.sandbox,
            turn_sandbox_policy=turn_policy,
            model=self.model or base.model,
            effort=self.effort or base.effort,
            allowed_skills=self.skills,
        )

    def render_prompt(self, issue: dict[str, Any], attempt: int | None) -> str:
        allowed_skills = ", ".join(self.skills) if self.skills else "none"
        policy = (
            f"Agent profile: {self.name} v{self.version}\n"
            f"Allowed skills: {allowed_skills}\n"
            f"Sandbox: {self.sandbox}; network access: "
            f"{'enabled' if self.network_access else 'disabled'}\n"
            "Use no skill outside this profile's allowlist.\n"
            "When a Skill requires human confirmation, call "
            "work_item_request_human and end the Turn; never wait for input "
            "inside the Codex session."
        )
        try:
            rendered = (
                Environment(undefined=StrictUndefined)
                .from_string(self.prompt_template)
                .render(
                    issue=issue,
                    attempt=attempt,
                    profile=self.snapshot(),
                )
            )
        except LiquidError as error:
            raise WorkflowError(
                f"prompt rendering failed for profile {self.name}: {error}"
            ) from error
        return f"{policy}\n\n{rendered.strip()}".strip()


@dataclass(frozen=True)
class Workflow:
    path: Path
    tracker: TrackerConfig
    workspace: WorkspaceConfig
    skill_repository: SkillRepositoryConfig
    agent: AgentConfig
    codex: CodexConfig
    polling_interval_ms: int
    prompt_template: str
    agent_profiles: tuple[AgentProfileConfig, ...]

    def profile_for(self, issue: dict[str, Any]) -> AgentProfileConfig:
        role = issue.get("agent_role")
        matches = [
            profile for profile in self.agent_profiles if profile.agent_role == role
        ]
        if not matches:
            raise WorkflowError(f"no agent profile matches agent_role {role!r}")
        if len(matches) != 1:
            raise WorkflowError(f"multiple agent profiles match agent_role {role!r}")
        return matches[0]


def load_workflow(path: str | Path) -> Workflow:
    workflow_path = Path(path).expanduser().resolve()
    try:
        source = workflow_path.read_text(encoding="utf-8")
    except OSError as error:
        raise WorkflowError(f"cannot read workflow file: {workflow_path}") from error

    front_matter, prompt = _parse_front_matter(source)
    tracker_data = _mapping(front_matter.get("tracker"), "tracker")
    provider = _mapping(tracker_data.get("provider"), "tracker.provider")
    kind = tracker_data.get("kind")
    if kind not in {"fshows_control_plane", "windows_control_plane"}:
        raise WorkflowError("tracker.kind must be fshows_control_plane")

    endpoint = _validated_endpoint(
        str(provider.get("endpoint", "http://127.0.0.1:8080")),
        provider.get("allow_insecure_http") is True,
    )
    token, token_env = _resolve_secret(provider.get("token"), "CONTROL_PLANE_TOKEN")
    if not token:
        raise WorkflowError("tracker.provider.token or CONTROL_PLANE_TOKEN is required")

    worker_value, _worker_env = _resolve_value(
        provider.get("worker_id"),
        os.getenv("SYMPHONY_WORKER_ID"),
    )
    worker_id = worker_value or f"windows-{socket.gethostname()}-{os.getpid()}"
    lease_seconds = _bounded_int(
        provider.get("lease_seconds", 300), 10, 3600, "lease_seconds"
    )
    request_timeout = _positive_number(
        provider.get("request_timeout_seconds", 30),
        "request_timeout_seconds",
    )

    workspace_data = _mapping(front_matter.get("workspace"), "workspace")
    raw_root, _root_env = _resolve_value(workspace_data.get("root"), None)
    default_root = Path(tempfile.gettempdir()) / "symphony_workspaces"
    workspace_root = _resolve_path(raw_root, workflow_path.parent, default_root)

    skill_repository = _load_skill_repository(
        front_matter.get("skill_repository"), workflow_path.parent
    )

    hooks_data = _mapping(front_matter.get("hooks"), "hooks")
    hooks = HookConfig(
        after_create=_optional_string(
            hooks_data.get("after_create"), "hooks.after_create"
        ),
        before_run=_optional_string(hooks_data.get("before_run"), "hooks.before_run"),
        after_run=_optional_string(hooks_data.get("after_run"), "hooks.after_run"),
        before_remove=_optional_string(
            hooks_data.get("before_remove"), "hooks.before_remove"
        ),
        timeout_ms=_bounded_int(
            hooks_data.get("timeout_ms", 60_000), 1, 3_600_000, "hooks.timeout_ms"
        ),
    )

    polling_data = _mapping(front_matter.get("polling"), "polling")
    polling_interval = _bounded_int(
        polling_data.get("interval_ms", 30_000),
        100,
        3_600_000,
        "polling.interval_ms",
    )

    agent_data = _mapping(front_matter.get("agent"), "agent")
    agent = AgentConfig(
        max_concurrent_agents=_bounded_int(
            agent_data.get("max_concurrent_agents", 4),
            1,
            100,
            "agent.max_concurrent_agents",
        ),
        max_retry_backoff_ms=_bounded_int(
            agent_data.get("max_retry_backoff_ms", 300_000),
            1_000,
            86_400_000,
            "agent.max_retry_backoff_ms",
        ),
    )

    codex_data = _mapping(front_matter.get("codex"), "codex")
    command = codex_data.get("command", "codex app-server")
    if not isinstance(command, str) or not command.strip():
        raise WorkflowError("codex.command must be a non-empty string")
    approval_policy = codex_data.get("approval_policy", "never")
    if not isinstance(approval_policy, (str, dict)):
        raise WorkflowError("codex.approval_policy must be a string or object")
    thread_sandbox = codex_data.get("thread_sandbox", "danger-full-access")
    if thread_sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
        raise WorkflowError("invalid codex.thread_sandbox")
    turn_policy = codex_data.get(
        "turn_sandbox_policy",
        {"type": "dangerFullAccess"},
    )
    if not isinstance(turn_policy, dict):
        raise WorkflowError("codex.turn_sandbox_policy must be an object")
    codex = CodexConfig(
        command=command.strip(),
        approval_policy=approval_policy,
        thread_sandbox=thread_sandbox,
        turn_sandbox_policy=turn_policy,
        turn_timeout_ms=_bounded_int(
            codex_data.get("turn_timeout_ms", 3_600_000),
            1_000,
            86_400_000,
            "codex.turn_timeout_ms",
        ),
        read_timeout_ms=_bounded_int(
            codex_data.get("read_timeout_ms", 5_000),
            100,
            300_000,
            "codex.read_timeout_ms",
        ),
        stall_timeout_ms=_bounded_int(
            codex_data.get("stall_timeout_ms", 300_000),
            0,
            86_400_000,
            "codex.stall_timeout_ms",
        ),
        model=_optional_string(codex_data.get("model"), "codex.model"),
        effort=_optional_string(codex_data.get("effort"), "codex.effort"),
        isolate_user_home=_boolean(
            codex_data.get("isolate_user_home", True), "codex.isolate_user_home"
        ),
    )

    profiles = _load_agent_profiles(
        front_matter.get("agent_profiles"),
        workflow_path.parent,
        prompt,
        agent.max_concurrent_agents,
    )

    secret_names = {
        "ACP_API_TOKEN",
        "CONTROL_PLANE_TOKEN",
        *(name for name in [token_env] if name),
    }
    tracker = TrackerConfig(
        endpoint=endpoint,
        token=token,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        request_timeout_seconds=request_timeout,
        secret_environment_names=tuple(sorted(secret_names)),
    )
    return Workflow(
        path=workflow_path,
        tracker=tracker,
        workspace=WorkspaceConfig(root=workspace_root, hooks=hooks),
        skill_repository=skill_repository,
        agent=agent,
        codex=codex,
        polling_interval_ms=polling_interval,
        prompt_template=prompt,
        agent_profiles=profiles,
    )


def _load_skill_repository(value: Any, base: Path) -> SkillRepositoryConfig:
    data = _mapping(value, "skill_repository")
    if not data:
        raise WorkflowError("skill_repository is required")
    raw_url, _url_env = _resolve_value(data.get("url"), None)
    if raw_url is None:
        raise WorkflowError("skill_repository.url is required")
    url = _validated_git_source(raw_url, base)
    revision, _revision_env = _resolve_value(data.get("revision"), None)
    if revision is None or not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        raise WorkflowError(
            "skill_repository.revision must be a full 40-character commit SHA"
        )
    skills_path = _safe_posix_directory(
        data.get("skills_path", "skills"), "skill_repository.skills_path"
    )
    raw_cache, _cache_env = _resolve_value(data.get("cache_root"), None)
    cache_root = _resolve_path(
        raw_cache,
        base,
        Path(tempfile.gettempdir()) / "fshows_symphony_skill_cache",
    )
    return SkillRepositoryConfig(
        url=url,
        revision=revision.lower(),
        skills_path=skills_path,
        cache_root=cache_root,
    )


def _load_agent_profiles(
    value: Any,
    base: Path,
    global_prompt: str,
    global_concurrency: int,
) -> tuple[AgentProfileConfig, ...]:
    profiles_data = _mapping(value, "agent_profiles")
    if not profiles_data:
        raise WorkflowError("agent_profiles must define at least one profile")
    profiles: list[AgentProfileConfig] = []
    roles: set[str] = set()
    for name, raw_profile in profiles_data.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise WorkflowError("agent profile names must be snake_case")
        profile_data = _mapping(raw_profile, f"agent_profiles.{name}")
        match = _mapping(profile_data.get("match"), f"agent_profiles.{name}.match")
        unknown_match = set(match) - {"agent_role"}
        if unknown_match:
            raise WorkflowError(
                f"agent_profiles.{name}.match has unsupported fields: "
                f"{', '.join(sorted(unknown_match))}"
            )
        agent_role = _required_string(
            match.get("agent_role"), f"agent_profiles.{name}.match.agent_role"
        )
        if agent_role in roles:
            raise WorkflowError(
                f"multiple agent profiles match agent_role {agent_role!r}"
            )
        roles.add(agent_role)

        prompt_path = _safe_relative_file(
            profile_data.get("prompt_file"),
            base,
            f"agent_profiles.{name}.prompt_file",
        )
        try:
            profile_prompt = prompt_path.read_text(encoding="utf-8")
        except OSError as error:
            raise WorkflowError(
                f"cannot read profile prompt file: {prompt_path}"
            ) from error
        combined_prompt = "\n\n".join(
            part.strip() for part in (profile_prompt, global_prompt) if part.strip()
        )
        if not combined_prompt:
            raise WorkflowError(f"agent_profiles.{name} prompt must not be empty")

        raw_skills = profile_data.get("skills", [])
        if not isinstance(raw_skills, list) or not all(
            isinstance(skill, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", skill)
            for skill in raw_skills
        ):
            raise WorkflowError(
                f"agent_profiles.{name}.skills must be a list of skill names"
            )
        if len(set(raw_skills)) != len(raw_skills):
            raise WorkflowError(f"agent_profiles.{name}.skills must be unique")

        sandbox = profile_data.get("sandbox", "danger-full-access")
        if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            raise WorkflowError(f"invalid agent_profiles.{name}.sandbox")
        network_access = profile_data.get("network_access", False)
        if not isinstance(network_access, bool):
            raise WorkflowError(f"agent_profiles.{name}.network_access must be boolean")
        if sandbox == "read-only" and network_access:
            raise WorkflowError(
                f"agent_profiles.{name} cannot enable network_access with read-only sandbox"
            )
        profiles.append(
            AgentProfileConfig(
                name=name,
                version=_bounded_int(
                    profile_data.get("version", 1),
                    1,
                    2**31 - 1,
                    f"agent_profiles.{name}.version",
                ),
                agent_role=agent_role,
                prompt_file=prompt_path.relative_to(base.resolve()).as_posix(),
                prompt_template=combined_prompt,
                skills=tuple(raw_skills),
                sandbox=sandbox,
                network_access=network_access,
                max_concurrent_agents=_bounded_int(
                    profile_data.get("max_concurrent_agents", global_concurrency),
                    1,
                    100,
                    f"agent_profiles.{name}.max_concurrent_agents",
                ),
                max_turns=_bounded_int(
                    profile_data.get("max_turns", 10),
                    1,
                    1000,
                    f"agent_profiles.{name}.max_turns",
                ),
                model=_optional_string(
                    profile_data.get("model"), f"agent_profiles.{name}.model"
                ),
                effort=_optional_string(
                    profile_data.get("effort"), f"agent_profiles.{name}.effort"
                ),
            )
        )
    return tuple(profiles)


def _parse_front_matter(source: str) -> tuple[dict[str, Any], str]:
    normalized = source.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized.strip()
    end = normalized.find("\n---\n", 4)
    delimiter_length = 5
    if end < 0 and normalized.endswith("\n---"):
        end = len(normalized) - 4
        delimiter_length = 4
    if end < 0:
        raise WorkflowError("WORKFLOW.md front matter is not terminated")
    try:
        decoded = yaml.safe_load(normalized[4:end]) or {}
    except yaml.YAMLError as error:
        raise WorkflowError(f"invalid WORKFLOW.md YAML: {error}") from error
    if not isinstance(decoded, dict):
        raise WorkflowError("WORKFLOW.md front matter must be an object")
    return decoded, normalized[end + delimiter_length :].strip()


def _mapping(value: Any, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise WorkflowError(f"{field_name} must be an object")
    return value


def _resolve_secret(value: Any, default_env: str) -> tuple[str | None, str | None]:
    if value is None:
        return _clean_string(os.getenv(default_env)), default_env
    return _resolve_value(value, None)


def _resolve_value(value: Any, fallback: str | None) -> tuple[str | None, str | None]:
    if value is None:
        return _clean_string(fallback), None
    if not isinstance(value, str):
        raise WorkflowError("environment-backed values must be strings")
    stripped = value.strip()
    if stripped.startswith("$"):
        env_name = stripped[1:]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
            raise WorkflowError(f"invalid environment reference: {value}")
        return _clean_string(os.getenv(env_name) or fallback), env_name
    return _clean_string(stripped), None


def _clean_string(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _resolve_path(value: str | None, base: Path, default: Path) -> Path:
    candidate = Path(value).expanduser() if value else default
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _validated_endpoint(value: str, allow_insecure_http: bool) -> str:
    endpoint = value.strip().rstrip("/")
    parsed = urlparse(endpoint)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme == "https" and parsed.hostname:
        return endpoint
    if (
        parsed.scheme == "http"
        and parsed.hostname
        and (loopback or allow_insecure_http)
    ):
        return endpoint
    raise WorkflowError("tracker endpoint must use HTTPS or explicit loopback HTTP")


def _bounded_int(value: Any, minimum: int, maximum: int, field_name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise WorkflowError(f"{field_name} must be between {minimum} and {maximum}")
    return value


def _positive_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise WorkflowError(f"{field_name} must be positive")
    return float(value)


def _boolean(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise WorkflowError(f"{field_name} must be boolean")
    return value


def _optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise WorkflowError(f"{field_name} must be a string")
    return value if value.strip() else None


def _required_string(value: Any, field_name: str) -> str:
    resolved = _optional_string(value, field_name)
    if resolved is None:
        raise WorkflowError(f"{field_name} is required")
    return resolved.strip()


def _safe_relative_file(value: Any, base: Path, field_name: str) -> Path:
    raw = _required_string(value, field_name)
    relative = Path(raw)
    if relative.is_absolute():
        raise WorkflowError(f"{field_name} must be relative to WORKFLOW.md")
    resolved = (base / relative).resolve()
    try:
        resolved.relative_to(base.resolve())
    except ValueError as error:
        raise WorkflowError(f"{field_name} escapes the workflow directory") from error
    if not resolved.is_file():
        raise WorkflowError(f"{field_name} does not exist: {resolved}")
    return resolved


def _validated_git_source(value: str, base: Path) -> str:
    source = value.strip()
    if not source:
        raise WorkflowError("skill_repository.url must not be empty")
    if re.match(r"^[A-Za-z]:[\\/]", source):
        resolved = Path(source).resolve()
        if not resolved.is_dir():
            raise WorkflowError(
                f"local skill_repository.url does not exist: {resolved}"
            )
        return str(resolved)
    parsed = urlparse(source)
    if parsed.scheme:
        if parsed.scheme not in {"https", "ssh", "git", "file"}:
            raise WorkflowError("skill_repository.url has an unsupported scheme")
        if parsed.password or parsed.query or parsed.fragment:
            raise WorkflowError(
                "skill_repository.url must not embed credentials or query data"
            )
        return source
    if re.fullmatch(r"[^@\s]+@[^:\s]+:.+", source):
        return source
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = base / path
    resolved = path.resolve()
    if not resolved.is_dir():
        raise WorkflowError(f"local skill_repository.url does not exist: {resolved}")
    return str(resolved)


def _safe_posix_directory(value: Any, field_name: str) -> str:
    raw = _required_string(value, field_name)
    if "\\" in raw:
        raise WorkflowError(f"{field_name} must use forward slashes")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise WorkflowError(
            f"{field_name} must be a safe repository-relative directory"
        )
    return path.as_posix()
