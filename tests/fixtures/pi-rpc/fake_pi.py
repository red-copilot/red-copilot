#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def emit(payload: dict) -> None:
    print(json.dumps(payload, separators=(",", ":")), flush=True)


def settle() -> None:
    run_dir = Path(os.environ["HARNESS_RUN_DIR"])
    (run_dir / "proof.txt").write_text("red-harness-ok", encoding="utf-8")
    cwd_marker = Path("/workspace/.fake-pi-touched")
    if cwd_marker.parent.is_dir():
        cwd_marker.write_text("subagent-ok\n", encoding="utf-8")
    emit({"type": "message_start", "message": {"role": "assistant"}})
    emit(
        {
            "type": "tool_execution_start",
            "toolCallId": "fake-tool-1",
            "toolName": "write",
            "args": {"path": "proof.txt"},
        }
    )
    emit(
        {
            "type": "tool_execution_end",
            "toolCallId": "fake-tool-1",
            "toolName": "write",
            "isError": False,
            "result": {"structuredContent": {"exit_code": 0}},
        }
    )
    emit(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "FINDINGS: fake-pi completed"}],
                "usage": {
                    "input": 7,
                    "output": 5,
                    "totalTokens": 12,
                    "cost": {"total": 0.01},
                },
            },
        }
    )
    emit({"type": "agent_settled"})


def main() -> int:
    if "--mode" not in sys.argv or sys.argv[sys.argv.index("--mode") + 1] != "rpc":
        print("fake Pi only supports --mode rpc", file=sys.stderr)
        return 2
    emit({"type": "session", "id": "fake-session", "cwd": os.getcwd()})
    for raw in sys.stdin:
        try:
            command = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(command, dict):
            continue
        if command.get("type") in {"prompt", "steer"}:
            settle()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
