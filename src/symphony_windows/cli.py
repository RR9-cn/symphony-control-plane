from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Sequence

from symphony_windows.orchestrator import WindowsSymphony
from symphony_windows.workflow import WorkflowError, load_workflow


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Windows-native Symphony runner")
    result.add_argument(
        "workflow",
        type=Path,
        nargs="?",
        default=Path("WORKFLOW.md"),
        help="Path to repository-owned WORKFLOW.md (default: ./WORKFLOW.md)",
    )
    result.add_argument(
        "--once",
        action="store_true",
        help="Poll once and wait for the dispatched batch to finish",
    )
    result.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
    )
    return result


async def run(args: argparse.Namespace) -> int:
    workflow = load_workflow(args.workflow)
    async with WindowsSymphony(workflow) as orchestrator:
        if args.once:
            outcomes = await orchestrator.run_once()
            for outcome in outcomes:
                logging.getLogger(__name__).info(
                    "Issue %s finished with %s",
                    outcome.issue_id,
                    outcome.status,
                )
            return 1 if any(outcome.error for outcome in outcomes) else 0
        await orchestrator.serve()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=args.log_level, handlers=[handler], force=True)
    try:
        return asyncio.run(run(args))
    except WorkflowError as error:
        logging.getLogger(__name__).error("invalid workflow: %s", error)
        return 2
    except KeyboardInterrupt:
        return 130
