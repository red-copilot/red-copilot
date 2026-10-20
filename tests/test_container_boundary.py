from __future__ import annotations

import json
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from harness.agent import build_container_command
from harness.agent_workspace import create_agent_workspace
from harness.models import AgentSpec, load_task


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker is unavailable")
def test_pi_container_cannot_reach_harness_state_secrets_or_host_network(
    monkeypatch, tmp_path: Path
) -> None:
    image = "harness-isolation-probe:ci"
    if subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, check=False
    ).returncode:
        pytest.skip(f"required test image is not built: {image}")

    monkeypatch.setenv("HARNESS_TEST_SECRET", "host-secret-canary")
    monkeypatch.setenv("HARNESS_CONTROL_TOKEN", "control-plane-secret-canary")
    docker_config = tmp_path / "docker-config"
    docker_config.mkdir()
    (docker_config / "config.json").write_text(
        json.dumps(
            {
                "proxies": {
                    "default": {
                        "httpProxy": "http://proxy-user:proxy-secret@127.0.0.1:3128",
                        "httpsProxy": "http://proxy-user:proxy-secret@127.0.0.1:3128",
                        "allProxy": "socks5://proxy-user:proxy-secret@127.0.0.1:1080",
                        "noProxy": "internal-secret.example",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    run_dir = tmp_path / "runs" / "run_isolation_probe"
    run_dir.mkdir(parents=True)
    workspace = create_agent_workspace(run_dir)
    protected_world_db = run_dir / "world.db"
    protected_result = run_dir / "result.json"
    protected_world_db.write_bytes(b"authoritative-world")
    protected_result.write_text('{"trusted": true}\n', encoding="utf-8")
    before_world = protected_world_db.read_bytes()
    before_result = protected_result.read_bytes()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    probe = (
        "import json, os, socket\n"
        "from pathlib import Path\n"
        f"world = Path({str(protected_world_db)!r})\n"
        f"result = Path({str(protected_result)!r})\n"
        "try: world.read_bytes(); can_read_world = True\n"
        "except OSError: can_read_world = False\n"
        "try: world.write_bytes(b'corrupted'); can_write_world = True\n"
        "except OSError: can_write_world = False\n"
        "try: result.write_bytes(b'forged'); can_write_result = True\n"
        "except OSError: can_write_result = False\n"
        "try: open('/etc/harness-escape', 'w').write('escaped'); root_writable = True\n"
        "except OSError: root_writable = False\n"
        "try:\n"
        f" s = socket.create_connection(('127.0.0.1', {port}), timeout=0.3); s.close(); network_open = True\n"
        "except OSError: network_open = False\n"
        "checks = [not can_read_world, not can_write_world, not can_write_result, "
        "not root_writable, not network_open, "
        "'HARNESS_TEST_SECRET' not in os.environ, "
        "'HARNESS_CONTROL_TOKEN' not in os.environ, "
        "not any(os.environ.get(name) for name in ('HTTP_PROXY','HTTPS_PROXY','NO_PROXY','FTP_PROXY','ALL_PROXY','http_proxy','https_proxy','no_proxy','ftp_proxy','all_proxy')), "
        "not any('proxy-secret' in value for name,value in os.environ.items() if 'proxy' in name.lower()), "
        "not Path('/task/verifier.py').exists()]\n"
        "Path(os.environ['HARNESS_RUN_DIR'], 'proof.txt').write_text("
        "'red-harness-ok' if all(checks) else json.dumps(checks), encoding='utf-8')\n"
    )
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))
    spec = AgentSpec.model_validate(
        {
            "apiVersion": "harness/v1",
            "id": "isolation-probe",
            "image": image,
            "network": "none",
            "network_profile": "fully-offline",
            "pi": {"model": "fake"},
        }
    )
    command = build_container_command(
        spec=spec,
        task=task,
        task_dir=Path("benchmarks/examples/hello").resolve(),
        run_dir=workspace,
        container_name="harness_isolation_probe",
        network="none",
        environment_project=None,
        seed=1,
        gateway_url=None,
        gateway_token=None,
        host_gateway=False,
        image_command=[image, "python", "-c", probe],
        workdir="/run/harness",
    )
    try:
        created = subprocess.run(command, capture_output=True, text=True, check=False)
        assert created.returncode == 0, created.stderr
        started = subprocess.run(
            ["docker", "start", "-a", "harness_isolation_probe"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert started.returncode == 0, started.stderr
    finally:
        listener.close()
        subprocess.run(
            ["docker", "rm", "-f", "harness_isolation_probe"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    assert (workspace / "proof.txt").read_text(encoding="utf-8") == "red-harness-ok"
    assert protected_world_db.read_bytes() == before_world
    assert protected_result.read_bytes() == before_result
    assert not (run_dir / "agent-task-input" / "verifier.py").exists()
