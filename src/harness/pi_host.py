from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .agent import (
    FINALIZE_GRACE_SECONDS,
    AgentError,
    AgentResult,
    AgentRunContext,
    _terminate_process,
    task_environment,
)
from .budget import BudgetMonitor
from .models import AgentSpec
from .pi_adapter import PiAdapter
from .trace import TraceRecorder

# The hosted evaluation platform has no public network: Pi must reach its
# model through the platform gateway, so the provider written into models.json
# uses this id and the baseUrl the agent spec declares.
HOSTED_PROVIDER_ID = "hosted"


class HostPiAdapter(PiAdapter):
    """Run Pi as a local process inside the runner container (no nested Docker).

    The managed evaluation mode packages the whole Harness as one image: the
    platform starts it, ``BENCHMARK_*`` variables and the model-gateway
    credential are injected, and the Agent executes directly in this container.
    Event normalization, budgets, and the finalize flow are the shared Pi
    pipeline; only the process boundary differs from ContainerPiAdapter.
    """

    def __init__(self, spec: AgentSpec, *, allow_host: bool, trace: TraceRecorder) -> None:
        if not allow_host:
            raise AgentError(
                "pi-host agents execute on the host; pass --allow-host-agent only for "
                "trusted agents"
            )
        if spec.pi is None:
            raise AgentError("pi-host agent requires a pi configuration block")
        if spec.pi.mode != "json":
            raise AgentError("pi-host agents use pi.mode: json (the RPC session is "
                             "container-based)")
        if shutil.which(spec.pi.binary) is None:
            raise AgentError(f"pi executable was not found: {spec.pi.binary}")
        self.spec = spec
        self.pi = spec.pi
        self.trace = trace
        self._tool_started: dict[str, float] = {}

    @property
    def capabilities(self):
        from .session import ONE_SHOT_AGENT_CAPABILITIES

        return ONE_SHOT_AGENT_CAPABILITIES

    def _prepare_hosted_models(self, run_dir: Path) -> Path:
        """Write Pi's models.json pointing at the platform model gateway."""
        agent_dir = run_dir / "pi-agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        if self.pi.models_base_url and self.pi.models_api_key_env:
            models = {
                "providers": {
                    HOSTED_PROVIDER_ID: {
                        "baseUrl": self.pi.models_base_url,
                        "api": "openai-completions",
                        "apiKey": f"${{{self.pi.models_api_key_env}}}",
                        "models": [{"id": self.pi.model}],
                    }
                }
            }
            (agent_dir / "models.json").write_text(
                json.dumps(models, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return agent_dir

    def _hosted_command(self, task) -> list[str]:
        args = self._pi_args(task, gateway_enabled=False)
        if self.pi.models_base_url:
            # Route --provider at the platform gateway provider written in
            # models.json. Replace in place when present; otherwise insert
            # before the json-mode prompt, which must stay the final
            # positional argument.
            if "--provider" in args:
                args[args.index("--provider") + 1] = HOSTED_PROVIDER_ID
            elif self.pi.mode == "json":
                prompt = args.pop()
                args.extend(["--provider", HOSTED_PROVIDER_ID, prompt])
            else:
                args.extend(["--provider", HOSTED_PROVIDER_ID])
        return [self.pi.binary, *args]

    def run(self, context: AgentRunContext) -> AgentResult:
        from .network_policy import enforced_network

        enforced_network(
            self.spec,
            environment_network=context.environment_network,
            gateway_network=context.gateway_network,
            gateway_enabled=context.gateway_enabled,
        )
        run_dir = context.run_dir
        event_file = run_dir / "events.jsonl"
        event_file.write_text("", encoding="utf-8")
        raw_path = run_dir / "agent.stdout.log"
        stderr_path = run_dir / "agent.stderr.log"
        raw_path.write_text("", encoding="utf-8")
        agent_dir = self._prepare_hosted_models(run_dir)

        env = os.environ.copy()
        env.update(self.spec.env)
        env.update(
            task_environment(
                context.task,
                task_dir=context.task_dir,
                run_dir=run_dir,
                environment_project=context.environment_project,
                seed=context.seed,
                network_profile=self.spec.network_profile,
            )
        )
        # Refuse to start with an empty credential rather than let every
        # model call fail downstream.
        if self.pi.models_api_key_env and not os.environ.get(self.pi.models_api_key_env):
            raise AgentError(
                f"model credential environment variable is empty: "
                f"{self.pi.models_api_key_env}"
            )
        for name in self.pi.env_passthrough:
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
        env.update(
            {
                "PI_CODING_AGENT_DIR": str(agent_dir),
                "PI_TELEMETRY": "0",
                "PI_SKIP_VERSION_CHECK": "1",
            }
        )

        command = self._hosted_command(context.task)
        self.trace.emit(
            "agent.started",
            actor="agent",
            data={
                "agent_id": self.spec.id,
                "type": "pi-host",
                "model": self.pi.model,
                "provider": HOSTED_PROVIDER_ID
                if self.pi.models_base_url
                else self.pi.provider,
                "tools": self.pi.tools,
                "network_profile": self.spec.network_profile,
            },
        )
        result = self._run_host_monitored(
            command,
            cwd=run_dir,
            env=env,
            run_dir=run_dir,
            task=context.task,
            raw_path=raw_path,
            stderr_path=stderr_path,
            event_file=event_file,
            account_model=not context.gateway_enabled,
        )
        self.trace.emit(
            "agent.finished",
            actor="agent",
            data={
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "budget_exceeded": result.budget_exceeded,
                "metrics": result.metrics.as_dict(),
                "pi_protocol": "json",
                "hosted": True,
            },
        )
        return result

    async def start_session(self, context: AgentRunContext):
        from dataclasses import replace

        from .session import OneShotAgentSession

        context = replace(
            context,
            cancel_path=context.cancel_path or context.run_dir / "agent.control.jsonl",
        )
        return await OneShotAgentSession.start(self, context=context)

    def _run_host_monitored(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        run_dir: Path,
        task,
        raw_path: Path,
        stderr_path: Path,
        event_file: Path,
        account_model: bool,
    ) -> AgentResult:
        """Run the local Pi process, translating its JSONL into harness events."""
        monitor = BudgetMonitor(event_file, task.budgets, self.trace)
        timed_out = False
        started = time.monotonic()
        offset = 0
        remainder = ""
        with raw_path.open("ab") as stdout_handle, stderr_path.open("wb") as stderr_handle:
            proc = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
            )
            while proc.poll() is None:
                offset, remainder = self._poll_output(
                    raw_path,
                    offset=offset,
                    remainder=remainder,
                    event_file=event_file,
                    account_model=account_model,
                )
                if monitor.poll():
                    _terminate_process(proc)
                    break
                if (
                    time.monotonic() - started
                    > task.budgets.wall_time + FINALIZE_GRACE_SECONDS
                ):
                    timed_out = True
                    self.trace.emit("budget.exceeded", data={"budget": "wall_time"})
                    _terminate_process(proc)
                    break
                time.sleep(0.05)
            returncode = proc.wait()

        offset, remainder = self._poll_output(
            raw_path,
            offset=offset,
            remainder=remainder,
            event_file=event_file,
            account_model=account_model,
        )
        del offset
        if remainder.strip():
            self._translate_line(
                remainder.strip(),
                event_file=event_file,
                account_model=account_model,
            )
        monitor.finish()
        return AgentResult(
            returncode=returncode,
            timed_out=timed_out,
            budget_exceeded=monitor.exceeded,
            stdout=raw_path.read_text(encoding="utf-8", errors="replace"),
            stderr=stderr_path.read_text(encoding="utf-8", errors="replace"),
            metrics=monitor.metrics,
        )
