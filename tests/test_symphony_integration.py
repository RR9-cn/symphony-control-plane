from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from symphony_windows.workflow import AgentConfig, CodexConfig, load_workflow


ROOT = Path(__file__).resolve().parents[1]


def test_agent_config_controls_codex_as_one_runtime(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-token")
    workflow = load_workflow(ROOT / "WORKFLOW.md")
    config = replace(
        workflow.agent,
        sandbox="workspace-write",
        network_access=False,
        model="gpt-test",
        effort="high",
    )
    codex = config.codex_config(CodexConfig())
    assert codex.thread_sandbox == "workspace-write"
    assert codex.turn_sandbox_policy == {"type": "workspaceWrite", "networkAccess": False}
    assert not hasattr(codex, "allowed_skills")
    assert codex.model == "gpt-test"
    assert codex.effort == "high"


def test_agent_snapshot_contains_no_role_or_stage():
    snapshot = AgentConfig(max_turns=7).snapshot()
    assert snapshot["kind"] == "coding_agent"
    assert snapshot["max_turns"] == 7
    assert "role" not in snapshot
    assert "stage" not in snapshot
    assert "profile" not in snapshot
