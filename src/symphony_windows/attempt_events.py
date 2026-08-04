from __future__ import annotations

import json
import re
from typing import Any


_SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(token|secret|password|credential|authorization|cookie|api[_-]?key)(?:$|[_-])",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?(?:key|token)|access[_-]?token|refresh[_-]?token|password|secret|authorization)"
    r"(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+\-/=]+")
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")


def _text(value: object, limit: int) -> str:
    result = str(value).replace("\x00", "")
    result = _BEARER.sub(r"\1[REDACTED]", result)
    result = _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", result)
    result = _OPENAI_KEY.sub("[REDACTED]", result)
    if len(result) > limit:
        result = result[: limit - 1] + "…"
    return result


def _value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 5:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:50]:
            key = _text(raw_key, 100)
            result[key] = (
                "[REDACTED]"
                if _SENSITIVE_KEY.search(key)
                else _value(raw_value, depth=depth + 1)
            )
        return result
    if isinstance(value, list):
        return [_value(item, depth=depth + 1) for item in value[:50]]
    if isinstance(value, str):
        return _text(value, 4000)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _text(value, 1000)


def _json_detail(value: Any) -> str | None:
    if value is None:
        return None
    return _text(
        json.dumps(_value(value), ensure_ascii=False, indent=2, default=str), 16000
    )


def normalize_codex_event(message: dict[str, Any]) -> dict[str, Any] | None:
    """Convert an App Server event into a safe, UI-oriented execution event.

    Reasoning text is intentionally discarded. Only its lifecycle is retained.
    """

    method = message.get("method")
    params = message.get("params")
    if not isinstance(method, str):
        return None
    params = params if isinstance(params, dict) else {}

    if method in {"turn/started", "turn/completed", "turn/failed", "turn/cancelled"}:
        action = method.split("/", 1)[1]
        raw_turn = params.get("turn")
        turn: dict[str, Any] = raw_turn if isinstance(raw_turn, dict) else {}
        payload: dict[str, Any] = {"source_method": method}
        if turn.get("id"):
            payload["turn_id"] = _text(turn["id"], 200)
        error = params.get("error") or turn.get("error")
        return {
            "event_type": f"turn_{action}",
            "item_type": None,
            "status": _optional_text(turn.get("status"), 64),
            "summary": f"Codex Turn {action}",
            "detail": _json_detail(error),
            "payload": payload,
        }

    if method in {"item/started", "item/completed"}:
        item = params.get("item")
        if not isinstance(item, dict):
            return None
        lifecycle = method.split("/", 1)[1]
        item_type = _optional_text(item.get("type"), 100) or "unknown"
        status = _optional_text(item.get("status"), 64)
        payload = {"source_method": method}
        if item.get("id"):
            payload["source_item_id"] = _text(item["id"], 200)

        if item_type == "reasoning":
            return {
                "event_type": f"item_{lifecycle}",
                "item_type": item_type,
                "status": status,
                "summary": f"Reasoning {lifecycle}",
                "detail": None,
                "payload": payload,
            }

        if item_type == "commandExecution":
            command = _optional_text(item.get("command"), 4000)
            if command:
                payload["command"] = command
            if item.get("exitCode") is not None:
                payload["exit_code"] = item.get("exitCode")
            output = item.get("aggregatedOutput") or item.get("output")
            return {
                "event_type": f"command_{lifecycle}",
                "item_type": item_type,
                "status": status,
                "summary": _command_summary(command, lifecycle),
                "detail": _optional_text(output, 16000),
                "payload": payload,
            }

        if item_type == "agentMessage":
            return {
                "event_type": f"agent_message_{lifecycle}",
                "item_type": item_type,
                "status": status,
                "summary": f"Agent message {lifecycle}",
                "detail": _optional_text(item.get("text"), 16000),
                "payload": payload,
            }

        if item_type == "fileChange":
            changes = item.get("changes")
            if isinstance(changes, list):
                payload["changes"] = [
                    {
                        key: _text(change[key], 1000)
                        for key in ("path", "kind")
                        if key in change
                    }
                    for change in changes[:50]
                    if isinstance(change, dict)
                ]
            return {
                "event_type": f"file_change_{lifecycle}",
                "item_type": item_type,
                "status": status,
                "summary": f"File change {lifecycle}",
                "detail": None,
                "payload": payload,
            }

        tool_name = item.get("tool") or item.get("name") or item.get("server")
        if tool_name:
            payload["tool_name"] = _text(tool_name, 200)
        detail_source = item.get("result") or item.get("content")
        return {
            "event_type": f"item_{lifecycle}",
            "item_type": item_type,
            "status": status,
            "summary": f"{item_type} {lifecycle}",
            "detail": _json_detail(detail_source),
            "payload": payload,
        }

    if method == "item/tool/call":
        tool_name = params.get("tool") or params.get("name") or "unknown"
        return {
            "event_type": "tool_call_started",
            "item_type": "dynamicToolCall",
            "status": "running",
            "summary": f"Tool call: {_text(tool_name, 200)}",
            "detail": _json_detail(params.get("arguments")),
            "payload": {
                "source_method": method,
                "tool_name": _text(tool_name, 200),
            },
        }

    if method == "control_plane/tool/completed":
        tool_name = params.get("tool") or "unknown"
        success = bool(params.get("success"))
        return {
            "event_type": "tool_call_completed",
            "item_type": "dynamicToolCall",
            "status": "completed" if success else "failed",
            "summary": f"Tool {'completed' if success else 'failed'}: {_text(tool_name, 200)}",
            "detail": _json_detail(params.get("result")),
            "payload": {
                "source_method": method,
                "tool_name": _text(tool_name, 200),
                "success": success,
            },
        }

    if "approval" in method.lower() or "requestuserinput" in method.lower():
        return {
            "event_type": "input_required",
            "item_type": None,
            "status": "waiting",
            "summary": "Agent requested human input",
            "detail": None,
            "payload": {"source_method": method},
        }
    return None


def _optional_text(value: object, limit: int) -> str | None:
    if value is None:
        return None
    result = _text(value, limit)
    return result or None


def _command_summary(command: str | None, lifecycle: str) -> str:
    if not command:
        return f"Command {lifecycle}"
    one_line = " ".join(command.split())
    return _text(f"Command {lifecycle}: {one_line}", 1000)
