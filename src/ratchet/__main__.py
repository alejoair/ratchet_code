"""CLI entry point for the ratchet autonomous code solver.

Usage:
    ratchet solve <repo_path> --prompt TEXT
    ratchet chat <repo_path> [--model MODEL]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time

from ratchet.config import Config

logger = logging.getLogger(__name__)


def _load_config(
    config_path: str | None,
    repo_path: str,
    model: str | None,
) -> tuple[Config, str]:
    """Load or create a Config and resolve the model."""
    default_model = "claude-sonnet-4-6"
    cfg: Config | None = None
    resolved = config_path
    if resolved is None:
        from pathlib import Path  # noqa: PLC0415

        candidate = Path(repo_path) / "ratchet.config.json"
        if candidate.is_file():
            resolved = str(candidate)
            logger.info(
                "Using config from %s", resolved,
            )

    if resolved is not None:
        try:
            cfg = Config.load(resolved)
        except (FileNotFoundError, ValueError):
            logger.warning(
                "Could not load config from %s, "
                "using defaults",
                resolved,
            )

    active_model = model or default_model
    if cfg is None:
        cfg = Config(models={"planner": active_model})

    return cfg, active_model


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the ratchet CLI."""
    parser = argparse.ArgumentParser(
        prog="ratchet",
        description="Ratchet autonomous code solver",
    )
    subs = parser.add_subparsers(dest="command")

    # --- solve subcommand (default) ---
    solve_p = subs.add_parser(
        "solve", help="Run the solver on a repo",
    )
    solve_p.add_argument(
        "repo_path",
        help="Absolute path to the target repository",
    )
    pg = solve_p.add_mutually_exclusive_group(required=True)
    pg.add_argument(
        "--prompt",
        help="Problem statement (inline text)",
    )
    pg.add_argument(
        "--prompt-file",
        dest="prompt_file",
        help="Path to file containing problem statement",
    )
    solve_p.add_argument(
        "--model",
        default=None,
        help="Claude model ID (overrides config)",
    )
    solve_p.add_argument(
        "--max-turns",
        type=int,
        default=250,
        dest="max_turns",
        help="Maximum agentic turns (default: 250)",
    )
    solve_p.add_argument(
        "--cost-limit",
        type=float,
        default=0.0,
        dest="cost_limit",
        help="Max spend in USD per run (0 = unlimited)",
    )
    solve_p.add_argument(
        "--config",
        default=None,
        help=(
            "Path to ratchet.config.json "
            "(default: <repo_path>/ratchet.config.json)"
        ),
    )
    solve_p.add_argument(
        "--instance-id",
        default=None,
        dest="instance_id",
        help="SWE-bench instance ID for log/plan files",
    )

    # --- chat subcommand ---
    chat_p = subs.add_parser(
        "chat",
        help="Interactive chat with the planner",
    )
    chat_p.add_argument(
        "repo_path",
        nargs="?",
        default=os.getcwd(),
        help=(
            "Path to the target repository "
            "(default: current directory)"
        ),
    )
    chat_p.add_argument(
        "--model",
        default=None,
        help="Claude model ID (overrides config)",
    )
    chat_p.add_argument(
        "--config",
        default=None,
        help=(
            "Path to ratchet.config.json "
            "(default: <repo_path>/ratchet.config.json)"
        ),
    )

    return parser


def _setup_logging(
    repo_path: str, suffix: str,
) -> None:
    """Configure logging to stderr and a file next to repo."""
    log_path = (
        repo_path.rstrip("/\\") + suffix + ".ratchet.log"
    )
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
    )
    handler = logging.FileHandler(
        log_path, encoding="utf-8", mode="w",
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(name)s %(levelname)s %(message)s",
        ),
    )
    logging.getLogger().addHandler(handler)


def _run_solve(args: argparse.Namespace) -> None:
    """Execute the solve subcommand."""
    suffix = (
        f".{args.instance_id}" if args.instance_id else ""
    )
    _setup_logging(args.repo_path, suffix)

    if args.prompt_file:
        try:
            with open(args.prompt_file, encoding="utf-8") as fh:
                prompt = fh.read()
        except OSError as exc:
            logger.error(
                "Cannot read prompt file: %s", exc,
            )
            sys.exit(1)
    else:
        prompt = args.prompt

    metrics: dict[str, object] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cost_usd": 0.0,
        "num_turns": 0,
        "duration_ms": 0,
    }

    start_ms = int(time.time() * 1000)
    exit_code = 0
    result = None

    try:
        from ratchet.solve import solve  # noqa: PLC0415

        kw: dict[str, str] = {}
        if args.model is not None:
            kw["model"] = args.model
        result = asyncio.run(
            solve(
                args.repo_path,
                prompt,
                args.config,
                **kw,
            ),
        )
        metrics["input_tokens"] = result.input_tokens
        metrics["output_tokens"] = result.output_tokens
        metrics["cache_read_tokens"] = (
            result.cache_read_tokens
        )
        metrics["cache_creation_tokens"] = (
            result.cache_creation_tokens
        )
        metrics["cost_usd"] = result.cost_usd
        metrics["num_turns"] = result.num_turns
    except NotImplementedError:
        logger.warning(
            "solve() not yet implemented "
            "-- producing empty patch",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "ratchet solve failed: %s",
            exc,
            exc_info=True,
        )
        exit_code = 1
    finally:
        metrics["duration_ms"] = (
            int(time.time() * 1000) - start_ms
        )

    print(
        f"RATCHET_METRICS:{json.dumps(metrics)}",
        flush=True,
    )

    if result is not None and result.plan_trace is not None:
        plan_path = (
            args.repo_path.rstrip("/\\")
            + suffix
            + ".ratchet_plan.json"
        )
        try:
            with open(plan_path, "w", encoding="utf-8") as pf:
                json.dump(
                    result.plan_trace, pf, indent=2,
                    default=str,
                )
        except OSError as exc:
            logger.warning(
                "Could not write plan file: %s", exc,
            )

    sys.exit(exit_code)


def _run_chat(args: argparse.Namespace) -> None:
    """Execute the chat subcommand."""
    _setup_logging(args.repo_path, "")
    cfg, model = _load_config(
        args.config, args.repo_path, args.model,
    )
    from ratchet.chat import run_chat  # noqa: PLC0415

    asyncio.run(run_chat(args.repo_path, cfg, model))


def main() -> None:
    """Entry point for the ratchet CLI."""
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "chat":
        _run_chat(args)
    else:
        _run_solve(args)


if __name__ == "__main__":
    main()
