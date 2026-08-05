from __future__ import annotations

import os
import socket
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import yaml
from liquid import Environment, StrictUndefined
from liquid.exceptions import LiquidError


class WorkflowError(ValueError):
    pass


@dataclass(frozen=True)
class TrackerConfig:
    endpoint: str
    token: str
    worker_id: str
    lease_seconds: int = 300
    request_timeout_seconds: float = 30.0
    secret_environment_names: tuple[str, ...] = ("ACP_API_TOKEN", "CONTROL_PLANE_TOKEN")


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
class CodexConfig:
    command: str = "codex app-server"
    approval_policy: str | dict[str, Any] = "never"
    thread_sandbox: str = "danger-full-access"
    turn_sandbox_policy: dict[str, Any] = field(default_factory=lambda: {"type": "dangerFullAccess"})
    turn_timeout_ms: int = 3_600_000
    read_timeout_ms: int = 5_000
    stall_timeout_ms: int = 300_000
    model: str | None = None
    effort: str | None = None
    allowed_skills: tuple[str, ...] = ()
    isolate_user_home: bool = True


@dataclass(frozen=True)
class AgentConfig:
    max_concurrent_agents: int = 4
    max_retry_backoff_ms: int = 300_000
    max_turns: int = 20
    skills: tuple[str, ...] = ()
    sandbox: str = "danger-full-access"
    network_access: bool = True
    model: str | None = None
    effort: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "kind": "coding_agent",
            "skills": list(self.skills),
            "sandbox": self.sandbox,
            "network_access": self.network_access,
            "model": self.model,
            "effort": self.effort,
            "max_turns": self.max_turns,
        }

    def codex_config(self, base: CodexConfig) -> CodexConfig:
        if self.sandbox == "read-only":
            policy: dict[str, Any] = {"type": "readOnly"}
        elif self.sandbox == "danger-full-access":
            policy = {"type": "dangerFullAccess"}
        else:
            policy = {**base.turn_sandbox_policy, "type": "workspaceWrite", "networkAccess": self.network_access}
        return replace(
            base, thread_sandbox=self.sandbox, turn_sandbox_policy=policy,
            model=self.model or base.model, effort=self.effort or base.effort,
            allowed_skills=self.skills,
        )


@dataclass(frozen=True)
class Workflow:
    path: Path
    tracker: TrackerConfig
    polling_interval_ms: int
    workspace: WorkspaceConfig
    skill_repository: SkillRepositoryConfig
    agent: AgentConfig
    codex: CodexConfig
    prompt_template: str
    required_environment: tuple[str, ...]

    def render_prompt(self, issue: dict[str, Any], attempt: int | None) -> str:
        try:
            return Environment(undefined=StrictUndefined).from_string(self.prompt_template).render(issue=issue, attempt=attempt).strip()
        except LiquidError as error:
            raise WorkflowError(f"prompt rendering failed: {error}") from error


def load_workflow(path: str | Path) -> Workflow:
    workflow_path = Path(path).expanduser().resolve()
    try:
        source = workflow_path.read_text(encoding="utf-8")
    except OSError as error:
        raise WorkflowError(f"cannot read workflow file: {workflow_path}") from error
    front, prompt = _parse_front_matter(source)
    if not prompt.strip():
        raise WorkflowError("WORKFLOW.md prompt body is required")

    tracker_data = _mapping(front.get("tracker"), "tracker")
    if tracker_data.get("kind") not in {"fshows_control_plane", "windows_control_plane"}:
        raise WorkflowError("tracker.kind must be fshows_control_plane")
    provider = _mapping(tracker_data.get("provider"), "tracker.provider")
    endpoint = _endpoint(str(provider.get("endpoint", "http://127.0.0.1:8080")), provider.get("allow_insecure_http") is True)
    token, token_env = _resolve_secret(provider.get("token"), "CONTROL_PLANE_TOKEN")
    if not token:
        raise WorkflowError("tracker provider token is required")
    worker_id = _resolve(provider.get("worker_id")) or os.getenv("SYMPHONY_WORKER_ID") or f"windows-{socket.gethostname()}-{os.getpid()}"
    tracker = TrackerConfig(
        endpoint=endpoint, token=token, worker_id=worker_id,
        lease_seconds=_integer(provider.get("lease_seconds", 300), 10, 3600, "tracker.provider.lease_seconds"),
        request_timeout_seconds=_number(provider.get("request_timeout_seconds", 30), "tracker.provider.request_timeout_seconds"),
        secret_environment_names=tuple(sorted({"ACP_API_TOKEN", "CONTROL_PLANE_TOKEN", *(name for name in [token_env] if name)})),
    )

    workspace_data = _mapping(front.get("workspace"), "workspace")
    raw_root = _resolve(workspace_data.get("root"))
    root = _path(raw_root, workflow_path.parent, Path(tempfile.gettempdir()) / "symphony_workspaces")
    hooks_data = _mapping(front.get("hooks"), "hooks")
    hooks = HookConfig(
        after_create=_optional(hooks_data.get("after_create")), before_run=_optional(hooks_data.get("before_run")),
        after_run=_optional(hooks_data.get("after_run")), before_remove=_optional(hooks_data.get("before_remove")),
        timeout_ms=_integer(hooks_data.get("timeout_ms", 60000), 1, 3600000, "hooks.timeout_ms"),
    )

    skill_data = _mapping(front.get("skill_repository"), "skill_repository")
    skill_repository = SkillRepositoryConfig(
        url=_required(skill_data.get("url"), "skill_repository.url"),
        revision=_required(skill_data.get("revision"), "skill_repository.revision"),
        skills_path=_relative(_required(skill_data.get("skills_path"), "skill_repository.skills_path")),
        cache_root=_path(_resolve(skill_data.get("cache_root")), workflow_path.parent, Path(tempfile.gettempdir()) / "fshows-symphony-skills"),
    )

    if "agent" not in front:
        raise WorkflowError("agent section is required")
    agent_data = _mapping(front.get("agent"), "agent")
    skills = _strings(agent_data.get("skills", []), "agent.skills")
    sandbox = str(agent_data.get("sandbox", "danger-full-access"))
    if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
        raise WorkflowError("agent.sandbox is invalid")
    agent = AgentConfig(
        max_concurrent_agents=_integer(agent_data.get("max_concurrent_agents", 4), 1, 100, "agent.max_concurrent_agents"),
        max_retry_backoff_ms=_integer(agent_data.get("max_retry_backoff_ms", 300000), 1000, 86400000, "agent.max_retry_backoff_ms"),
        max_turns=_integer(agent_data.get("max_turns", 20), 1, 100, "agent.max_turns"),
        skills=skills, sandbox=sandbox,
        network_access=_boolean(agent_data.get("network_access", True), "agent.network_access"),
        model=_optional(agent_data.get("model")), effort=_optional(agent_data.get("effort")),
    )

    codex_data = _mapping(front.get("codex"), "codex")
    command = _required(codex_data.get("command", "codex app-server"), "codex.command")
    turn_policy = codex_data.get("turn_sandbox_policy", {"type": "dangerFullAccess"})
    if not isinstance(turn_policy, dict):
        raise WorkflowError("codex.turn_sandbox_policy must be an object")
    codex = CodexConfig(
        command=command, approval_policy=codex_data.get("approval_policy", "never"),
        turn_sandbox_policy=turn_policy,
        turn_timeout_ms=_integer(codex_data.get("turn_timeout_ms", 3600000), 1000, 86400000, "codex.turn_timeout_ms"),
        read_timeout_ms=_integer(codex_data.get("read_timeout_ms", 5000), 100, 300000, "codex.read_timeout_ms"),
        stall_timeout_ms=_integer(codex_data.get("stall_timeout_ms", 300000), 0, 86400000, "codex.stall_timeout_ms"),
        isolate_user_home=_boolean(codex_data.get("isolate_user_home", True), "codex.isolate_user_home"),
    )
    polling = _mapping(front.get("polling"), "polling")
    return Workflow(
        path=workflow_path, tracker=tracker,
        polling_interval_ms=_integer(polling.get("interval_ms", 30000), 100, 3600000, "polling.interval_ms"),
        workspace=WorkspaceConfig(root=root, hooks=hooks), skill_repository=skill_repository,
        agent=agent, codex=codex, prompt_template=prompt,
        required_environment=tuple(sorted(name for name in [token_env] if name)),
    )


def _parse_front_matter(source: str) -> tuple[dict[str, Any], str]:
    normalized = source.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        raise WorkflowError("WORKFLOW.md requires YAML front matter")
    end = normalized.find("\n---\n", 4)
    if end < 0:
        raise WorkflowError("WORKFLOW.md front matter is not terminated")
    try:
        decoded = yaml.safe_load(normalized[4:end]) or {}
    except yaml.YAMLError as error:
        raise WorkflowError(f"invalid workflow YAML: {error}") from error
    if not isinstance(decoded, dict):
        raise WorkflowError("workflow front matter must be an object")
    return decoded, normalized[end + 5 :]


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise WorkflowError(f"{name} must be an object")
    return value


def _resolve(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.startswith("$"):
        return os.getenv(text[1:])
    return text or None


def _resolve_secret(value: Any, fallback: str) -> tuple[str | None, str | None]:
    if value is None:
        return os.getenv(fallback), fallback
    text = str(value).strip()
    if text.startswith("$"):
        return os.getenv(text[1:]), text[1:]
    return text or None, None


def _required(value: Any, name: str) -> str:
    result = _resolve(value)
    if not result:
        raise WorkflowError(f"{name} is required")
    return result


def _optional(value: Any) -> str | None:
    return _resolve(value)


def _integer(value: Any, minimum: int, maximum: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise WorkflowError(f"{name} must be between {minimum} and {maximum}")
    return value


def _number(value: Any, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise WorkflowError(f"{name} must be positive")
    return float(value)


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise WorkflowError(f"{name} must be boolean")
    return value


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise WorkflowError(f"{name} must be a string list")
    result = tuple(item.strip() for item in value)
    if len(set(result)) != len(result):
        raise WorkflowError(f"{name} must be unique")
    return result


def _path(value: str | None, base: Path, default: Path) -> Path:
    candidate = Path(value) if value else default
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.expanduser().resolve()


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise WorkflowError("skill_repository.skills_path must be relative")
    return path.as_posix()


def _endpoint(value: str, allow_insecure: bool) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise WorkflowError("tracker endpoint must be HTTP(S)")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"} and not allow_insecure:
        raise WorkflowError("insecure tracker HTTP requires allow_insecure_http")
    return value.rstrip("/")
