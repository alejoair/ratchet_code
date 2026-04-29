"""CLI entry point for the ratchet autonomous code solver.

Usage:
    ratchet <repo_path> --prompt TEXT
    ratchet <repo_path> --prompt-file PATH
    python -m ratchet <repo_path> --prompt TEXT
"""

import argparse
import asyncio
import json
import logging
import sys
import time

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the ratchet CLI."""
    parser = argparse.ArgumentParser(
        prog="ratchet",
        description="Ratchet autonomous code solver",
    )
    parser.add_argument(
        "repo_path",
        help="Absolute path to the target repository",
    )

    prompt_group = parser.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument(
        "--prompt",
        help="Problem statement (inline text)",
    )
    prompt_group.add_argument(
        "--prompt-file",
        dest="prompt_file",
        help="Path to a file containing the problem statement",
    )

    parser.add_argument(
        "--model",
        default=None,
        help="Claude model ID (overrides ratchet.config.json)",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=250,
        dest="max_turns",
        help="Maximum agentic turns (default: 250)",
    )
    parser.add_argument(
        "--cost-limit",
        type=float,
        default=0.0,
        dest="cost_limit",
        help="Max spend in USD per run (0 = unlimited)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to ratchet.config.json (default: <repo_path>/ratchet.config.json)",
    )
    return parser


def main() -> None:
    """Entry point for the ratchet CLI.

    Runs the solver and prints a RATCHET_METRICS JSON line to stdout so the
    vexp-swe-bench harness can capture token usage and cost.
    """
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    parser = _build_parser()
    args = parser.parse_args()

    if args.prompt_file:
        try:
            with open(args.prompt_file, encoding="utf-8") as fh:
                prompt = fh.read()
        except OSError as exc:
            logger.error("Cannot read prompt file: %s", exc)
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
        "tool_calls": {},
    }

    start_ms = int(time.time() * 1000)
    exit_code = 0

    try:
        from ratchet.solve import solve  # noqa: PLC0415

        asyncio.run(solve(args.repo_path, prompt, args.config))
    except NotImplementedError:
        logger.warning("solve() not yet implemented — producing empty patch")
    except Exception as exc:  # noqa: BLE001
        logger.error("ratchet solve failed: %s", exc, exc_info=True)
        exit_code = 1
    finally:
        metrics["duration_ms"] = int(time.time() * 1000) - start_ms

    # Always emit metrics so the harness adapter can parse them.
    print(f"RATCHET_METRICS:{json.dumps(metrics)}", flush=True)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
