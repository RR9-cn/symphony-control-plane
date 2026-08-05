#!/usr/bin/env python3
"""Validate the repository-owned one-agent WORKFLOW.md without dispatching."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from symphony_windows.workflow import Workflow, WorkflowError, load_workflow  # noqa: E402


class WorkflowValidationError(RuntimeError):
    pass


def validate_contract(workflow: Workflow) -> None:
    if workflow.agent.max_turns <= 1:
        raise WorkflowValidationError("agent.max_turns must allow multi-Turn execution")
    sample = {
        "id": "ISSUE-001",
        "identifier": "ISSUE-001",
        "title": "Validate the generic agent",
        "description": "Analyze, implement, and test one complete Issue.",
        "acceptance_criteria": ["Prompt renders"],
        "project_id": "validation-project",
        "source_commit": "0" * 40,
        "workflow_revision": "0" * 64,
    }
    prompt = workflow.render_prompt(sample, 1)
    required = ["ISSUE-001", "analy", "implement", "test", "issue_complete"]
    missing = [text for text in required if text.lower() not in prompt.lower()]
    if missing:
        raise WorkflowValidationError(f"generic agent prompt is missing required context: {missing}")


def validate(path: Path) -> Workflow:
    workflow = load_workflow(
        path,
        token_override="validation-token",
        worker_id_override="workflow-validator",
        project_id_override="validation-project",
    )
    validate_contract(workflow)
    return workflow


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Validate WORKFLOW.md without claiming an Issue")
    result.add_argument("workflow", nargs="?", type=Path, default=ROOT / "WORKFLOW.md")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        workflow = validate(args.workflow.resolve())
    except (OSError, WorkflowError, WorkflowValidationError) as error:
        print(f"Workflow validation failed: {error}", file=sys.stderr)
        return 1
    print("Symphony one-agent workflow validation passed")
    print(f"  file: {workflow.path}")
    print(f"  max turns: {workflow.agent.max_turns}")
    print(f"  max concurrent Issues: {workflow.agent.max_concurrent_agents}")
    print("  skills: repository-local .codex/skills (runtime discovery)")
    print("  dispatch performed: no")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
