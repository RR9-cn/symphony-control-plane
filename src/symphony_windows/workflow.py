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
    project_id: str
    kind: str = "fshows_control_plane"
    required_labels: tuple[str, ...] = ()
    active_states: tuple[str, ...] = ("ready", "running")
    terminal_states: tuple[str, ...] = ("done", "cancelled")
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
class SkillSourceConfig:
    repository: str
    revision: str
    source_path: str = "coding"
    target_path: str = ".codex/skills"


@dataclass(frozen=True)
class WorkspaceConfig:
    root: Path
    hooks: HookConfig = field(default_factory=HookConfig)


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
    isolate_user_home: bool = True


@dataclass(frozen=True)
class AgentConfig:
    max_concurrent_agents: int = 10
    max_concurrent_agents_by_state: dict[str, int] = field(default_factory=dict)
    max_retry_backoff_ms: int = 300_000
    max_turns: int = 20
    sandbox: str = "danger-full-access"
    network_access: bool = True
    model: str | None = None
    effort: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "kind": "coding_agent",
            "sandbox": self.sandbox,
            "network_access": self.network_access,
            "model": self.model,
            "effort": self.effort,
            "max_turns": self.max_turns,
            "max_concurrent_agents_by_state": dict(
                self.max_concurrent_agents_by_state
            ),
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
        )


@dataclass(frozen=True)
class Workflow:
    path: Path
    tracker: TrackerConfig
    polling_interval_ms: int
    workspace: WorkspaceConfig
    skills: SkillSourceConfig | None
    agent: AgentConfig
    codex: CodexConfig
    prompt_template: str
    required_environment: tuple[str, ...]

    def render_prompt(self, issue: dict[str, Any], attempt: int | None) -> str:
        try:
            return Environment(undefined=StrictUndefined).from_string(self.prompt_template).render(issue=issue, attempt=attempt).strip()
        except LiquidError as error:
            raise WorkflowError(f"prompt rendering failed: {error}") from error


def load_workflow(
    path: str | Path,
    *,
    source_override: str | None = None,
    token_override: str | None = None,
    worker_id_override: str | None = None,
    project_id_override: str | None = None,
) -> Workflow:
    workflow_path = Path(path).expanduser().resolve()
    if source_override is None:
        try:
            source = workflow_path.read_text(encoding="utf-8")
        except OSError as error:
            raise WorkflowError(f"cannot read workflow file: {workflow_path}") from error
    else:
        source = source_override
    front, prompt = _parse_front_matter(source)
    if not prompt.strip():
        prompt = "You are working on an issue from the configured tracker."

    tracker_data = _mapping(front.get("tracker"), "tracker")
    if tracker_data.get("kind") not in {"fshows_control_plane", "windows_control_plane"}:
        raise WorkflowError("tracker.kind must be fshows_control_plane")
    provider = _mapping(tracker_data.get("provider"), "tracker.provider")
    endpoint = _endpoint(str(provider.get("endpoint", "http://127.0.0.1:8080")), provider.get("allow_insecure_http") is True)
    token, token_env = _resolve_secret(provider.get("token"), "CONTROL_PLANE_TOKEN")
    token = token_override or token
    if not token:
        raise WorkflowError("tracker provider token is required")
    project_id = project_id_override or os.getenv("SYMPHONY_PROJECT_ID")
    if not project_id:
        raise WorkflowError("SYMPHONY_PROJECT_ID is required")
    worker_id = worker_id_override or os.getenv("SYMPHONY_WORKER_ID") or _resolve(provider.get("worker_id")) or f"windows-{socket.gethostname()}-{os.getpid()}"
    required_labels = _normalized_strings(
        tracker_data.get("required_labels", []),
        "tracker.required_labels",
        allow_blank=True,
    )
    active_states = _normalized_strings(
        tracker_data.get("active_states", ["ready", "running"]),
        "tracker.active_states",
    )
    terminal_states = _normalized_strings(
        tracker_data.get("terminal_states", ["done", "cancelled"]),
        "tracker.terminal_states",
    )
    if not active_states:
        raise WorkflowError("tracker.active_states must not be empty")
    overlap = set(active_states) & set(terminal_states)
    if overlap:
        raise WorkflowError(
            "tracker.active_states and tracker.terminal_states must be disjoint: "
            + ", ".join(sorted(overlap))
        )
    tracker = TrackerConfig(
        kind=str(tracker_data["kind"]), endpoint=endpoint, token=token, worker_id=worker_id,
        project_id=project_id,
        required_labels=required_labels, active_states=active_states,
        terminal_states=terminal_states,
        lease_seconds=_integer(provider.get("lease_seconds", 300), 10, 3600, "tracker.provider.lease_seconds"),
        request_timeout_seconds=_number(provider.get("request_timeout_seconds", 30), "tracker.provider.request_timeout_seconds"),
        secret_environment_names=tuple(sorted({"ACP_API_TOKEN", "CONTROL_PLANE_TOKEN", *(name for name in [token_env] if name)})),
    )

    workspace_data = _mapping(front.get("workspace"), "workspace")
    raw_root = _resolve(workspace_data.get("root"))
    root = _path(raw_root, workflow_path.parent, Path(tempfile.gettempdir()) / "symphony_workspaces")
    hooks_data = _mapping(front.get("hooks"), "hooks")
    hooks = HookConfig(
        after_create=_optional_script(hooks_data.get("after_create"), "hooks.after_create"),
        before_run=_optional_script(hooks_data.get("before_run"), "hooks.before_run"),
        after_run=_optional_script(hooks_data.get("after_run"), "hooks.after_run"),
        before_remove=_optional_script(hooks_data.get("before_remove"), "hooks.before_remove"),
        timeout_ms=_integer(hooks_data.get("timeout_ms", 60000), 1, 3600000, "hooks.timeout_ms"),
    )

    skills_data = _mapping(front.get("skills"), "skills")
    skills = None
    if skills_data:
        skills_revision = _required(skills_data.get("revision"), "skills.revision").lower()
        if len(skills_revision) != 40 or any(character not in "0123456789abcdef" for character in skills_revision):
            raise WorkflowError("skills.revision must be a full 40-character Git commit")
        source_path = _relative_posix_path(
            skills_data.get("source_path", "coding"), "skills.source_path"
        )
        target_path = _relative_posix_path(
            skills_data.get("target_path", ".codex/skills"), "skills.target_path"
        )
        skills = SkillSourceConfig(
            repository=_required(skills_data.get("repository"), "skills.repository"),
            revision=skills_revision,
            source_path=source_path,
            target_path=target_path,
        )

    agent_data = _mapping(front.get("agent"), "agent")
    sandbox = str(agent_data.get("sandbox", "danger-full-access"))
    if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
        raise WorkflowError("agent.sandbox is invalid")
    agent = AgentConfig(
        max_concurrent_agents=_integer(agent_data.get("max_concurrent_agents", 10), 1, 100, "agent.max_concurrent_agents"),
        max_concurrent_agents_by_state=_state_limits(
            agent_data.get("max_concurrent_agents_by_state", {})
        ),
        max_retry_backoff_ms=_integer(agent_data.get("max_retry_backoff_ms", 300000), 1000, 86400000, "agent.max_retry_backoff_ms"),
        max_turns=_integer(agent_data.get("max_turns", 20), 1, 100, "agent.max_turns"),
        sandbox=sandbox,
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
        workspace=WorkspaceConfig(root=root, hooks=hooks),
        skills=skills,
        agent=agent, codex=codex, prompt_template=prompt,
        required_environment=tuple(sorted(name for name in [token_env] if name)),
    )


def _parse_front_matter(source: str) -> tuple[dict[str, Any], str]:
    normalized = source.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized
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


def _optional_script(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise WorkflowError(f"{name} must be a string")
    return value.strip() or None


def _relative_posix_path(value: Any, name: str) -> str:
    text = _required(value, name).replace("\\", "/")
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise WorkflowError(f"{name} must stay inside the checkout")
    return path.as_posix()


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


def _strings(
    value: Any, name: str, *, allow_blank: bool = False
) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise WorkflowError(f"{name} must be a string list")
    result = tuple(item.strip() for item in value)
    if not allow_blank and any(not item for item in result):
        raise WorkflowError(f"{name} must not contain blank values")
    if len(set(result)) != len(result):
        raise WorkflowError(f"{name} must be unique")
    return result


def _normalized_strings(
    value: Any, name: str, *, allow_blank: bool = False
) -> tuple[str, ...]:
    values = _strings(value, name, allow_blank=allow_blank)
    normalized = tuple(item.strip().lower() for item in values)
    if len(set(normalized)) != len(normalized):
        raise WorkflowError(f"{name} must be unique after normalization")
    return normalized


def _state_limits(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise WorkflowError("agent.max_concurrent_agents_by_state must be an object")
    result: dict[str, int] = {}
    for raw_state, raw_limit in value.items():
        state = str(raw_state).strip().lower()
        if (
            not state
            or state in result
            or not isinstance(raw_limit, int)
            or isinstance(raw_limit, bool)
            or not 1 <= raw_limit <= 100
        ):
            continue
        result[state] = raw_limit
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
