from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from .benchmark.base import BENCHMARK_MAX_REPLANS, BENCHMARK_MAX_UNCONFIRMED_PROGRESS
from .benchmark.tsec_adapter import run_tsec_challenges
from .benchmark.tsec_client import load_tsec_config
from .gateway_runtime import GatewayConfig
from .models import BudgetSpec, load_agent, load_task
from .orchestrator import Orchestrator
from .preflight import run_preflight
from .runtime.solver_profile import DEFAULT_SOLVER_PROFILE, SolverProfile
from .skills import load_skill

app = typer.Typer(
    name="harness",
    help="Reproducible evaluation runtime for security agents.",
    no_args_is_help=True,
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


@app.command("validate-skill")
def validate_skill(
    skill: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Validate a skill metadata contract."""
    try:
        spec = load_skill(skill)
    except (ValidationError, ValueError, TypeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"valid skill: {spec.id} ({spec.domain})")


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
    solver_profile: Annotated[SolverProfile, typer.Option("--solver-profile")] = (
        DEFAULT_SOLVER_PROFILE
    ),
    context_token_budget: Annotated[
        int | None,
        typer.Option("--context-token-budget", min=1, help="Bound world.context.txt assembly by an offline token estimate."),
    ] = None,
) -> None:
    """Run one task with one agent."""
    if resume_run is not None and (resume_world is not None or repeat != 1):
        raise typer.BadParameter("--resume-run cannot be combined with --resume-world or --repeat")
    task_spec = load_task(task)
    agent_spec = load_agent(agent)
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
            solver_profile=solver_profile,
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
    skills_root: Annotated[Path, typer.Option("--skills-root")] = Path("skills"),
    base_url_env: Annotated[str, typer.Option("--base-url-env")] = "BENCHMARK_BASE_URL",
    token_env: Annotated[str, typer.Option("--benchmark-token-env")] = "BENCHMARK_TOKEN",
    seed: Annotated[int, typer.Option("--seed", min=0)] = 0,
    wall_time: Annotated[int, typer.Option("--wall-time", min=1)] = 3600,
    max_tokens: Annotated[int | None, typer.Option("--max-tokens", min=1)] = None,
    max_model_calls: Annotated[int | None, typer.Option("--max-model-calls", min=1)] = None,
    max_tool_calls: Annotated[int | None, typer.Option("--max-tool-calls", min=1)] = None,
    max_cost_usd: Annotated[float | None, typer.Option("--max-cost-usd", min=0)] = None,
    use_hint: Annotated[
        bool,
        typer.Option("--hint", help="Request the platform hint; this may reduce flag score."),
    ] = False,
    start_retries: Annotated[int, typer.Option("--start-retries", min=0, max=10)] = 2,
    retry_delay: Annotated[float, typer.Option("--retry-delay", min=0)] = 2.0,
    allow_host_agent: Annotated[
        bool,
        typer.Option(
            "--allow-host-agent",
            help="Allow a trusted CLI agent to execute on the host.",
        ),
    ] = False,
    solver_profile: Annotated[SolverProfile, typer.Option("--solver-profile")] = (
        DEFAULT_SOLVER_PROFILE
    ),
    max_unconfirmed_progress: Annotated[
        int, typer.Option("--max-unconfirmed-progress", min=1)
    ] = BENCHMARK_MAX_UNCONFIRMED_PROGRESS,
    max_replans: Annotated[int, typer.Option("--max-replans", min=1)] = BENCHMARK_MAX_REPLANS,
) -> None:
    """Run TSec Benchmark challenges through the generic benchmark runtime."""
    spec = load_agent(agent)
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
    results = asyncio.run(
        run_tsec_challenges(
            config=config,
            agent=spec,
            budgets=budgets,
            runs_root=runs_root,
            skills_root=skills_root,
            challenge_code=challenge,
            run_all=run_all,
            seed=seed,
            use_hint=use_hint,
            start_retries=start_retries,
            retry_delay=retry_delay,
            allow_host_agent=allow_host_agent,
            solver_profile=solver_profile,
            max_unconfirmed_progress=max_unconfirmed_progress,
            max_replans=max_replans,
        )
    )
    typer.echo(json.dumps(results, ensure_ascii=False, indent=2))


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
    typer.echo(result_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    app()
