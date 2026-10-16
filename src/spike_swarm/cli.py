"""Operator CLI: ``python -m spike_swarm sweep``.

Argument handling stays deliberately small. The interesting limits (three
challenges, three solvers each) are enforced here as usage errors so a bad
invocation cannot quietly launch twelve containers or trip the platform's
instance cap.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .models import (
    DEFAULT_COUNT,
    DEFAULT_IMAGE,
    DEFAULT_MODEL,
    DEFAULT_PARALLEL,
    DEFAULT_PROVIDER,
    DEFAULT_ROUND_SECONDS,
    DEFAULT_WALL_TIME_SECONDS,
    MAX_PARALLEL_TASKS,
    MAX_SESSIONS_PER_TASK,
    SpikeError,
)
from .sweep import (
    EXIT_USAGE,
    SweepConfig,
    plan_only,
    run_sweep,
)

DEFAULT_AGENT_DIR = Path("spike/agent")
DEFAULT_KNOWLEDGE_ROOTS = (Path("skills/offensive"), Path("knowledge"))
DEFAULT_RUNS_ROOT = Path(".spike/runs")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spike_swarm",
        description=(
            "Pi-swarm spike: three challenges, three Pi solvers each, one "
            "runtime-rendered intent-fact graph per challenge."
        ),
    )
    parser.add_argument("--version", action="version", version=f"spike_swarm {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sweep = subparsers.add_parser("sweep", help="run challenges concurrently")
    sweep.add_argument(
        "--count",
        type=int,
        default=DEFAULT_COUNT,
        help=f"how many challenges to solve at once (max {MAX_PARALLEL_TASKS})",
    )
    sweep.add_argument(
        "--parallel",
        type=int,
        default=DEFAULT_PARALLEL,
        help=f"how many challenges run concurrently (max {MAX_PARALLEL_TASKS})",
    )
    sweep.add_argument(
        "--challenge",
        action="append",
        default=[],
        metavar="CODE",
        help="explicit challenge code; repeat up to --count times",
    )
    sweep.add_argument("--wall-time", type=int, default=DEFAULT_WALL_TIME_SECONDS)
    sweep.add_argument("--round-seconds", type=int, default=DEFAULT_ROUND_SECONDS)
    sweep.add_argument("--image", default=DEFAULT_IMAGE)
    sweep.add_argument("--provider", default=DEFAULT_PROVIDER)
    sweep.add_argument("--model", default=DEFAULT_MODEL)
    sweep.add_argument(
        "--passthrough",
        action="append",
        default=[],
        metavar="VAR",
        help="host environment variable to expose inside every solver container",
    )
    sweep.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    sweep.add_argument("--agent-dir", type=Path, default=DEFAULT_AGENT_DIR)
    sweep.add_argument(
        "--knowledge-root",
        action="append",
        type=Path,
        default=None,
        help="read-only markdown root mounted at /knowledge/<name>; repeatable",
    )
    sweep.add_argument("--base-url-env", default="BENCHMARK_BASE_URL")
    sweep.add_argument("--token-env", default="BENCHMARK_TOKEN")
    sweep.add_argument(
        "--plan-only",
        action="store_true",
        help="print the selection and docker commands; requires explicit --challenge codes",
    )
    return parser


def config_from_args(args: argparse.Namespace) -> SweepConfig:
    if not 1 <= args.count <= MAX_PARALLEL_TASKS:
        raise SpikeError(f"--count must be between 1 and {MAX_PARALLEL_TASKS}")
    if not 1 <= args.parallel <= MAX_PARALLEL_TASKS:
        raise SpikeError(f"--parallel must be between 1 and {MAX_PARALLEL_TASKS}")
    if args.parallel > args.count:
        raise SpikeError("--parallel cannot exceed --count")
    if args.wall_time < 1:
        raise SpikeError("--wall-time must be at least 1 second")
    if args.round_seconds < 5:
        raise SpikeError("--round-seconds must be at least 5 seconds")
    knowledge = tuple(args.knowledge_root) if args.knowledge_root else DEFAULT_KNOWLEDGE_ROOTS
    for root in knowledge:
        if not root.is_dir():
            raise SpikeError(f"knowledge root is not a directory: {root}")
    if not (args.agent_dir / "system-prompt.md").is_file():
        raise SpikeError(f"solver contract not found: {args.agent_dir / 'system-prompt.md'}")
    return SweepConfig(
        runs_root=args.runs_root,
        agent_dir=args.agent_dir,
        knowledge_roots=knowledge,
        image=args.image,
        provider=args.provider,
        model=args.model,
        challenge_count=args.count,
        parallel=args.parallel,
        wall_time=args.wall_time,
        round_seconds=args.round_seconds,
        passthrough=tuple(args.passthrough),
        base_url_env=args.base_url_env,
        token_env=args.token_env,
        challenges=tuple(args.challenge),
        plan_only=args.plan_only,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        config = config_from_args(args)
    except SpikeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if config.plan_only:
        try:
            plan = plan_only(config)
        except SpikeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_USAGE
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        print(
            f"\nplan only: {len(plan)} container(s) across {len(config.challenges)} "
            f"challenge(s), {MAX_SESSIONS_PER_TASK} solvers each",
            file=sys.stderr,
        )
        return 0

    try:
        report = asyncio.run(run_sweep(config))
    except KeyboardInterrupt:  # pragma: no cover - operator abort
        print("interrupted; teardown attempted to close every started instance", file=sys.stderr)
        return 130
    for outcome in report.outcomes:
        print(
            f"{outcome.code}: {outcome.status} ({outcome.exit_reason}) "
            f"rounds={outcome.rounds} flags={len(outcome.flags)}"
        )
    if report.message:
        print(f"message: {report.message}", file=sys.stderr)
    if report.platform_kind:
        print(f"platform failure: {report.platform_kind}", file=sys.stderr)
    print(f"runs: {config.runs_root}")
    return report.exit_code


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
