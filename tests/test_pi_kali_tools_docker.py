"""The Kali image must actually execute the tools the sweep reached for.

The static contract in ``tests/test_pi_kali_image.py`` proves what the image
installs; this probe proves the image *works* under the exact container settings
the harness uses, which is how the 2026-10-16 sweep lost 28 nmap calls to
``Operation not permitted``. CI does not build this image, so the probe skips
where it is absent - build it with
``docker build -t harness/pi-kali:local docker/pi-kali``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from harness.agent import ROOT_AGENT_CAPABILITIES, hardened_docker_flags

IMAGE = "harness/pi-kali:local"

# Runs inside the image with no package network and no privileges beyond the
# declared set: exactly the position the Agent is in at solve time.
PROBE = r'''
import importlib, json, shutil, socket, subprocess, sys, threading
from pathlib import Path

REQUIRED = ("unzip", "xxd", "php", "traceroute", "iptables")
# Kali ships pycryptodome under the ``Cryptodome`` namespace, not ``Crypto``.
MODULES = ("requests", "bs4", "lxml", "PIL", "Cryptodome", "paramiko")

missing = [tool for tool in REQUIRED if shutil.which(tool) is None]
for module in MODULES:
    importlib.import_module(module)

listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", 0))
listener.listen(8)
port = listener.getsockname()[1]


def serve():
    for _ in range(8):
        try:
            listener.settimeout(5.0)
            connection, _ = listener.accept()
        except OSError:
            return
        connection.close()


threading.Thread(target=serve, daemon=True).start()


def scan(flag):
    completed = subprocess.run(
        ["nmap", "-n", flag, "-p", str(port), "127.0.0.1"],
        capture_output=True,
        text=True,
        timeout=180,
    )
    tail = [line for line in completed.stdout.strip().splitlines() if line.strip()][-4:]
    return "open" in completed.stdout, tail


connect_ok, connect_tail = scan("-sT")
raw_ok, raw_tail = scan("-sS")
Path("/run/harness/probe.json").write_text(
    json.dumps(
        {
            "missing_tools": missing,
            "connect_scan": connect_ok,
            "raw_scan": raw_ok,
            "port": port,
            "raw_tail": raw_tail,
            "connect_tail": connect_tail,
        }
    )
    + "\n",
    encoding="utf-8",
)

if missing:
    print("MISSING_TOOLS", missing)
    sys.exit(3)
print("PROBE_OK")
'''.strip()


def _run(*, workspace: Path, extra_caps: list[str]) -> subprocess.CompletedProcess[str]:
    command = [
        "docker",
        "run",
        "--rm",
        "--user",
        "0:0",
        *hardened_docker_flags(
            pids_limit=512,
            memory="4g",
            cpus="4",
            tmpfs=("/tmp:rw,nosuid,nodev,size=512m",),
        ),
    ]
    for capability in (*ROOT_AGENT_CAPABILITIES, *extra_caps):
        command.extend(["--cap-add", capability])
    command.extend(
        [
            "-v",
            f"{workspace}:/run/harness:rw",
            "--entrypoint",
            "python3",
            IMAGE,
            "-c",
            PROBE,
        ]
    )
    return subprocess.run(command, capture_output=True, text=True, timeout=900, check=False)


def _require_image() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker is unavailable")
    inspect = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, check=False)
    if inspect.returncode:
        pytest.skip(f"required test image is not built: {IMAGE}")


@pytest.fixture(scope="module")
def workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("kali-workspace") / "agent-workspace"
    # The harness-owned, owner-only workspace the root container writes into.
    root.mkdir(mode=0o700)
    return root


def _probe(workspace: Path, extra_caps: list[str]) -> tuple[subprocess.CompletedProcess[str], dict]:
    result = _run(workspace=workspace, extra_caps=extra_caps)
    report = workspace / "probe.json"
    assert report.is_file(), result.stdout + result.stderr
    return result, json.loads(report.read_text(encoding="utf-8"))


def test_image_provides_the_tools_and_python_stack(workspace: Path) -> None:
    _require_image()

    result, report = _probe(workspace, extra_caps=[])

    assert result.returncode == 0, result.stdout + result.stderr
    assert report["missing_tools"] == []


def test_image_writes_a_harness_readable_artifact(workspace: Path) -> None:
    _require_image()

    _, report = _probe(workspace, extra_caps=[])

    # The root container writes into a workspace it does not own, and the
    # harness identity must still own and read the artifact back.
    assert workspace.stat().st_uid == os.getuid()
    assert isinstance(report["port"], int)


def test_image_runs_connect_and_raw_socket_scans(workspace: Path) -> None:
    _require_image()

    result, report = _probe(workspace, extra_caps=["NET_RAW"])

    assert result.returncode == 0, result.stdout + result.stderr
    assert report["connect_scan"] is True, report["connect_tail"]
    assert report["raw_scan"] is True, report["raw_tail"]


def test_raw_sockets_need_a_declared_capability(workspace: Path) -> None:
    _require_image()

    _, report = _probe(workspace, extra_caps=[])

    assert report["connect_scan"] is True, report["connect_tail"]
    assert report["raw_scan"] is False, report["raw_tail"]
