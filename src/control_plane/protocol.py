from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


PROTOCOL_ROOT = Path(__file__).resolve().parents[2] / "protocol"


@dataclass(frozen=True)
class Transition:
    from_status: str
    to_status: str
    event: str
    actor: str


@dataclass(frozen=True)
class ProtocolDefinition:
    statuses: frozenset[str]
    terminal_statuses: frozenset[str]
    transitions: dict[tuple[str, str, str], Transition]

    def transition(self, from_status: str, to_status: str, event: str) -> Transition:
        try:
            return self.transitions[(from_status, to_status, event)]
        except KeyError as error:
            raise ValueError(
                f"transition {from_status} -> {to_status} via {event} is not allowed"
            ) from error


def _load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"protocol file must contain a mapping: {path}")
    return value


@lru_cache(maxsize=1)
def get_protocol() -> ProtocolDefinition:
    machine = _load_yaml(PROTOCOL_ROOT / "state-machine.yaml")
    transitions = {
        (item["from"], item["to"], item["event"]): Transition(
            from_status=item["from"],
            to_status=item["to"],
            event=item["event"],
            actor=item["actor"],
        )
        for item in machine["transitions"]
    }
    return ProtocolDefinition(
        statuses=frozenset(machine["statuses"]),
        terminal_statuses=frozenset(machine["terminal_statuses"]),
        transitions=transitions,
    )


PROTOCOL = get_protocol()
