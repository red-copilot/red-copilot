"""The Kali image is the Agent's tool surface; it must not need solve-time installs."""
from __future__ import annotations

import re
from pathlib import Path

DOCKERFILE = Path("docker/pi-kali/Dockerfile")

# The 2026-10-16 sweep reached for these and found them missing: `unzip`/`xxd`/
# `php`/`traceroute` in the session transcripts, and the Python stack could not be
# installed at solve time because `pip install` cannot run under the offline
# profiles.
ADDED_TOOLS = (
    "unzip",
    "xxd",
    "php-cli",
    "traceroute",
    "iptables",
    "python3-requests",
    "python3-bs4",
    "python3-lxml",
    "python3-pil",
    "python3-pycryptodome",
    "python3-paramiko",
)

# Agent-facing material must never tell the Agent to install anything at solve
# time: the offline profiles have no package network.
AGENT_MATERIAL_ROOTS = ("prompts", "skills", "knowledge", "agents")
INSTALL_COMMAND = re.compile(r"\b(?:apt-get|apt)\s+install\b|\bpip\s+install\b|\bnpm\s+install\b")


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _instructions(text: str) -> str:
    """The Dockerfile without comment lines: only real build commands."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def test_pi_kali_image_contains_required_base_and_tools() -> None:
    dockerfile = _dockerfile()
    assert "kalilinux/kali-last-release@sha256:" in dockerfile
    assert "kali-linux-core" in dockerfile
    assert "@earendil-works/pi-coding-agent" in dockerfile
    assert "ARG PI_VERSION=1.0.4" in dockerfile
    assert "kali-last-snapshot" in dockerfile
    assert "s/kali-rolling/kali-last-snapshot/g" in dockerfile
    assert dockerfile.index("kali-last-snapshot") < dockerfile.index("apt-get update")
    assert "apt-get update" in dockerfile
    assert "nmap" in dockerfile
    assert "sqlmap" in dockerfile
    assert "gdb" in dockerfile


def test_pi_kali_image_installs_the_tools_the_sweep_reached_for() -> None:
    dockerfile = _dockerfile()
    block = dockerfile[
        dockerfile.index("apt-get install") : dockerfile.index('node -e')
    ]
    missing = [tool for tool in ADDED_TOOLS if tool not in block]
    assert not missing, f"tools outside the install block: {missing}"


def test_pi_kali_image_strips_the_nmap_file_capabilities() -> None:
    dockerfile = _dockerfile()
    # Kali ships the nmap binary with file capabilities; under `--cap-drop ALL`
    # plus `no-new-privileges` that exec fails with EPERM (28 sweep results).
    assert "setcap -r /usr/lib/nmap/nmap" in dockerfile
    assert 'test -z "$(getcap /usr/lib/nmap/nmap)"' in dockerfile


def test_pi_kali_image_installs_nothing_at_solve_time() -> None:
    dockerfile = _dockerfile()
    instructions = _instructions(dockerfile)
    # One apt layer and one npm install, both inside the build layer...
    assert instructions.count("apt-get install") == 1
    assert instructions.count("npm install") == 1
    # ...no pip at all: the Python stack comes from the distribution...
    assert "pip install" not in instructions
    # ...and nothing installs after the same layer cleans up.
    tail = instructions[instructions.index("apt-get clean") :]
    assert INSTALL_COMMAND.findall(tail) == []
    # The image's own command is inert; the harness supplies the entrypoint.
    assert 'CMD ["pi", "--version"]' in dockerfile


def test_agent_material_never_instructs_a_runtime_install() -> None:
    offenders: list[str] = []
    for root in AGENT_MATERIAL_ROOTS:
        for path in sorted(Path(root).rglob("*")):
            if not path.is_file() or path.suffix not in {".md", ".yaml", ".yml"}:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if INSTALL_COMMAND.search(line):
                    offenders.append(f"{path}:{number}: {line.strip()}")
    assert not offenders, "agent material asks for a solve-time install:\n" + "\n".join(offenders)
