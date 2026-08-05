#!/usr/bin/env python3
"""Validate the Symphony Issue protocol without mutating runtime state."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from control_plane.models import ISSUE_STATUSES  # noqa: E402
from control_plane.protocol import PROTOCOL  # noqa: E402


def main() -> int:
    schema_path = ROOT / "protocol" / "schemas" / "issue.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    sample = {
        "id": "ISSUE-001",
        "project_id": "project-001",
        "title": "Validate protocol",
        "description": "One generic coding agent completes this Issue.",
        "priority": 1,
        "acceptance_criteria": ["Protocol validates"],
    }
    Draft202012Validator(schema).validate(sample)
    if PROTOCOL.statuses != ISSUE_STATUSES:
        raise RuntimeError(
            f"protocol/model status mismatch: protocol={sorted(PROTOCOL.statuses)}, model={sorted(ISSUE_STATUSES)}"
        )
    machine = yaml.safe_load((ROOT / "protocol" / "state-machine.yaml").read_text(encoding="utf-8"))
    keys = [(row["from"], row["to"], row["event"]) for row in machine["transitions"]]
    if len(keys) != len(set(keys)):
        raise RuntimeError("state machine contains duplicate transitions")
    if any(source not in ISSUE_STATUSES or target not in ISSUE_STATUSES for source, target, _ in keys):
        raise RuntimeError("state machine transition references an unknown status")
    print("Symphony Issue protocol validation passed")
    print(f"  statuses: {len(PROTOCOL.statuses)}")
    print(f"  transitions: {len(PROTOCOL.transitions)}")
    print("  compatibility model: none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
