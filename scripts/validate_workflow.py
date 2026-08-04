#!/usr/bin/env python3
"""Validate the repository-owned Windows Symphony workflow without dispatching work."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Sequence

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from symphony_windows.skill import SkillError, SkillManager  # noqa: E402
from symphony_windows.workflow import Workflow, WorkflowError, load_workflow  # noqa: E402


class WorkflowValidationError(RuntimeError):
    """The formal repository workflow has drifted from the control-plane contract."""


def expected_roles() -> dict[str, str]:
    protocol_path = ROOT / "protocol" / "agent-roles.yaml"
    payload = yaml.safe_load(protocol_path.read_text(encoding="utf-8"))
    roles = payload.get("roles") if isinstance(payload, dict) else None
    if not isinstance(roles, dict) or not roles:
        raise WorkflowValidationError("protocol/agent-roles.yaml defines no roles")
    result: dict[str, str] = {}
    for role, definition in roles.items():
        stage = definition.get("stage") if isinstance(definition, dict) else None
        if not isinstance(role, str) or not isinstance(stage, str):
            raise WorkflowValidationError("protocol role or stage is invalid")
        result[role] = stage
    return result


def validate_contract(workflow: Workflow) -> None:
    roles = expected_roles()
    configured = {profile.agent_role for profile in workflow.agent_profiles}
    if configured != set(roles):
        missing = sorted(set(roles) - configured)
        extra = sorted(configured - set(roles))
        raise WorkflowValidationError(
            f"agent profile roles drifted; missing={missing}, extra={extra}"
        )

    sample_commit = "0" * 40
    for index, (role, stage) in enumerate(roles.items(), start=1):
        issue: dict[str, Any] = {
            "id": f"WI-VALIDATE-{index:03d}",
            "identifier": f"WI-VALIDATE-{index:03d}",
            "title": f"Validate {role}",
            "description": "Repository-owned WORKFLOW.md validation fixture.",
            "agent_role": role,
            "stage": stage,
            "acceptance_criteria": ["The formal profile prompt renders strictly."],
            "repository": {
                "url": str(ROOT),
                "base_branch": "master",
                "head_branch": None,
                "commit": sample_commit,
                "pull_request": None,
            },
        }
        profile = workflow.profile_for(issue)
        rendered = profile.render_prompt(issue, 1)
        if issue["identifier"] not in rendered or role not in rendered:
            raise WorkflowValidationError(
                f"rendered prompt for {profile.name} lost WorkItem context"
            )
        if not profile.skills:
            raise WorkflowValidationError(f"profile {profile.name} has no Skill allowlist")


async def validate(path: Path, *, check_skills: bool = True) -> Workflow:
    workflow = load_workflow(path)
    validate_contract(workflow)
    if check_skills:
        await SkillManager(
            workflow.skill_repository,
            workflow.agent_profiles,
        ).initialize()
    return workflow


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Validate WORKFLOW.md without claiming work or starting Codex"
    )
    result.add_argument(
        "workflow",
        nargs="?",
        type=Path,
        default=ROOT / "WORKFLOW.md",
    )
    result.add_argument(
        "--skip-skills",
        action="store_true",
        help="Skip pinned Skill checkout and compatibility validation",
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        workflow = asyncio.run(
            validate(args.workflow.resolve(), check_skills=not args.skip_skills)
        )
    except (OSError, SkillError, WorkflowError, WorkflowValidationError) as error:
        print(f"Workflow validation failed: {error}", file=sys.stderr)
        return 1

    print("Workflow validation passed")
    print(f"  file: {workflow.path}")
    print(f"  profiles: {len(workflow.agent_profiles)}")
    print(f"  polling interval: {workflow.polling_interval_ms}ms")
    print(f"  max concurrent agents: {workflow.agent.max_concurrent_agents}")
    print(f"  skill revision: {workflow.skill_repository.revision}")
    print(f"  skill compatibility checked: {not args.skip_skills}")
    print("  dispatch performed: no")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
