from __future__ import annotations

import asyncio
import os
import time
import typing
from dataclasses import dataclass


class TSecAdapterError(RuntimeError):
    pass


# Platform-level failures the operator has to fix before a sweep can run: the
# VPN is down, the control plane is unreachable (DNS or transport), or the team
# already holds its three running instances. They are deliberately *not*
# retried in a loop: the 2026-10-16 sweep burned thirty attempts on exactly
# these three states, and each attempt still spawned run directories.
PLATFORM_FAILURE_VPN = "vpn_unreachable"
PLATFORM_FAILURE_UNREACHABLE = "platform_unreachable"
PLATFORM_FAILURE_INSTANCES = "instance_conflict"

# Transport-level exception class names reported by httpx/httpcore and the SDK.
_TRANSPORT_ERROR_NAMES = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "ReadError",
        "WriteError",
        "RemoteProtocolError",
        "TimeoutException",
        "TransportError",
        "gaierror",
    }
)


class TSecPlatformError(TSecAdapterError):
    """A platform-wide failure that a blind retry cannot clear."""

    def __init__(self, *, kind: str, detail: str | None = None) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}" if detail else kind)


def classify_platform_failure(exc: BaseException) -> str | None:
    """Map an SDK failure onto a platform-wide kind, or ``None`` when case-local.

    Matching is by exception class name and message text so the harness never
    imports the optional SDK on a path that only classifies errors.
    """
    name = type(exc).__name__
    text = str(exc)
    if name == "VpnCheckError" or "VPN" in text:
        return PLATFORM_FAILURE_VPN
    if name == "InvalidState" and "max active challenge instances" in text:
        return PLATFORM_FAILURE_INSTANCES
    if name in {"TSecConnectionError"} or name in _TRANSPORT_ERROR_NAMES:
        return PLATFORM_FAILURE_UNREACHABLE
    if "name resolution" in text or "temporary failure in name resolution" in text.lower():
        return PLATFORM_FAILURE_UNREACHABLE
    return None


TSEC_REQUESTS_PER_SECOND = 3.0
TSEC_RATE_LIMIT_BURST = 3
TSEC_INSTANCE_LIMIT = 3
TSEC_RATE_LIMIT_RETRY_DELAY_SECONDS = 1.0

# Bound on the platform reachability gate that runs before any run directory
# exists. The SDK's own VPN health check allows 10s; this leaves room for the
# challenge listing on a slow tunnel.
TSEC_PREFLIGHT_TIMEOUT_SECONDS = 45.0


@dataclass(frozen=True)
class TSecConfig:
    base_url: str
    token: str


def load_tsec_config(
    *,
    base_url_env: str = "BENCHMARK_BASE_URL",
    token_env: str = "BENCHMARK_TOKEN",
) -> TSecConfig:
    base_url = os.environ.get(base_url_env, "")
    token = os.environ.get(token_env, "")
    if not base_url:
        raise TSecAdapterError(f"missing {base_url_env}")
    if not token:
        raise TSecAdapterError(f"missing {token_env}")
    return TSecConfig(base_url=base_url, token=token)


class _TokenBucket:
    """Async token bucket enforcing the platform's 3 requests/second cap."""

    def __init__(
        self,
        *,
        rate: float = TSEC_REQUESTS_PER_SECOND,
        capacity: int = TSEC_RATE_LIMIT_BURST,
        clock: typing.Callable[[], float] = time.monotonic,
        sleep: typing.Callable[[float], typing.Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._rate = rate
        self._capacity = float(capacity)
        self._tokens = float(capacity)
        self._clock = clock
        self._sleep = sleep
        self._updated_at = clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                elapsed = now - self._updated_at
                if elapsed > 0:
                    self._tokens = min(
                        self._capacity, self._tokens + elapsed * self._rate
                    )
                    self._updated_at = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await self._sleep((1.0 - self._tokens) / self._rate)


class _InstanceRegistry:
    """Tracks started challenge instances so the team cap of three holds."""

    def __init__(self, *, limit: int = TSEC_INSTANCE_LIMIT) -> None:
        self._limit = limit
        self._open: set[str] = set()

    @property
    def open_codes(self) -> frozenset[str]:
        return frozenset(self._open)

    def register(self, unique_code: str) -> None:
        if unique_code in self._open:
            raise TSecAdapterError(
                f"challenge already has a running instance: {unique_code}"
            )
        if len(self._open) >= self._limit:
            # Case-local by construction (our dispatcher bounds concurrency):
            # the server-side stop_pending race is retried in _start, and a
            # sweep must not die over a slot that frees itself in seconds.
            raise TSecAdapterError(
                f"concurrent instance limit reached ({self._limit}); "
                "retry this case on a later pass"
            )
        self._open.add(unique_code)

    def release(self, unique_code: str) -> None:
        self._open.discard(unique_code)


def _is_rate_limited(exc: Exception) -> bool:
    if getattr(exc, "status_code", None) == 429:
        return True
    return "ratelimit" in type(exc).__name__.lower() or "429" in str(exc)


class TSecClientAdapter:
    """Async wrapper over the official TSec Benchmark SDK.

    Every call passes a shared token bucket, started and closed challenges are
    accounted for against the platform's per-team instance cap, and a 429 is
    retried at most once before the call fails closed.
    """

    def __init__(
        self,
        config: TSecConfig,
        *,
        sdk_factory: typing.Callable[[str, str], typing.Any] | None = None,
        limiter: _TokenBucket | None = None,
        registry: _InstanceRegistry | None = None,
        sleep: typing.Callable[[float], typing.Awaitable[None]] = asyncio.sleep,
        rate_limit_retry_delay: float = TSEC_RATE_LIMIT_RETRY_DELAY_SECONDS,
    ) -> None:
        factory = sdk_factory or _default_sdk_factory
        try:
            self._client = factory(config.base_url, config.token)
        except ImportError as exc:  # pragma: no cover - import guard
            raise TSecAdapterError(
                'tsec-benchmark is not installed; install red-harness with ".[tsec]"'
            ) from exc
        self._limiter = limiter or _TokenBucket(sleep=sleep)
        self._registry = registry or _InstanceRegistry()
        self._sleep = sleep
        self._rate_limit_retry_delay = rate_limit_retry_delay

    @property
    def open_instances(self) -> frozenset[str]:
        return self._registry.open_codes

    async def _call(self, method: typing.Callable[..., typing.Any], *args) -> typing.Any:
        await self._limiter.acquire()
        try:
            return await method(*args)
        except Exception as exc:
            if not _is_rate_limited(exc):
                raise
            await self._sleep(self._rate_limit_retry_delay)
            await self._limiter.acquire()
            return await method(*args)

    async def __aenter__(self) -> typing.Self:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._client.__aexit__(exc_type, exc, tb)

    async def list_challenges(self):
        return await self._call(self._client.list_challenges)

    async def check_vpn(self):
        return await self._call(self._client.check_vpn)

    async def start_challenge(self, unique_code: str):
        self._registry.register(unique_code)
        try:
            return await self._call(self._client.start_challenge, unique_code)
        except Exception:
            self._registry.release(unique_code)
            raise

    async def submit_flag(self, unique_code: str, flag: str):
        return await self._call(self._client.submit_flag, unique_code, flag)

    async def close_challenge(self, unique_code: str):
        try:
            return await self._call(self._client.close_challenge, unique_code)
        finally:
            self._registry.release(unique_code)

    async def get_hint(self, unique_code: str):
        return await self._call(self._client.get_hint, unique_code)


def _default_sdk_factory(base_url: str, token: str) -> typing.Any:
    from tsec_benchmark import TSecBenchmarkAsync

    return TSecBenchmarkAsync(base_url=base_url, token=token)
