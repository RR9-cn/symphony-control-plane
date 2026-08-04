#!/usr/bin/env python3
"""Validate the Symphony overlay without modifying an upstream checkout."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

import tree_sitter_elixir
from tree_sitter import Language, Node, Parser

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "integrations" / "symphony_elixir"
LIB = INTEGRATION / "lib" / "symphony_elixir" / "fshows_control_plane"
PATCH = INTEGRATION / "patches" / "tracker-registry.patch"
EXPECTED_MODULES = {
    "adapter.ex": "SymphonyElixir.FshowsControlPlane.Adapter",
    "client.ex": "SymphonyElixir.FshowsControlPlane.Client",
    "agent_tool.ex": "SymphonyElixir.FshowsControlPlane.AgentTool",
}
EXPECTED_TOOLS = {
    "work_item_get",
    "work_item_add_event",
    "work_item_add_artifact",
    "work_item_request_human",
    "work_item_complete",
    "work_item_block",
}

ELIXIR_PARSER = Parser(Language(tree_sitter_elixir.language()))


def syntax_errors(node: Node) -> list[Node]:
    errors: list[Node] = []
    if node.type == "ERROR" or node.is_missing:
        errors.append(node)
    for child in node.children:
        errors.extend(syntax_errors(child))
    return errors


def validate_public_specs(path: Path, source: str) -> None:
    lines = source.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("def ") or stripped.startswith("defp "):
            continue
        window = "\n".join(lines[max(0, index - 8) : index])
        if "@spec" not in window and "@impl true" not in window:
            raise RuntimeError(f"public function lacks nearby @spec/@impl: {path.name}:{index + 1}")


def validate_sources() -> None:
    for filename, module in EXPECTED_MODULES.items():
        path = LIB / filename
        if not path.is_file():
            raise RuntimeError(f"missing Symphony module: {path.relative_to(ROOT)}")
        source = path.read_text(encoding="utf-8")
        tree = ELIXIR_PARSER.parse(source.encode("utf-8"))
        errors = syntax_errors(tree.root_node)
        if errors:
            locations = ", ".join(
                f"{node.start_point.row + 1}:{node.start_point.column + 1}"
                for node in errors[:5]
            )
            raise RuntimeError(f"Elixir syntax errors in {filename}: {locations}")
        if f"defmodule {module} do" not in source:
            raise RuntimeError(f"unexpected module declaration in {filename}")
        validate_public_specs(path, source)

    for path in (INTEGRATION / "test").rglob("*.exs"):
        source = path.read_text(encoding="utf-8")
        errors = syntax_errors(ELIXIR_PARSER.parse(source.encode("utf-8")).root_node)
        if errors:
            locations = ", ".join(
                f"{node.start_point.row + 1}:{node.start_point.column + 1}"
                for node in errors[:5]
            )
            raise RuntimeError(f"Elixir syntax errors in {path.name}: {locations}")

    agent_tool = (LIB / "agent_tool.ex").read_text(encoding="utf-8")
    tools = set(re.findall(r'tool\(\s*"(work_item_[a-z_]+)"', agent_tool))
    if tools != EXPECTED_TOOLS:
        raise RuntimeError(f"tool allowlist mismatch: {sorted(tools)}")
    if '"claim_token"' in agent_tool or '"work_item_id" =>' in agent_tool:
        raise RuntimeError("Agent tool schema or payload exposes claim_token/work_item_id")

    client = (LIB / "client.ex").read_text(encoding="utf-8")
    if ":ets" not in client or "claim_token" not in client:
        raise RuntimeError("Client does not implement host-side claim token storage")
    native_ref = client.split("native_ref:", 1)[1].split("identifier:", 1)[0]
    if "token" in native_ref:
        raise RuntimeError("Issue.native_ref exposes a token")

    patch = PATCH.read_text(encoding="utf-8")
    if '"fshows_control_plane" => SymphonyElixir.FshowsControlPlane.Adapter' not in patch:
        raise RuntimeError("Tracker registry patch does not register the adapter")


def validate_patch(symphony_root: Path) -> None:
    result = subprocess.run(
        ["git", "apply", "--check", str(PATCH)],
        cwd=symphony_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"registry patch does not apply: {result.stderr.strip()}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symphony-root", type=Path)
    args = parser.parse_args()
    try:
        validate_sources()
        if args.symphony_root:
            validate_patch(args.symphony_root.resolve())
    except RuntimeError as error:
        print(f"Symphony integration validation failed: {error}", file=sys.stderr)
        return 1
    print("Symphony integration validation passed")
    print("  modules: Adapter, Client, AgentTool")
    print(f"  restricted tools: {len(EXPECTED_TOOLS)}")
    print("  claim token exposure: none")
    if args.symphony_root:
        print("  tracker registry patch: applies cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
