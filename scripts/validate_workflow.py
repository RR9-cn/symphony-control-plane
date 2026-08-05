#!/usr/bin/env python3
"""Validate the repository-owned one-agent WORKFLOW.md without dispatching."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from symphony_windows.skill import SkillError, SkillManager  # noqa: E402
from symphony_windows.workflow import Workflow, WorkflowError, load_workflow  # noqa: E402


class WorkflowValidationError(RuntimeError):
    pass


def validate_contract(workflow: Workflow) -> None:
    if workflow.agent.max_turns <= 1:
        raise WorkflowValidationError("agent.max_turns must allow multi-Turn execution")
    if not workflow.agent.skills:
        raise WorkflowValidationError("the coding agent must declare a Skill allowlist")
    sample = {
        "id": "ISSUE-001",
        "identifier": "ISSUE-001",
        "title": "Validate the generic agent",
        "description": "Analyze, implement, and test one complete Issue.",
        "acceptance_criteria": ["Prompt renders"],
        "repository": {
            "url": str(ROOT),
            "base_branch": "main",
            "commit": "0" * 40,
        },
    }
    prompt = workflow.render_prompt(sample, 1)
    required = ["ISSUE-001", "analy", "implement", "test", "issue_complete"]
    missing = [text for text in required if text.lower() not in prompt.lower()]
    if missing:
        raise WorkflowValidationError(f"generic agent prompt is missing required context: {missing}")


async def validate(path: Path, *, check_skills: bool = True) -> Workflow:
    workflow = load_workflow(path)
    validate_contract(workflow)
    if check_skills:
        await SkillManager(workflow.skill_repository, workflow.agent).initialize()
    return workflow


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Validate WORKFLOW.md without claiming an Issue")
    result.add_argument("workflow", nargs="?", type=Path, default=ROOT / "WORKFLOW.md")
    result.add_argument("--skip-skills", action="store_true")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        workflow = asyncio.run(validate(args.workflow.resolve(), check_skills=not args.skip_skills))
    except (OSError, SkillError, WorkflowError, WorkflowValidationError) as error:
        print(f"Workflow validation failed: {error}", file=sys.stderr)
        return 1
    print("Symphony one-agent workflow validation passed")
    print(f"  file: {workflow.path}")
    print(f"  max turns: {workflow.agent.max_turns}")
    print(f"  max concurrent Issues: {workflow.agent.max_concurrent_agents}")
    print(f"  skills: {len(workflow.agent.skills)}")
    print("  dispatch performed: no")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
