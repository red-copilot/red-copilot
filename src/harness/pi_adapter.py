from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

from .agent import AgentError
from .models import AgentSpec, TaskSpec
from .trace import TraceRecorder

# Leading commands that say nothing about what the attempt is actually doing.
_FAMILY_NOISE_HEADS = frozenset(
    {"cd", "export", "set", "sudo", "env", "rm", "mkdir", "timeout", "sleep"}
)


def _command_family(tool: str, arguments: object) -> str | None:
    """Coarse identity of a shell command, e.g. ``bash:for`` or ``bash:curl``.

    Repeated near-identical attempts (the same loop with a different wordlist or
    path) share a family even when their exact arguments differ, which is the
    signal that a line of attack is being ground down.
    """
    if not isinstance(arguments, dict):
        return None
    command = arguments.get("command") or arguments.get("cmd")
    if not isinstance(command, str) or not command.strip():
        return None
    lines = [
        line
        for line in command.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    tokens = [
        token
        for line in lines
        for segment in re.split(r"[;&|]+", line)
        for token in segment.split()
    ]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("#"):
            index += 1  # inline comment
            continue
        if "=" in token and not token.startswith(("/", "-", ".")):
            index += 1  # leading VAR=value assignment
            continue
        head = token.strip("'\"").rsplit("/", 1)[-1]
        if head in _FAMILY_NOISE_HEADS:
            index += 2  # drop the command and its argument
            continue
        return f"{tool}:{head[:16]}"
    return None


def _tool_output_excerpt(event: object, *, limit: int = 8 * 1024) -> str | None:
    """Bounded, sanitized excerpt of a tool result for fact extraction.

    Only what an extractor needs is kept: the excerpt is truncated and scrubbed
    by the shared observability sanitizer before it reaches any artifact.
    """
    if not isinstance(event, dict):
        return None
    result = event.get("result")
    if not isinstance(result, dict):
        return None
    structured = result.get("structuredContent")
    text = structured.get("output") if isinstance(structured, dict) else None
    if not isinstance(text, str):
        content = result.get("content")
        if isinstance(content, list):
            text = "".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            )
    if not isinstance(text, str) or not text:
        return None
    excerpt = text[:limit]
    from .trace import sanitize_observability_data

    return sanitize_observability_data({"output": excerpt}).get("output")


def _tool_exit_code(event: object) -> int | None:
    if not isinstance(event, dict):
        return None
    structured = (event.get("result") or {}).get("structuredContent")
    if isinstance(structured, dict):
        code = structured.get("exit_code")
        if isinstance(code, int) and not isinstance(code, bool):
            return code
    return None


class PiAdapter:
    """Normalize a container Pi ``--mode rpc`` stream and build its argv."""

    def __init__(
        self,
        spec: AgentSpec,
        *,
        trace: TraceRecorder,
        include_solver_protocol: bool = True,
    ) -> None:
        if spec.pi is None:
            raise AgentError("Pi agent requires a pi configuration block")
        if not spec.image:
            raise AgentError("Pi agent requires a container image")
        if shutil.which("docker") is None:
            raise AgentError("docker executable was not found")
        self.spec = spec
        self.pi = spec.pi
        self.trace = trace
        self.include_solver_protocol = include_solver_protocol
        self._tool_started: dict[str, float] = {}

    def _normalize_line(
        self,
        line: str,
        *,
        account_model: bool,
    ) -> list[tuple[str, dict[str, Any]]]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            return [
                ("agent.telemetry_error", {"source": "pi", "message": str(exc), "line": line[:500]})
            ]
        if not isinstance(event, dict):
            return []

        event_type = event.get("type")
        if event_type == "session":
            return [
                (
                    "pi.session",
                    {
                        "session_id": event.get("id"),
                        "version": event.get("version"),
                        "cwd": event.get("cwd"),
                    },
                )
            ]
        if event_type == "agent_settled":
            return [("pi.agent_settled", {})]
        if event_type == "tool_execution_start":
            tool_call_id = str(event.get("toolCallId") or "")
            if tool_call_id:
                self._tool_started[tool_call_id] = time.monotonic()
            arguments = event.get("args")
            fingerprint = (
                hashlib.sha256(
                    json.dumps(arguments, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest()[:16]
                if arguments
                else None
            )
            return [
                (
                    "tool.call",
                    {
                        "tool": event.get("toolName", "unknown"),
                        "tool_call_id": event.get("toolCallId"),
                        # Bounded identity of the arguments, so repeated identical
                        # attempts are visible without storing agent text.
                        "args_fingerprint": fingerprint,
                        "args_family": _command_family(
                            str(event.get("toolName", "unknown")), arguments
                        ),
                        "source": "pi",
                    },
                )
            ]
        if event_type == "tool_execution_end":
            tool_call_id = str(event.get("toolCallId") or "")
            started = self._tool_started.pop(tool_call_id, None) if tool_call_id else None
            is_error = bool(event.get("isError", False))
            return [
                (
                    "tool.result",
                    {
                        "tool": event.get("toolName", "unknown"),
                        "tool_call_id": event.get("toolCallId"),
                        "source": "pi",
                        "is_error": is_error,
                        "duration_ms": (
                            int((time.monotonic() - started) * 1000)
                            if started is not None
                            else None
                        ),
                        "failure_class": "tool_error" if is_error else None,
                        "exit_code": _tool_exit_code(event),
                        # Bounded and sanitized; the fact extractor derives
                        # semantic observations from it without keeping raw output.
                        "output_excerpt": _tool_output_excerpt(event),
                    },
                )
            ]
        if not account_model:
            return []
        if event_type == "message_start":
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                return [
                    (
                        "model.request",
                        {
                            "source": "pi",
                            "provider": message.get("provider"),
                            "model": message.get("model", self.pi.model),
                        },
                    )
                ]
            return []
        if event_type != "message_end":
            return []
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return []
        usage = message.get("usage")
        normalized = [
            (
                "model.response",
                {
                    "source": "pi",
                    "provider": message.get("provider"),
                    "model": message.get("model", self.pi.model),
                    "stop_reason": message.get("stopReason"),
                },
            )
        ]
        if not isinstance(usage, dict):
            normalized.append(
                (
                    "model.usage_missing",
                    {
                        "source": "pi",
                        "provider": message.get("provider"),
                        "model": message.get("model", self.pi.model),
                    },
                )
            )
            return normalized
        cost = usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
        normalized.append(
            (
                "model.usage",
                {
                    "source": "pi",
                    "provider": message.get("provider"),
                    "model": message.get("model", self.pi.model),
                    # Keep raw counters intact; the budget boundary validates
                    # them and terminates on malformed or negative values.
                    "input_tokens": usage.get("input", 0),
                    "output_tokens": usage.get("output", 0),
                    "total_tokens": usage.get("totalTokens", 0),
                    "cache_read_tokens": usage.get("cacheRead", 0),
                    "cache_write_tokens": usage.get("cacheWrite", 0),
                    "reasoning_tokens": usage.get("reasoning", 0),
                    "cost_usd": cost.get("total", 0.0),
                },
            )
        )
        return normalized

    def _prepare_agent_dir(
        self,
        *,
        run_dir: Path,
        gateway_url: str | None,
        gateway_token: str | None,
    ) -> Path:
        agent_dir = run_dir / "pi-agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        if self.pi.harness_extension:
            extensions_dir = agent_dir / "extensions"
            extensions_dir.mkdir(parents=True, exist_ok=True)
            asset = (
                Path(__file__).resolve().parent
                / "pi_assets"
                / "harness-context.ts"
            )
            (extensions_dir / "harness-context.ts").write_text(
                asset.read_text(encoding="utf-8"), encoding="utf-8"
            )
        if gateway_url and gateway_token:
            models = {
                "providers": {
                    "harness": {
                        "baseUrl": f"{gateway_url}/v1",
                        "api": "openai-completions",
                        "apiKey": "$HARNESS_GATEWAY_TOKEN",
                        "models": [{"id": self.pi.model}],
                    }
                }
            }
            (agent_dir / "models.json").write_text(
                json.dumps(models, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return agent_dir

    def _pi_args(
        self,
        *,
        gateway_enabled: bool,
        session_id: str | None = None,
    ) -> list[str]:
        args = [*self.pi.launcher_args, "--mode", "rpc"]
        if session_id is not None:
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", session_id):
                raise AgentError("invalid Pi session ID in recovery checkpoint")
            args.extend(["--session", session_id])
        args.append("--approve" if self.pi.approve_project else "--no-approve")
        for enabled, flag in (
            (self.pi.context_files, "--no-context-files"),
            (self.pi.extensions or self.pi.harness_extension, "--no-extensions"),
            (self.pi.skills, "--no-skills"),
            (self.pi.prompt_templates, "--no-prompt-templates"),
            (self.pi.themes, "--no-themes"),
            (self.pi.mcp, "--no-mcp"),
        ):
            if not enabled:
                args.append(flag)
        if self.pi.offline:
            args.append("--offline")
        if self.pi.tools:
            args.extend(["--tools", ",".join(self.pi.tools)])
        else:
            args.append("--no-tools")
        if gateway_enabled:
            args.extend(["--provider", "harness", "--model", self.pi.model])
        else:
            if self.pi.provider:
                args.extend(["--provider", self.pi.provider])
            args.extend(["--model", self.pi.model])
        if self.pi.thinking:
            args.extend(["--thinking", self.pi.thinking])
        args.extend(self.pi.extra_args)
        if any(argument.startswith("@") for argument in self.pi.extra_args):
            raise AgentError("Pi RPC mode does not accept @file prompt arguments")
        return args

    def _prompt_text(self, task: TaskSpec) -> str:
        world_protocol = (
            "\n\nHarness solver protocol:\n"
            "Read $HARNESS_WORLD_CONTEXT and $HARNESS_PROGRESS_FILE before acting. "
            "Use $HARNESS_EVENT_FILE for typed solver events; do not hand-write WorldSubmission. "
            "Before a meaningful action append action.intent with data containing description, "
            "optional tool/subgoal_id/skill_id, expected_observations, success_conditions, and "
            "replan_conditions. When you learn durable semantic state append world.observe "
            "with data {type, summary, content, confidence}; use world.hypothesis, "
            "world.capability, world.artifact, or world.failure for those kinds. "
            "Harness validates typed events and writes canonical World state. "
            "Your observations and confirmed_fact fields are claims until independent evidence "
            "verifies them; a successful tool exit does not prove the objective is complete. "
            "Never write world.db, world.events.jsonl, world.snapshot.json, or progress.json. "
            "Benchmark submissions are interactive: after appending a candidate to "
            "$HARNESS_SUBMISSION_INBOX, keep the process alive and read new records from "
            "$HARNESS_FEEDBACK_FILE before deciding the next action. A rejected candidate is "
            "evidence to revise the current hypothesis or path; do not resubmit the same value. "
            "When feedback requests replanning, reread $HARNESS_WORLD_CONTEXT and "
            "$HARNESS_PROGRESS_FILE, then continue from updated state. "
            "There is no token or call budget and no hard stop; the wall-clock limit is a soft "
            "deadline. When Harness sends solver.finalize, stop probing and write your report to "
            "$HARNESS_SUMMARY_FILE: confirmed facts with their evidence, unconfirmed hypotheses, "
            "what you tried and ruled out, and unfinished intents. "
            "Treat Internet access as optional and non-essential; solve from benchmark targets "
            "and local tools first."
        )
        protocol = world_protocol if self.include_solver_protocol else ""
        return self._system_prompt() + task.objective.description + protocol

    def _system_prompt(self) -> str:
        reference = self.pi.system_prompt_file
        if not reference:
            return ""
        path = Path(reference)
        if not path.is_file():
            raise AgentError(f"Pi system prompt file was not found: {reference}")
        text = path.read_text(encoding="utf-8")
        if len(text.encode("utf-8")) > 64 * 1024:
            raise AgentError("Pi system prompt file exceeds 64 KiB")
        return text.rstrip() + "\n\n"

    def _command(
        self,
        task: TaskSpec,
        *,
        gateway_enabled: bool,
        session_id: str | None = None,
    ) -> list[str]:
        return [
            self.pi.binary,
            *self._pi_args(gateway_enabled=gateway_enabled, session_id=session_id),
        ]
