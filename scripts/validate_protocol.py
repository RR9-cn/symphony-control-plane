#!/usr/bin/env python3
"""Validate the v1 scheduling protocol and its executable example fixture."""

from __future__ import annotations

import copy
import fnmatch
import json
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "protocol"
EXAMPLE = PROTOCOL / "examples" / "FEATURE-001"
FEATURE_ROOT = EXAMPLE / "feature-root"


class ProtocolError(RuntimeError):
    pass


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise ProtocolError(f"{path.relative_to(ROOT)} must contain one mapping")
    return value


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ProtocolError(f"{path.relative_to(ROOT)} must contain one object")
    return value


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def assert_relative_posix(path: str) -> None:
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or "\\" in path:
        raise ProtocolError(f"non-canonical artifact path: {path}")


def validate_schema_instances(
    schema_path: Path, instance_paths: list[Path]
) -> tuple[Draft202012Validator, list[dict[str, Any]]]:
    schema = load_json(schema_path)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    instances: list[dict[str, Any]] = []
    for path in instance_paths:
        instance = load_yaml(path)
        errors = sorted(validator.iter_errors(instance), key=lambda item: list(item.path))
        if errors:
            details = "; ".join(
                f"{'.'.join(map(str, error.path)) or '<root>'}: {error.message}"
                for error in errors
            )
            raise ProtocolError(f"{path.relative_to(ROOT)} failed schema validation: {details}")
        instances.append(instance)
    return validator, instances


def validate_roles(
    roles_doc: dict[str, Any], work_item_schema: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    roles = roles_doc.get("roles", {})
    expected_roles = set(work_item_schema["properties"]["agent_role"]["enum"])
    if set(roles) != expected_roles:
        raise ProtocolError(
            f"role set mismatch: expected {sorted(expected_roles)}, got {sorted(roles)}"
        )

    expected_stages = set(work_item_schema["properties"]["stage"]["enum"])
    actual_stages = {role["stage"] for role in roles.values()}
    if actual_stages != expected_stages:
        raise ProtocolError(
            f"stage set mismatch: expected {sorted(expected_stages)}, got {sorted(actual_stages)}"
        )

    required = {
        "stage",
        "reads",
        "writes",
        "skills",
        "sandbox",
        "network_access",
        "business_code_write",
        "completion_conditions",
        "human_confirmation_triggers",
    }
    for name, role in roles.items():
        missing = required - set(role)
        if missing:
            raise ProtocolError(f"role {name} misses fields: {sorted(missing)}")
        if not role["skills"] or not role["completion_conditions"]:
            raise ProtocolError(f"role {name} must define skills and completion conditions")
        if not role["human_confirmation_triggers"]:
            raise ProtocolError(f"role {name} must define human confirmation triggers")

    guards = roles_doc.get("global_guards", {})
    human_gates = set(guards.get("require_human_authorization", []))
    if not {"git_push", "merge", "release"}.issubset(human_gates):
        raise ProtocolError("global role guards must gate git_push, merge and release")
    return roles


def validate_state_machine(
    state_machine: dict[str, Any], work_item_schema: dict[str, Any]
) -> set[tuple[str, str, str]]:
    statuses = set(state_machine.get("statuses", {}))
    schema_statuses = set(work_item_schema["properties"]["status"]["enum"])
    if statuses != schema_statuses:
        raise ProtocolError(
            f"status set mismatch: schema={sorted(schema_statuses)}, machine={sorted(statuses)}"
        )

    terminals = set(state_machine["terminal_statuses"])
    if terminals != {"done", "cancelled"}:
        raise ProtocolError("v1 terminal statuses must be done and cancelled")

    transitions: set[tuple[str, str, str]] = set()
    outgoing: dict[str, int] = {status: 0 for status in statuses}
    for transition in state_machine["transitions"]:
        key = (transition["from"], transition["to"], transition["event"])
        if key in transitions:
            raise ProtocolError(f"duplicate transition: {key}")
        if transition["from"] not in statuses or transition["to"] not in statuses:
            raise ProtocolError(f"transition references unknown status: {key}")
        transitions.add(key)
        outgoing[transition["from"]] += 1

    for status, count in outgoing.items():
        if status in terminals and count:
            raise ProtocolError(f"terminal status {status} has outgoing transitions")
        if status not in terminals and not count:
            raise ProtocolError(f"non-terminal status {status} has no outgoing transition")
    return transitions


def validate_dependency_graph(work_items: dict[str, dict[str, Any]]) -> None:
    for work_item in work_items.values():
        unknown = set(work_item["dependencies"]) - set(work_items)
        if unknown:
            raise ProtocolError(f"{work_item['id']} has unknown dependencies: {sorted(unknown)}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(item_id: str) -> None:
        if item_id in visiting:
            raise ProtocolError(f"dependency cycle detected at {item_id}")
        if item_id in visited:
            return
        visiting.add(item_id)
        for dependency in work_items[item_id]["dependencies"]:
            visit(dependency)
        visiting.remove(item_id)
        visited.add(item_id)

    for item_id in work_items:
        visit(item_id)


def artifact_patterns(layout: dict[str, Any]) -> list[str]:
    patterns: list[str] = []
    for definition in layout["artifacts"].values():
        pattern = definition.get("path") or definition.get("path_pattern")
        if not pattern:
            raise ProtocolError("artifact definition must have path or path_pattern")
        patterns.append(pattern)
    return patterns


def validate_artifacts(
    work_items: dict[str, dict[str, Any]],
    handoffs: dict[str, dict[str, Any]],
    roles: dict[str, dict[str, Any]],
    layout: dict[str, Any],
) -> None:
    canonical_patterns = artifact_patterns(layout)
    deprecated_patterns = layout.get("deprecated_aliases", [])

    def check_path(path: str) -> None:
        assert_relative_posix(path)
        if matches_any(path, deprecated_patterns):
            raise ProtocolError(f"deprecated artifact path used: {path}")
        if not matches_any(path, canonical_patterns):
            raise ProtocolError(f"artifact path is outside the v1 layout: {path}")
        if not (FEATURE_ROOT / Path(*PurePosixPath(path).parts)).is_file():
            raise ProtocolError(f"fixture artifact does not exist: {path}")

    for item in work_items.values():
        role = roles[item["agent_role"]]
        for artifact in item["input_artifacts"]:
            path = artifact["path"]
            check_path(path)
            if not matches_any(path, role["reads"]):
                raise ProtocolError(f"{item['agent_role']} may not read {path}")
        for artifact in item["output_artifacts"]:
            path = artifact["path"]
            check_path(path)
            if not matches_any(path, role["writes"]):
                raise ProtocolError(f"{item['agent_role']} may not write {path}")

    for item_id, handoff in handoffs.items():
        item = work_items[item_id]
        role = roles[item["agent_role"]]
        for artifact in handoff["inputs"]:
            path = artifact["path"]
            check_path(path)
            if not matches_any(path, role["reads"]):
                raise ProtocolError(f"{item['agent_role']} may not read handoff input {path}")
        for artifact in handoff["outputs"]:
            path = artifact["path"]
            check_path(path)
            if not matches_any(path, role["writes"]):
                raise ProtocolError(f"{item['agent_role']} may not write handoff output {path}")

        handoff_path = f"orchestration/handoffs/{item_id}.yaml"
        if not matches_any(handoff_path, role["writes"]):
            raise ProtocolError(f"{item['agent_role']} may not write its handoff")


def validate_handoff_links(
    work_items: dict[str, dict[str, Any]], handoffs: dict[str, dict[str, Any]]
) -> None:
    if set(work_items) != set(handoffs):
        raise ProtocolError("each fixture WorkItem must have exactly one final Handoff")
    ordered_ids = sorted(work_items)
    for index, item_id in enumerate(ordered_ids):
        item = work_items[item_id]
        handoff = handoffs[item_id]
        if handoff["work_item_id"] != item_id:
            raise ProtocolError(f"handoff filename/id mismatch for {item_id}")
        if handoff["agent_role"] != item["agent_role"]:
            raise ProtocolError(f"handoff role mismatch for {item_id}")
        if {entry["path"] for entry in handoff["outputs"]} != {
            entry["path"] for entry in item["output_artifacts"]
        }:
            raise ProtocolError(f"handoff outputs mismatch WorkItem outputs for {item_id}")
        expected_next = (
            work_items[ordered_ids[index + 1]]["agent_role"]
            if index + 1 < len(ordered_ids)
            else None
        )
        if handoff["recommended_next_role"] != expected_next:
            raise ProtocolError(f"unexpected recommended_next_role for {item_id}")


def validate_scenario(
    scenario: dict[str, Any],
    work_items: dict[str, dict[str, Any]],
    transitions: set[tuple[str, str, str]],
) -> None:
    if scenario["feature_id"] != "FEATURE-001":
        raise ProtocolError("fixture scenario has unexpected feature_id")
    ordered_items = [work_items[item_id] for item_id in sorted(work_items)]
    actual_role_order = [item["agent_role"] for item in ordered_items]
    if actual_role_order != scenario["expected_role_order"]:
        raise ProtocolError("fixture does not exercise roles in the declared order")

    current = {item_id: "draft" for item_id in work_items}
    reached: set[str] = {"draft"}
    for step in scenario["steps"]:
        item_id = step["work_item_id"]
        if item_id not in work_items:
            raise ProtocolError(f"scenario references unknown WorkItem {item_id}")
        if current[item_id] != step["from"]:
            raise ProtocolError(
                f"scenario state mismatch for {item_id}: current={current[item_id]}, "
                f"step.from={step['from']}"
            )
        key = (step["from"], step["to"], step["event"])
        if key not in transitions:
            raise ProtocolError(f"scenario uses undefined transition: {key}")
        if step["from"] == "draft" and step["to"] == "ready":
            incomplete = [
                dep for dep in work_items[item_id]["dependencies"] if current[dep] != "done"
            ]
            if incomplete:
                raise ProtocolError(
                    f"{item_id} readied before dependencies completed: {incomplete}"
                )
        current[item_id] = step["to"]
        reached.add(step["to"])

    expected_final = {item_id: item["status"] for item_id, item in work_items.items()}
    if current != expected_final:
        raise ProtocolError(f"scenario final state mismatch: {current} != {expected_final}")
    required_recovery = set(scenario["required_recovery_statuses"])
    if not required_recovery.issubset(reached):
        raise ProtocolError(
            f"scenario misses recovery statuses: {sorted(required_recovery - reached)}"
        )


def validate_claim_invariant(
    validator: Draft202012Validator, exemplar: dict[str, Any]
) -> None:
    running = copy.deepcopy(exemplar)
    running["status"] = "running"
    running["claim"] = {
        "worker_id": "symphony-01",
        "token": "0123456789abcdef",
        "expires_at": "2026-08-03T04:00:00Z",
    }
    validator.validate(running)

    broken = copy.deepcopy(running)
    broken["claim"]["token"] = None
    if not list(validator.iter_errors(broken)):
        raise ProtocolError("running WorkItem without claim token unexpectedly validates")

    leaked = copy.deepcopy(exemplar)
    leaked["claim"] = running["claim"]
    if not list(validator.iter_errors(leaked)):
        raise ProtocolError("non-running WorkItem with active claim unexpectedly validates")


def main() -> int:
    try:
        work_item_schema_path = PROTOCOL / "schemas" / "work-item.schema.json"
        handoff_schema_path = PROTOCOL / "schemas" / "handoff.schema.json"
        work_item_paths = sorted((EXAMPLE / "work-items").glob("WI-*.yaml"))
        handoff_paths = sorted(
            (FEATURE_ROOT / "orchestration" / "handoffs").glob("WI-*.yaml")
        )
        if len(work_item_paths) != 5 or len(handoff_paths) != 5:
            raise ProtocolError("FEATURE-001 must contain five WorkItems and five Handoffs")

        work_item_validator, work_item_list = validate_schema_instances(
            work_item_schema_path, work_item_paths
        )
        _, handoff_list = validate_schema_instances(handoff_schema_path, handoff_paths)
        work_items = {item["id"]: item for item in work_item_list}
        handoffs = {handoff["work_item_id"]: handoff for handoff in handoff_list}

        work_item_schema = load_json(work_item_schema_path)
        roles = validate_roles(load_yaml(PROTOCOL / "agent-roles.yaml"), work_item_schema)
        transitions = validate_state_machine(
            load_yaml(PROTOCOL / "state-machine.yaml"), work_item_schema
        )
        validate_dependency_graph(work_items)
        validate_handoff_links(work_items, handoffs)
        validate_artifacts(
            work_items,
            handoffs,
            roles,
            load_yaml(PROTOCOL / "artifact-layout.yaml"),
        )
        validate_scenario(load_yaml(EXAMPLE / "scenario.yaml"), work_items, transitions)
        validate_claim_invariant(work_item_validator, work_item_list[0])
    except (ProtocolError, KeyError, TypeError, ValueError, yaml.YAMLError) as error:
        print(f"protocol validation failed: {error}", file=sys.stderr)
        return 1

    print("protocol validation passed")
    print(f"  roles: {len(roles)}")
    print(f"  statuses: {len(load_yaml(PROTOCOL / 'state-machine.yaml')['statuses'])}")
    print(f"  transitions: {len(transitions)}")
    print(f"  work items: {len(work_items)}")
    print(f"  handoffs: {len(handoffs)}")
    print("  recovery paths: blocked, retry_queued, rework, needs_human")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
