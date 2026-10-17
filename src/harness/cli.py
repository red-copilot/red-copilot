from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from .benchmark.base import BENCHMARK_MAX_UNCONFIRMED_PROGRESS
from .benchmark.tsec_adapter import (
    MAX_EVALUATION_WALL_TIME_SECONDS,
    MAX_PARALLEL_CHALLENGES,
    run_tsec_challenges,
)
from .benchmark.tsec_client import TSecPlatformError, load_tsec_config
from .gateway_runtime import GatewayConfig
from .models import BudgetSpec, load_agent, load_task
from .orchestrator import Orchestrator
from .preflight import run_preflight
from .runtime.results import find_empty_run_dirs

app = typer.Typer(
    name="harness",
    help="Reproducible evaluation runtime for security agents.",
    no_args_is_help=True,
)


@app.command("subagent")
def subagent_command(
    objective: Annotated[str, typer.Option("--objective", "-o", help="The focused task for the sub-agent.")],
    agent: Annotated[
        Path | None,
        typer.Option("--agent", help="Agent spec; defaults to $HARNESS_AGENT."),
    ] = None,
    timeout: Annotated[
        float, typer.Option("--timeout", min=10, help="Hard kill limit in seconds.")
    ] = 300.0,
    workdir: Annotated[
        Path, typer.Option("--workdir", file_okay=False, help="Working directory.")
    ] = Path("."),
) -> None:
    """Dispatch one isolated Pi sub-agent and print its report.

    The sub-agent shares only the model gateway and the filesystem: no
    HARNESS_* state, no flag submission, no nested dispatch. Treat its text
    as unverified input.
    """
    from .models import load_agent
    from .pi_host import run_subagent

    agent_path = agent or Path(os.environ.get("HARNESS_AGENT", "agents/hosted/pi-deepseek.yaml"))
    spec = load_agent(agent_path)
    result = run_subagent(spec, objective, timeout=timeout, cwd=workdir)
    if result.text:
        typer.echo(result.text)
    raise typer.Exit(code=0 if result.returncode == 0 and not result.timed_out else 1)


@app.command("hint")
def hint_command(
    run_dir: Annotated[Path, typer.Argument(file_okay=False)],
    text: Annotated[
        str,
        typer.Option(
            "--text",
            "-t",
            help="Hint text; pass '-' (default) to read multi-line input from stdin.",
        ),
    ] = "-",
) -> None:
    """Append an operator hint to a run's hint inbox.

    Hints are advisory guidance: the running Agent absorbs them on its next
    turn and they are recorded in World state, but they never verify the
    objective by themselves.
    """
    from .hints import append_hint

    if run_dir.is_symlink() or not run_dir.is_dir():
        raise typer.BadParameter(f"not a run directory: {run_dir}")
    if text == "-":
        text = sys.stdin.read()
    if not text.strip():
        raise typer.BadParameter("hint text is empty")
    try:
        record = append_hint(run_dir, text)
    except OSError as exc:
        # e.g. hints.jsonl is a symlink; O_NOFOLLOW refuses to follow it.
        raise typer.BadParameter(f"cannot write hint inbox: {exc}") from exc
    typer.echo(
        json.dumps(
            {"id": record.id, "created_at": record.created_at, "file": "hints.jsonl"}
        )
    )


@app.command("audit-run")
def audit_run_command(
    run_dir: Annotated[Path, typer.Argument(file_okay=False)],
) -> None:
    """Audit saved run artifacts without changing them."""
    from .audit import audit_run

    report = audit_run(run_dir)
    typer.echo(report.model_dump_json(indent=2))
    raise typer.Exit(
        code={
            "consistent": 0,
            "contradictory": 1,
            "incomplete": 2,
        }[report.status]
    )


def _gateway_config(
    *,
    enabled: bool,
    policy: Path | None,
    model_upstream: str | None,
    model_api_key_env: str,
    input_price: float,
    output_price: float,
    mode: str,
    sidecar_image: str | None,
    sidecar_runtime: str | None,
) -> GatewayConfig | None:
    if not enabled:
        return None
    if mode not in {"host", "sidecar"}:
        raise typer.BadParameter("--gateway-mode must be host or sidecar")
    if mode == "sidecar" and not sidecar_image:
        raise typer.BadParameter("--gateway-image is required for sidecar mode")
    return GatewayConfig(
        policy_path=policy.resolve() if policy else None,
        model_upstream=model_upstream,
        model_api_key=os.environ.get(model_api_key_env),
        input_price_per_million_usd=input_price,
        output_price_per_million_usd=output_price,
        mode=mode,
        sidecar_image=sidecar_image,
        sidecar_runtime=sidecar_runtime,
    )


@app.command()
def validate(
    task: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Validate a task contract."""
    try:
        spec = load_task(task)
    except (ValidationError, ValueError, TypeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"valid: {spec.id} ({spec.api_version})")


@app.command()
def preflight(
    task: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    agent: Annotated[Path, typer.Option("--agent", exists=True, dir_okay=False)],
    tsec: Annotated[bool, typer.Option("--tsec")] = False,
    base_url_env: Annotated[str, typer.Option("--base-url-env")] = "BENCHMARK_BASE_URL",
    benchmark_token_env: Annotated[
        str, typer.Option("--benchmark-token-env")
    ] = "BENCHMARK_TOKEN",
    gateway_enabled: Annotated[bool, typer.Option("--gateway")] = False,
    gateway_mode: Annotated[str, typer.Option("--gateway-mode")] = "host",
    gateway_image: Annotated[str | None, typer.Option("--gateway-image")] = None,
    model_upstream: Annotated[str | None, typer.Option("--model-upstream")] = None,
) -> None:
    """Check local run prerequisites without fetching images or contacting services."""
    try:
        report = run_preflight(
            load_task(task),
            load_agent(agent),
            task_path=task,
            require_tsec=tsec,
            tsec_base_url_env=base_url_env,
            tsec_token_env=benchmark_token_env,
            gateway_enabled=gateway_enabled,
            gateway_mode=gateway_mode,
            gateway_image=gateway_image,
            model_upstream=model_upstream,
        )
    except (ValidationError, ValueError, TypeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(report.model_dump_json(indent=2))
    if not report.ready:
        raise typer.Exit(code=1)


@app.command()
def run(
    task: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    agent: Annotated[Path, typer.Option("--agent", exists=True, dir_okay=False)],
    runs_root: Annotated[Path, typer.Option("--runs-root")] = Path(".harness/runs"),
    repeat: Annotated[int, typer.Option("--repeat", min=1)] = 1,
    seed: Annotated[int, typer.Option("--seed", min=0)] = 0,
    gateway_enabled: Annotated[bool, typer.Option("--gateway")] = False,
    gateway_policy: Annotated[
        Path | None,
        typer.Option("--gateway-policy", exists=True, dir_okay=False),
    ] = None,
    gateway_mode: Annotated[str, typer.Option("--gateway-mode")] = "host",
    gateway_image: Annotated[str | None, typer.Option("--gateway-image")] = None,
    gateway_runtime: Annotated[str | None, typer.Option("--gateway-runtime")] = None,
    model_upstream: Annotated[str | None, typer.Option("--model-upstream")] = None,
    model_api_key_env: Annotated[str, typer.Option("--model-api-key-env")] = (
        "HARNESS_MODEL_API_KEY"
    ),
    input_price: Annotated[float, typer.Option("--input-price-per-million", min=0)] = 0.0,
    output_price: Annotated[float, typer.Option("--output-price-per-million", min=0)] = 0.0,
    resume_world: Annotated[
        Path | None,
        typer.Option("--resume-world", exists=True, dir_okay=False),
    ] = None,
    resume_run: Annotated[
        Path | None,
        typer.Option("--resume-run", exists=True, file_okay=False),
    ] = None,
    allow_host_agent: Annotated[
        bool,
        typer.Option(
            "--allow-host-agent",
            help="Allow trusted CLI agent command to execute on the host.",
        ),
    ] = False,
    context_token_budget: Annotated[
        int | None,
        typer.Option("--context-token-budget", min=1, help="Bound world.context.txt assembly by an offline token estimate."),
    ] = None,
    knowledge_root: Annotated[
        list[Path] | None,
        typer.Option(
            "--knowledge-root",
            help="Read-only markdown knowledge root; repeat for more. Overrides the agent YAML.",
        ),
    ] = None,
) -> None:
    """Run one task with one agent."""
    if resume_run is not None and (resume_world is not None or repeat != 1):
        raise typer.BadParameter("--resume-run cannot be combined with --resume-world or --repeat")
    task_spec = load_task(task)
    agent_spec = load_agent(agent)
    if knowledge_root:
        agent_spec = agent_spec.model_copy(
            update={"knowledge": [str(path) for path in knowledge_root]}
        )
    gateway_config = _gateway_config(
        enabled=gateway_enabled,
        policy=gateway_policy,
        model_upstream=model_upstream,
        model_api_key_env=model_api_key_env,
        input_price=input_price,
        output_price=output_price,
        mode=gateway_mode,
        sidecar_image=gateway_image,
        sidecar_runtime=gateway_runtime,
    )
    orchestrator = Orchestrator(runs_root)
    results = [
        orchestrator.run(
            task=task_spec,
            task_path=task,
            agent=agent_spec,
            agent_path=agent,
            allow_host_agent=allow_host_agent,
            seed=seed + index,
            gateway_config=gateway_config,
            resume_world_events=resume_world,
            resume_run_dir=resume_run,
            context_token_budget=context_token_budget,
        )
        for index in range(repeat)
    ]
    if repeat == 1:
        output: dict | list = results[0]
    else:
        output = {
            "attempts": repeat,
            "successes": sum(1 for item in results if item["success"]),
            "success_rate": sum(1 for item in results if item["success"]) / repeat,
            "mean_score": sum(float(item["score"]) for item in results) / repeat,
            "runs": results,
        }
    typer.echo(json.dumps(output, ensure_ascii=False, indent=2))
    if any(item["status"] == "error" for item in results):
        raise typer.Exit(code=1)


@app.command("tsec")
def tsec(
    agent: Annotated[Path, typer.Option("--agent", exists=True, dir_okay=False)],
    challenge: Annotated[str | None, typer.Option("--challenge")] = None,
    run_all: Annotated[bool, typer.Option("--all")] = False,
    runs_root: Annotated[Path, typer.Option("--runs-root")] = Path(".harness/tsec-runs"),
    parallel: Annotated[
        int | None,
        typer.Option(
            "--parallel",
            min=1,
            max=MAX_PARALLEL_CHALLENGES,
            help="Alias for --concurrency; the platform caps instances at 3.",
        ),
    ] = None,
    knowledge_root: Annotated[
        list[Path] | None,
        typer.Option(
            "--knowledge-root",
            help="Read-only markdown knowledge root; repeat for more. Overrides the agent YAML.",
        ),
    ] = None,
    base_url_env: Annotated[str, typer.Option("--base-url-env")] = "BENCHMARK_BASE_URL",
    token_env: Annotated[str, typer.Option("--benchmark-token-env")] = "BENCHMARK_TOKEN",
    seed: Annotated[int, typer.Option("--seed", min=0)] = 0,
    wall_time: Annotated[
        int,
        typer.Option(
            "--wall-time",
            min=1,
            help=(
                "Soft wall-clock bound per challenge set, in seconds. Defaults to "
                "the managed evaluation platform's eight-hour task window; pass a "
                "smaller value for local practice runs."
            ),
        ),
    ] = MAX_EVALUATION_WALL_TIME_SECONDS,
    max_tokens: Annotated[int | None, typer.Option("--max-tokens", min=1)] = None,
    max_model_calls: Annotated[int | None, typer.Option("--max-model-calls", min=1)] = None,
    max_tool_calls: Annotated[int | None, typer.Option("--max-tool-calls", min=1)] = None,
    max_cost_usd: Annotated[float | None, typer.Option("--max-cost-usd", min=0)] = None,
    use_hint: Annotated[
        bool,
        typer.Option("--hint", help="Request the platform hint; this may reduce flag score."),
    ] = False,
    hint_threshold: Annotated[
        int,
        typer.Option(
            "--hint-threshold",
            min=0,
            help=(
                "Fetch the platform hint once when consecutive no-progress outcomes "
                "reach N; 0 keeps hints off unless --hint is passed."
            ),
        ),
    ] = 0,
    start_retries: Annotated[int, typer.Option("--start-retries", min=0, max=10)] = 2,
    retry_delay: Annotated[float, typer.Option("--retry-delay", min=0)] = 2.0,
    allow_host_agent: Annotated[
        bool,
        typer.Option(
            "--allow-host-agent",
            help="Allow a trusted CLI agent to execute on the host.",
        ),
    ] = False,
    max_unconfirmed_progress: Annotated[
        int, typer.Option("--max-unconfirmed-progress", min=1)
    ] = BENCHMARK_MAX_UNCONFIRMED_PROGRESS,
    concurrency: Annotated[
        int,
        typer.Option(
            "--concurrency",
            min=1,
            max=3,
            help="Solve up to this many challenges at once (platform cap: 3).",
        ),
    ] = 1,
    total_wall_time: Annotated[
        int | None,
        typer.Option(
            "--total-wall-time",
            min=1,
            help=(
                "Global soft window for the whole set, in seconds. New cases stop "
                "being dispatched near the end so the finalize grace fits."
            ),
        ),
    ] = None,
    max_passes: Annotated[
        int,
        typer.Option(
            "--max-passes",
            min=1,
            max=5,
            help="Retry still-incomplete challenges this many passes over the set.",
        ),
    ] = 1,
    max_wall_time_seconds: Annotated[
        int | None,
        typer.Option(
            "--max-wall-time-seconds",
            min=1,
            help="Optional operator cap on the declared wall clock.",
        ),
    ] = None,
    seed_notes_from: Annotated[
        Path | None,
        typer.Option(
            "--seed-notes-from",
            exists=True,
            file_okay=False,
            dir_okay=True,
            help="Previous run directory whose summary seeds this run's notes.",
        ),
    ] = None,
    preflight: Annotated[
        bool,
        typer.Option(
            "--preflight/--no-preflight",
            help=(
                "Check the benchmark VPN and control plane before selecting cases. "
                "A platform-wide failure exits 3 without creating run directories."
            ),
        ),
    ] = True,
    resume: Annotated[
        bool,
        typer.Option(
            "--resume/--rerun",
            help=(
                "Skip cases that already have a completed local run and seed the "
                "remaining attempts from their last Agent report."
            ),
        ),
    ] = True,
    summary_out: Annotated[
        Path | None,
        typer.Option(
            "--summary-out",
            dir_okay=False,
            help=(
                "Sweep summary JSON path. Defaults to "
                "<runs-root>/sweep-summary-<UTC>.json."
            ),
        ),
    ] = None,
) -> None:
    """Run TSec Benchmark challenges through the generic benchmark runtime."""
    spec = load_agent(agent)
    if knowledge_root:
        spec = spec.model_copy(update={"knowledge": [str(path) for path in knowledge_root]})
    config = load_tsec_config(base_url_env=base_url_env, token_env=token_env)
    if any(
        value is not None
        for value in (max_tokens, max_model_calls, max_tool_calls, max_cost_usd)
    ):
        typer.echo(
            "warning: token, model-call, tool-call and cost budgets are no longer "
            "enforced; only --wall-time bounds a run, and it is a soft deadline "
            "followed by a finalize grace window",
            err=True,
        )
    budgets = BudgetSpec(
        wall_time=wall_time,
        max_tokens=max_tokens,
        max_model_calls=max_model_calls,
        max_tool_calls=max_tool_calls,
        max_cost_usd=max_cost_usd,
    )
    summary_path = summary_out or (
        runs_root / f"sweep-summary-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    try:
        results = asyncio.run(
            run_tsec_challenges(
                config=config,
                agent=spec,
                budgets=budgets,
                runs_root=runs_root,
                challenge_code=challenge,
                run_all=run_all,
                seed=seed,
                use_hint=use_hint,
                hint_threshold=hint_threshold,
                start_retries=start_retries,
                retry_delay=retry_delay,
                allow_host_agent=allow_host_agent,
                max_unconfirmed_progress=max_unconfirmed_progress,
                parallel=parallel,
                concurrency=concurrency,
                total_wall_time=total_wall_time,
                max_passes=max_passes,
                max_wall_time_seconds=max_wall_time_seconds,
                seed_notes_from=seed_notes_from,
                preflight=preflight,
                resume=resume,
                summary_out=summary_path,
            )
        )
    except TSecPlatformError as exc:
        typer.echo(
            f"benchmark platform is not usable ({exc.kind}): {exc.detail}\n"
            f"partial sweep summary: {summary_path}\n"
            "fix the platform or VPN state; do not retry this sweep in a loop.",
            err=True,
        )
        raise typer.Exit(code=3) from exc
    typer.echo(json.dumps(results, ensure_ascii=False, indent=2))
    failed = _sweep_failure_count(summary_path)
    if failed:
        typer.echo(
            f"{failed} case(s) did not produce a run; see {summary_path}", err=True
        )
        raise typer.Exit(code=4)


def _sweep_failure_count(summary_path: Path) -> int:
    """Failures recorded in a sweep summary, or ``0`` when there is nothing to read."""
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    failures = payload.get("failures")
    return len(failures) if isinstance(failures, list) else 0


@app.command("gc-runs")
def gc_runs(
    root: Annotated[Path, typer.Option("--root", file_okay=False)] = Path(
        ".harness/tsec-runs"
    ),
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Delete the reported directories. Without it this is a dry run.",
        ),
    ] = False,
) -> None:
    """Report (or delete) run directories that hold no artifact at all."""
    import shutil

    empty = find_empty_run_dirs(root)
    for path in empty:
        typer.echo(str(path))
    typer.echo(f"{len(empty)} empty run directory(ies) under {root}", err=True)
    if not apply:
        typer.echo("dry run: pass --apply to delete them", err=True)
        return
    removed = 0
    for path in empty:
        try:
            shutil.rmtree(path)
        except OSError as exc:
            typer.echo(f"could not remove {path}: {exc.strerror}", err=True)
            continue
        removed += 1
    typer.echo(f"removed {removed} empty run directory(ies)", err=True)


@app.command("capabilities")
def capabilities() -> None:
    """Show locally available execution backends."""
    from .execution import ExecutionCapabilities

    typer.echo(json.dumps(ExecutionCapabilities.detect().as_dict(), indent=2))


@app.command("gateway")
def gateway(
    event_file: Annotated[Path, typer.Option("--event-file")] = Path(
        ".harness/gateway/events.jsonl"
    ),
    trusted_event_file: Annotated[Path | None, typer.Option("--trusted-event-file")] = None,
    workspace: Annotated[Path, typer.Option("--workspace", file_okay=False)] = Path("."),
    task_dir: Annotated[Path, typer.Option("--task-dir", file_okay=False)] = Path("."),
    policy: Annotated[Path | None, typer.Option("--policy", dir_okay=False)] = None,
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8765,
    model_upstream: Annotated[str | None, typer.Option("--model-upstream")] = None,
    gateway_token_env: Annotated[str, typer.Option("--gateway-token-env")] = (
        "HARNESS_GATEWAY_TOKEN"
    ),
    model_api_key_env: Annotated[str, typer.Option("--model-api-key-env")] = (
        "HARNESS_MODEL_API_KEY"
    ),
    input_price: Annotated[float, typer.Option("--input-price-per-million", min=0)] = 0.0,
    output_price: Annotated[float, typer.Option("--output-price-per-million", min=0)] = 0.0,
) -> None:
    """Serve the policy-gated Tool Gateway and OpenAI-compatible model proxy."""
    import uvicorn

    from .gateway import ModelPricing, create_gateway_app
    from .policy import load_policy

    gateway_token = os.environ.get(gateway_token_env)
    if not gateway_token:
        typer.echo(
            f"missing gateway token in environment variable {gateway_token_env}",
            err=True,
        )
        raise typer.Exit(code=2)

    app_instance = create_gateway_app(
        event_file=event_file.resolve(),
        trusted_event_file=trusted_event_file.resolve() if trusted_event_file else None,
        workspace=workspace.resolve(),
        task_dir=task_dir.resolve(),
        policy=load_policy(policy),
        gateway_token=gateway_token,
        model_upstream=model_upstream,
        model_api_key=os.environ.get(model_api_key_env),
        model_pricing=ModelPricing(
            input_per_million_usd=input_price,
            output_per_million_usd=output_price,
        ),
    )
    uvicorn.run(app_instance, host=host, port=port, access_log=False)


@app.command()
def report(
    run_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
) -> None:
    """Print a saved result bundle."""
    result_path = run_dir / "result.json"
    if not result_path.is_file():
        typer.echo(f"missing {result_path}", err=True)
        raise typer.Exit(code=2)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    attribution = result.get("executor_summary")
    if isinstance(attribution, list) and attribution:
        typer.echo("executor attribution (prompt / provider / model):")
        for entry in attribution:
            if not isinstance(entry, dict):
                continue
            typer.echo(
                "- {prompt} / {provider} / {model}: sessions={sessions} "
                "submissions={submissions} completed={completed_sessions} "
                "tokens={total_tokens}".format(
                    prompt=entry.get("prompt") or "-",
                    provider=entry.get("provider") or "-",
                    model=entry.get("model") or "-",
                    sessions=entry.get("sessions", 0),
                    submissions=entry.get("submissions", 0),
                    completed_sessions=entry.get("completed_sessions", 0),
                    total_tokens=entry.get("total_tokens", 0),
                )
            )
    metrics = result.get("metrics")
    if isinstance(metrics, dict):
        typer.echo(_format_metrics(metrics))
    typer.echo(result_path.read_text(encoding="utf-8"))


def _format_metrics(metrics: dict) -> str:
    """Render the loop-health block a failing run is judged by.

    Written for the questions the 2026-10-16 sweep review had to answer with
    hand-written jq: did the run ever submit, how much of the wall clock did
    tools hold, how much did the Harness talk at the Agent, and how large was
    the final prompt.
    """
    attempted = metrics.get("submissions_attempted")
    first_ms = metrics.get("first_submission_ms")
    fraction = metrics.get("submitted_at_fraction")
    if first_ms is None:
        submitted = "submitted: never"
    else:
        submitted = f"submitted: first at {first_ms} ms"
        if isinstance(fraction, (int, float)):
            submitted += f" ({fraction:.2f} of the run)"
    steers = metrics.get("steer_counts")
    steer_text = (
        ", ".join(f"{key}={value}" for key, value in sorted(steers.items()))
        if isinstance(steers, dict) and steers
        else "none recorded"
    )
    lines = [
        "run metrics:",
        f"- candidates attempted: {attempted if attempted is not None else 'unknown'}",
        f"- {submitted}",
        f"- steers delivered: {steer_text}",
        "- tool time: {tool} ms over {calls} calls ({failures} failed)".format(
            tool=metrics.get("tool_time_ms", 0),
            calls=metrics.get("tool_calls", 0),
            failures=metrics.get("tool_failures", 0),
        ),
        f"- final prompt size: {metrics.get('context_tokens_final', 0)} tokens",
    ]
    slow = metrics.get("slow_tool_calls")
    if isinstance(slow, list) and slow:
        lines.append("- slowest calls:")
        for entry in slow:
            if not isinstance(entry, dict):
                continue
            lines.append(
                "  - {tool} {duration} ms".format(
                    tool=entry.get("tool") or "?",
                    duration=entry.get("duration_ms", 0),
                )
            )
    return "\n".join(lines)


if __name__ == "__main__":
    app()
