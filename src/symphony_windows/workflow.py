from __future__ import annotations

import os
import re
import socket
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
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
class AgentConfig:
    max_concurrent_agents: int = 4
    max_retry_backoff_ms: int = 300_000


@dataclass(frozen=True)
class CodexConfig:
    command: str = "codex app-server"
    approval_policy: str | dict[str, Any] = field(
        default_factory=lambda: {
            "reject": {
                "sandbox_approval": True,
                "rules": True,
                "mcp_elicitations": True,
            }
        }
    )
    thread_sandbox: str = "workspace-write"
    turn_sandbox_policy: dict[str, Any] = field(
        default_factory=lambda: {"type": "workspaceWrite", "networkAccess": False}
    )
    turn_timeout_ms: int = 3_600_000
    read_timeout_ms: int = 5_000
    stall_timeout_ms: int = 300_000


@dataclass(frozen=True)
class Workflow:
    path: Path
    tracker: TrackerConfig
    workspace: WorkspaceConfig
    agent: AgentConfig
    codex: CodexConfig
    polling_interval_ms: int
    prompt_template: str

    def render_prompt(self, issue: dict[str, Any], attempt: int | None) -> str:
        template = self.prompt_template.strip() or (
            "You are working on issue {{ issue.identifier }}.\n\n"
            "Title: {{ issue.title }}\n\n{{ issue.description }}"
        )
        try:
            return Environment(undefined=StrictUndefined).from_string(template).render(
                issue=issue,
                attempt=attempt,
            )
        except LiquidError as error:
            raise WorkflowError(f"prompt rendering failed: {error}") from error


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
    lease_seconds = _bounded_int(provider.get("lease_seconds", 300), 10, 3600, "lease_seconds")
    request_timeout = _positive_number(
        provider.get("request_timeout_seconds", 30),
        "request_timeout_seconds",
    )

    workspace_data = _mapping(front_matter.get("workspace"), "workspace")
    raw_root, _root_env = _resolve_value(workspace_data.get("root"), None)
    default_root = Path(tempfile.gettempdir()) / "symphony_workspaces"
    workspace_root = _resolve_path(raw_root, workflow_path.parent, default_root)

    hooks_data = _mapping(front_matter.get("hooks"), "hooks")
    hooks = HookConfig(
        after_create=_optional_string(hooks_data.get("after_create"), "hooks.after_create"),
        before_run=_optional_string(hooks_data.get("before_run"), "hooks.before_run"),
        after_run=_optional_string(hooks_data.get("after_run"), "hooks.after_run"),
        before_remove=_optional_string(hooks_data.get("before_remove"), "hooks.before_remove"),
        timeout_ms=_bounded_int(hooks_data.get("timeout_ms", 60_000), 1, 3_600_000, "hooks.timeout_ms"),
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
    approval_policy = codex_data.get(
        "approval_policy",
        {
            "reject": {
                "sandbox_approval": True,
                "rules": True,
                "mcp_elicitations": True,
            }
        },
    )
    if not isinstance(approval_policy, (str, dict)):
        raise WorkflowError("codex.approval_policy must be a string or object")
    thread_sandbox = codex_data.get("thread_sandbox", "workspace-write")
    if thread_sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
        raise WorkflowError("invalid codex.thread_sandbox")
    turn_policy = codex_data.get(
        "turn_sandbox_policy",
        {"type": "workspaceWrite", "networkAccess": False},
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
        agent=agent,
        codex=codex,
        polling_interval_ms=polling_interval,
        prompt_template=prompt,
    )


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
    if parsed.scheme == "http" and parsed.hostname and (loopback or allow_insecure_http):
        return endpoint
    raise WorkflowError("tracker endpoint must use HTTPS or explicit loopback HTTP")


def _bounded_int(value: Any, minimum: int, maximum: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise WorkflowError(f"{field_name} must be between {minimum} and {maximum}")
    return value


def _positive_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise WorkflowError(f"{field_name} must be positive")
    return float(value)


def _optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise WorkflowError(f"{field_name} must be a string")
    return value if value.strip() else None
