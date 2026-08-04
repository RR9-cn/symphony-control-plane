from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_ROOT = REPOSITORY_ROOT / "protocol"


@dataclass(frozen=True)
class Transition:
    from_status: str
    to_status: str
    event: str
    actor: str
    guards: tuple[str, ...]
    effects: tuple[str, ...]


@dataclass(frozen=True)
class ProtocolDefinition:
    statuses: frozenset[str]
    terminal_statuses: frozenset[str]
    roles: dict[str, dict[str, Any]]
    transitions: dict[tuple[str, str, str], Transition]

    def transition(self, from_status: str, to_status: str, event: str) -> Transition:
        try:
            return self.transitions[(from_status, to_status, event)]
        except KeyError as error:
            raise ValueError(
                f"transition {from_status} -> {to_status} via {event} is not allowed"
            ) from error


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise RuntimeError(f"protocol file must contain a mapping: {path}")
    return value


@lru_cache(maxsize=1)
def get_protocol() -> ProtocolDefinition:
    machine = _load_yaml(PROTOCOL_ROOT / "state-machine.yaml")
    roles = _load_yaml(PROTOCOL_ROOT / "agent-roles.yaml")["roles"]
    transitions: dict[tuple[str, str, str], Transition] = {}
    for item in machine["transitions"]:
        transition = Transition(
            from_status=item["from"],
            to_status=item["to"],
            event=item["event"],
            actor=item["actor"],
            guards=tuple(item.get("guards", [])),
            effects=tuple(item.get("effects", [])),
        )
        transitions[(transition.from_status, transition.to_status, transition.event)] = transition
    return ProtocolDefinition(
        statuses=frozenset(machine["statuses"]),
        terminal_statuses=frozenset(machine["terminal_statuses"]),
        roles=roles,
        transitions=transitions,
    )


PROTOCOL = get_protocol()
ROLE_STAGE = {name: definition["stage"] for name, definition in PROTOCOL.roles.items()}
