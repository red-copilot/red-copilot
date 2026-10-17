"""Documentation references must resolve to real code and fixtures.

The README, the roadmaps and the design docs are read as instructions by humans
and agents. A stale path, environment variable, CLI flag or subcommand sends
both to the wrong place, so the references are pinned to the tree here.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "harness"

DOC_PATHS = (REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").rglob("*.md")))

# Past-tense log entries name files that were deleted on purpose; those
# references cannot resolve and must not be rewritten into fiction.
HISTORICAL_PATH_REFERENCES = frozenset(
    {
        "docker/pi-kali/README.md",
    }
)

# These documents describe the pre-RUN-09 state and remain historical evidence.
HISTORICAL_CONTRACT_DOCS = frozenset(
    {
        "docs/tsec-run-review-2026-10-16.md",
        "docs/tsec-improvement-plan-2026-10-16.md",
        "docs/zc-improvement-design.md",
        "docs/zc-improvement-plan.md",
        "docs/agent-architecture.md",
    }
)

REMOVED_AGENT_CONTRACT = (
    "HARNESS_EVENT_FILE",
    "HARNESS_WORLD_INBOX",
    "HARNESS_PROGRESS_FILE",
    "action.intent",
    "--hint-threshold",
    "--max-unconfirmed-progress",
)

PATH_REFERENCE = re.compile(
    r"`((?:agents|benchmarks|docker|docs|knowledge|prompts|schemas|skills|src|tests)/"
    r"[A-Za-z0-9_./-]+)`"
)
ENVIRONMENT_VARIABLE = re.compile(r"\b((?:HARNESS|PI|BENCHMARK|TSEC)_[A-Z0-9_]+)\b")
FLAG = re.compile(r"(?<![\w-])--([a-z][a-z0-9-]{1,})")
INVOCATION = re.compile(r"^harness ([a-z][a-z0-9-]*)\b")


def _document_texts() -> dict[Path, str]:
    return {path: path.read_text(encoding="utf-8") for path in DOC_PATHS}


def _package_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(PACKAGE_ROOT.rglob("*.py"))
    )


def _cli_source() -> str:
    return (PACKAGE_ROOT / "cli.py").read_text(encoding="utf-8")


def _cli_commands() -> set[str]:
    source = _cli_source()
    named = set(re.findall(r'@app\.command\("([a-z][a-z0-9-]*)"\)', source))
    plain = set(re.findall(r"@app\.command\(\)\s*\ndef ([a-z_][a-z0-9_]*)\(", source))
    return named | {name.replace("_", "-") for name in plain}


def _invocation_lines() -> list[tuple[Path, str]]:
    """Lines that invoke the CLI, including their backslash continuations."""
    invocations: list[tuple[Path, str]] = []
    for path, text in _document_texts().items():
        collecting = False
        for raw in text.splitlines():
            stripped = re.sub(r"^\$?\s*", "", raw.strip())
            if collecting and stripped.startswith("-"):
                invocations.append((path, stripped))
                collecting = stripped.rstrip().endswith("\\")
                continue
            collecting = INVOCATION.match(stripped) is not None
            if collecting:
                invocations.append((path, stripped))
                collecting = stripped.rstrip().endswith("\\")
    return invocations


def test_documented_repo_paths_exist() -> None:
    missing: list[str] = []
    for path, text in _document_texts().items():
        for reference in PATH_REFERENCE.findall(text):
            if reference in HISTORICAL_PATH_REFERENCES:
                continue
            if not (REPO_ROOT / reference).exists():
                missing.append(f"{path.relative_to(REPO_ROOT)}: {reference}")

    assert not missing, "documentation references missing paths:\n" + "\n".join(missing)


def test_documented_environment_variables_exist_in_the_package() -> None:
    source = _package_source()
    missing: list[str] = []
    for path, text in _document_texts().items():
        for variable in ENVIRONMENT_VARIABLE.findall(text):
            if variable not in source:
                missing.append(f"{path.relative_to(REPO_ROOT)}: {variable}")

    assert not missing, "documentation references unknown environment variables:\n" + "\n".join(
        sorted(set(missing))
    )


def test_documented_cli_commands_exist() -> None:
    known = _cli_commands()
    unknown: list[str] = []
    for path, line in _invocation_lines():
        match = INVOCATION.match(line)
        if match and match.group(1) not in known:
            unknown.append(f"{path.relative_to(REPO_ROOT)}: harness {match.group(1)}")

    assert not unknown, "documentation invokes unknown subcommands:\n" + "\n".join(
        sorted(set(unknown))
    )


def test_documented_cli_flags_exist() -> None:
    source = _cli_source()
    missing: list[str] = []
    for path, line in _invocation_lines():
        for flag in FLAG.findall(line):
            if f"--{flag}" not in source:
                missing.append(f"{path.relative_to(REPO_ROOT)}: --{flag}")

    assert not missing, "documentation invokes unknown flags:\n" + "\n".join(
        sorted(set(missing))
    )


def test_active_docs_do_not_teach_removed_agent_contracts() -> None:
    stale: list[str] = []
    for path, text in _document_texts().items():
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative in HISTORICAL_CONTRACT_DOCS:
            continue
        for token in REMOVED_AGENT_CONTRACT:
            if token in text:
                stale.append(f"{relative}: {token}")

    assert not stale, "active docs teach removed Agent contracts:\n" + "\n".join(stale)


def test_runtime_roadmap_records_run_09() -> None:
    roadmap = (REPO_ROOT / "docs" / "roadmap" / "runtime.md").read_text(encoding="utf-8")

    assert "RUN-09" in roadmap
    assert "Agent-Native" in roadmap
