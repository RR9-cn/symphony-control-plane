from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def read_message() -> dict:
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    return json.loads(line)


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def call_tool(request_id: int, name: str, arguments: dict) -> dict:
    send(
        {
            "method": "item/tool/call",
            "id": request_id,
            "params": {"tool": name, "arguments": arguments},
        }
    )
    response = read_message()
    assert response["id"] == request_id
    assert response["result"]["success"], response
    return response["result"]


def main() -> int:
    if "CONTROL_PLANE_TOKEN" in os.environ or "ACP_API_TOKEN" in os.environ:
        return 88
    assert Path(os.environ["HOME"]).resolve().is_relative_to(Path.cwd().resolve())
    assert Path(os.environ["USERPROFILE"]).resolve().is_relative_to(
        Path.cwd().resolve()
    )
    installed_skills = Path.cwd() / ".agents" / "skills"
    assert sorted(path.name for path in installed_skills.iterdir()) == [
        "fskill-analysis-tech"
    ]
    assert (Path.cwd() / ".agents" / "skills.lock.json").is_file()

    initialize = read_message()
    assert initialize["method"] == "initialize"
    send({"id": initialize["id"], "result": {"userAgent": "fake-codex"}})

    initialized = read_message()
    assert initialized["method"] == "initialized"

    skills_request = read_message()
    assert skills_request["method"] == "skills/list"
    requested_cwd = skills_request["params"]["cwds"][0]
    assert Path(requested_cwd).resolve() == Path.cwd().resolve()
    send(
        {
            "id": skills_request["id"],
            "result": {
                "data": [
                    {
                        "cwd": requested_cwd,
                        "skills": [
                            {
                                "name": "fskill-analysis-tech",
                                "description": "fixture",
                                "enabled": True,
                                "path": str(
                                    installed_skills
                                    / "fskill-analysis-tech"
                                    / "SKILL.md"
                                ),
                            }
                        ],
                        "errors": [],
                    }
                ]
            },
        }
    )

    thread = read_message()
    assert thread["method"] == "thread/start"
    tools = thread["params"]["dynamicTools"]
    assert thread["params"]["sandbox"] == "workspace-write"
    assert [tool["name"] for tool in tools] == [
        "work_item_get",
        "work_item_add_event",
        "work_item_add_artifact",
        "work_item_request_human",
        "work_item_complete",
        "work_item_block",
    ]
    encoded_tools = json.dumps(tools)
    assert "claim_token" not in encoded_tools
    assert "work_item_id" not in encoded_tools
    send({"id": thread["id"], "result": {"thread": {"id": "thread-test"}}})

    turn = read_message()
    assert turn["method"] == "turn/start"
    assert "WI-001" in turn["params"]["input"][0]["text"]
    assert "Agent profile: solution_architect v1" in turn["params"]["input"][0]["text"]
    assert "fskill-analysis-tech" in turn["params"]["input"][0]["text"]
    assert turn["params"]["sandboxPolicy"] == {
        "type": "workspaceWrite",
        "networkAccess": False,
    }
    send({"id": turn["id"], "result": {"turn": {"id": "turn-test"}}})

    if os.getenv("FAKE_CODEX_MODE") == "fail":
        send(
            {
                "method": "turn/failed",
                "params": {"turn": {"id": "turn-test"}, "error": "simulated failure"},
            }
        )
        return 0

    if os.getenv("FAKE_CODEX_MODE") == "human":
        call_tool(
            10,
            "work_item_request_human",
            {
                "question": "Approve the compatibility decision?",
                "options": ["approve", "reject"],
            },
        )
        return 0

    call_tool(10, "work_item_get", {})
    call_tool(
        11,
        "work_item_add_event",
        {"event_type": "turn_started", "payload": {"source": "fake-codex"}},
    )
    call_tool(
        12,
        "work_item_add_artifact",
        {
            "direction": "output",
            "path": "orchestration/handoffs/WI-001.yaml",
            "revision": "windows-native-test",
        },
    )
    call_tool(13, "work_item_complete", {})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
