from __future__ import annotations

import secrets
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import uvicorn

from .gateway import ModelPricing, create_gateway_app
from .policy import load_policy
from .trace import TraceRecorder


class GatewayRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class GatewayConfig:
    policy_path: Path | None = None
    model_upstream: str | None = None
    model_api_key: str | None = None
    input_price_per_million_usd: float = 0.0
    output_price_per_million_usd: float = 0.0


class HostGatewayRuntime:
    """Host-side Gateway exposed to the Pi container through Docker."""

    def __init__(
        self,
        *,
        config: GatewayConfig,
        run_dir: Path,
        task_dir: Path,
        trace: TraceRecorder,
    ) -> None:
        self.config = config
        self.run_dir = run_dir
        self.task_dir = task_dir
        self.trace = trace
        self.usage_event_path = run_dir.parent / "gateway.events.jsonl"
        self.token = secrets.token_urlsafe(32)
        self.listen_host = "0.0.0.0"
        self.port = self._pick_port(self.listen_host)
        self.url = f"http://host.docker.internal:{self.port}"

        app = create_gateway_app(
            event_file=run_dir / "events.jsonl",
            trusted_event_file=self.usage_event_path,
            workspace=run_dir,
            task_dir=task_dir,
            policy=load_policy(config.policy_path),
            gateway_token=self.token,
            model_upstream=config.model_upstream,
            model_api_key=config.model_api_key,
            model_pricing=ModelPricing(
                input_per_million_usd=config.input_price_per_million_usd,
                output_per_million_usd=config.output_price_per_million_usd,
            ),
        )
        uvicorn_config = uvicorn.Config(
            app,
            host=self.listen_host,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self.server = uvicorn.Server(uvicorn_config)
        self.thread = threading.Thread(
            target=self.server.run,
            name=f"harness-gateway-{self.port}",
            daemon=True,
        )

    @staticmethod
    def _pick_port(host: str) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
            return int(sock.getsockname()[1])

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started:
            if not self.thread.is_alive():
                raise GatewayRuntimeError("gateway server exited during startup")
            if time.monotonic() >= deadline:
                raise GatewayRuntimeError("gateway server startup timed out")
            time.sleep(0.02)
        self.trace.emit(
            "gateway.started",
            data={
                "url": self.url,
                "listen_host": self.listen_host,
                "model_proxy": self.config.model_upstream is not None,
                "policy": str(self.config.policy_path) if self.config.policy_path else "default",
            },
        )

    def stop(self) -> None:
        if not self.thread.is_alive():
            return
        self.server.should_exit = True
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise GatewayRuntimeError("gateway server did not stop cleanly")
        self.trace.emit("gateway.finished", data={"url": self.url})


GatewayRuntime = HostGatewayRuntime


def build_gateway_runtime(
    *,
    config: GatewayConfig,
    run_dir: Path,
    task_dir: Path,
    trace: TraceRecorder,
) -> GatewayRuntime:
    return HostGatewayRuntime(
        config=config,
        run_dir=run_dir,
        task_dir=task_dir,
        trace=trace,
    )
