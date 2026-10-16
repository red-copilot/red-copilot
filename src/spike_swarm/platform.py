"""Standalone TSec control-plane access for the spike.

Re-implemented rather than imported so the spike cannot reach into the
production package. Semantics are deliberately the same: a 3 requests/second
bucket, the per-team cap of three running instances, one retry on HTTP 429, and
platform-wide failures (VPN, unreachable, instance conflict) classified so the
sweep can exit instead of retrying in a loop.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Self

from .models import Challenge, challenge_from_sdk

PLATFORM_FAILURE_VPN = "vpn_unreachable"
PLATFORM_FAILURE_UNREACHABLE = "platform_unreachable"
PLATFORM_FAILURE_INSTANCES = "instance_conflict"

TSEC_REQUESTS_PER_SECOND = 3.0
TSEC_RATE_LIMIT_BURST = 3
TSEC_INSTANCE_LIMIT = 3
TSEC_RATE_LIMIT_RETRY_DELAY_SECONDS = 1.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0

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


class PlatformError(RuntimeError):
    """A platform call failed and the sweep cannot continue as planned."""


class PlatformUnusable(PlatformError):
    """A platform-wide failure a blind retry cannot clear."""

    def __init__(self, *, kind: str, detail: str | None = None) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}" if detail else kind)


class DuplicateFlag(PlatformError):
    """The platform already saw this flag for this challenge."""


def classify_platform_failure(exc: BaseException) -> str | None:
    """Map a failure onto a platform-wide kind, or ``None`` when case-local."""

    name = type(exc).__name__
    text = str(exc)
    if name == "VpnCheckError" or "VPN" in text:
        return PLATFORM_FAILURE_VPN
    if name == "InvalidState" and "max active challenge instances" in text:
        return PLATFORM_FAILURE_INSTANCES
    if name in _TRANSPORT_ERROR_NAMES or "Connection" in name:
        return PLATFORM_FAILURE_UNREACHABLE
    if isinstance(exc, TimeoutError):
        return PLATFORM_FAILURE_UNREACHABLE
    return None


def _is_rate_limited(exc: BaseException) -> bool:
    if getattr(exc, "status_code", None) == 429:
        return True
    return "ratelimit" in type(exc).__name__.lower() or "429" in str(exc)


def _is_sdk_error(exc: BaseException, expected: str) -> bool:
    name = type(exc).__name__
    text = str(exc).lower()
    return name == expected or expected.lower() in text


class TokenBucket:
    """Async token bucket enforcing the platform's shared 3 rps cap."""

    def __init__(
        self,
        *,
        rate: float = TSEC_REQUESTS_PER_SECOND,
        capacity: int = TSEC_RATE_LIMIT_BURST,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
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
                    self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                    self._updated_at = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await self._sleep((1.0 - self._tokens) / self._rate)


class InstanceRegistry:
    """Tracks started challenges so the team cap of three holds."""

    def __init__(self, *, limit: int = TSEC_INSTANCE_LIMIT) -> None:
        self._limit = limit
        self._open: set[str] = set()

    @property
    def open_codes(self) -> frozenset[str]:
        return frozenset(self._open)

    def register(self, code: str) -> None:
        if code in self._open:
            raise PlatformError(f"challenge already has a running instance: {code}")
        if len(self._open) >= self._limit:
            raise PlatformUnusable(
                kind=PLATFORM_FAILURE_INSTANCES,
                detail=f"concurrent instance limit reached ({self._limit})",
            )
        self._open.add(code)

    def release(self, code: str) -> None:
        self._open.discard(code)


@dataclass(frozen=True)
class StartedChallenge:
    code: str
    addresses: tuple[str, ...]


@dataclass(frozen=True)
class FlagVerdict:
    accepted: bool
    duplicate: bool = False
    awarded: int = 0
    cumulative_score: int = 0
    correct_flag_count: int = 0
    total_flag_count: int = 0
    matched_flag_index: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class TSecConfig:
    base_url: str
    token: str


def load_config(
    *,
    base_url_env: str = "BENCHMARK_BASE_URL",
    token_env: str = "BENCHMARK_TOKEN",
    environ: Mapping[str, str] | None = None,
) -> TSecConfig:
    env = os.environ if environ is None else environ
    base_url = env.get(base_url_env, "")
    token = env.get(token_env, "")
    if not base_url:
        raise PlatformError(f"missing {base_url_env}")
    if not token:
        raise PlatformError(f"missing {token_env}")
    return TSecConfig(base_url=base_url, token=token)


def default_sdk_factory(base_url: str, token: str) -> Any:
    try:
        from tsec_benchmark import TSecBenchmarkAsync
    except ImportError as exc:  # pragma: no cover - depends on the optional dep
        raise PlatformError(
            'tsec-benchmark is not installed; install it with "pip install tsec-benchmark"'
        ) from exc
    return TSecBenchmarkAsync(base_url=base_url, token=token)


@dataclass
class PlatformClient:
    """Async wrapper over the official SDK, with the spike's own bookkeeping."""

    config: TSecConfig
    sdk_factory: Callable[[str, str], Any] = default_sdk_factory
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    instance_limit: int = TSEC_INSTANCE_LIMIT
    rate_limit_retry_delay: float = TSEC_RATE_LIMIT_RETRY_DELAY_SECONDS
    events: list[dict[str, object]] = field(default_factory=list)
    _client: Any = field(default=None, init=False, repr=False)
    _limiter: TokenBucket | None = field(default=None, init=False, repr=False)
    _registry: InstanceRegistry | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = self.sdk_factory(self.config.base_url, self.config.token)
        self._limiter = TokenBucket(sleep=self.sleep)
        self._registry = InstanceRegistry(limit=self.instance_limit)

    @property
    def open_instances(self) -> frozenset[str]:
        assert self._registry is not None
        return self._registry.open_codes

    async def __aenter__(self) -> Self:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self._client.__aexit__(exc_type, exc, tb)

    # -- transport ---------------------------------------------------------
    async def _call(
        self,
        method: Callable[..., Awaitable[Any]],
        *args: object,
        challenge: str = "",
        event: str = "",
    ) -> Any:
        assert self._limiter is not None
        await self._limiter.acquire()
        try:
            result = await asyncio.wait_for(method(*args), timeout=self.request_timeout)
        except Exception as exc:
            if not _is_rate_limited(exc):
                self._record(challenge=challenge, event=event or "call", outcome="error",
                             detail=f"{type(exc).__name__}")
                raise
            await self.sleep(self.rate_limit_retry_delay)
            await self._limiter.acquire()
            result = await asyncio.wait_for(method(*args), timeout=self.request_timeout)
        self._record(challenge=challenge, event=event or "call", outcome="ok")
        return result

    def _record(
        self,
        *,
        challenge: str,
        event: str,
        outcome: str,
        detail: str = "",
    ) -> None:
        self.events.append(
            {
                "challenge": challenge,
                "event": event,
                "outcome": outcome,
                "detail": detail,
            }
        )

    def events_for(self, code: str) -> list[dict[str, object]]:
        return [event for event in self.events if event.get("challenge") in ("", code)]

    # -- operations --------------------------------------------------------
    async def check_vpn(self) -> object:
        return await self._call(self._client.check_vpn, event="check_vpn")

    async def list_challenges(self) -> list[Challenge]:
        listed = await self._call(self._client.list_challenges, event="list_challenges")
        raw_items: Sequence[Any]
        if isinstance(listed, Sequence) and not isinstance(listed, (str, bytes)):
            raw_items = listed
        else:
            raw_items = list(getattr(listed, "challenges", []) or [])
        return [challenge_from_sdk(item) for item in raw_items]

    async def start_challenge(self, code: str) -> StartedChallenge:
        assert self._registry is not None
        self._registry.register(code)
        try:
            started = await self._call(
                self._client.start_challenge, code, challenge=code, event="start_challenge"
            )
        except Exception:
            self._registry.release(code)
            raise
        raw_addresses = getattr(started, "container_addr", None) or ()
        if isinstance(raw_addresses, str):
            raw_addresses = (raw_addresses,)
        addresses = tuple(str(address) for address in raw_addresses)
        return StartedChallenge(code=code, addresses=addresses)

    async def submit_flag(self, code: str, flag: str) -> FlagVerdict:
        try:
            submitted = await self._call(
                self._client.submit_flag, code, flag, challenge=code, event="submit_flag"
            )
        except Exception as exc:
            if _is_sdk_error(exc, "DuplicateSubmit"):
                self._record(
                    challenge=code, event="submit_flag", outcome="duplicate", detail=flag
                )
                return FlagVerdict(accepted=False, duplicate=True, detail="duplicate")
            raise
        verdict = FlagVerdict(
            accepted=bool(getattr(submitted, "correct", False)),
            awarded=int(getattr(submitted, "awarded", 0) or 0),
            cumulative_score=int(getattr(submitted, "cumulative_score", 0) or 0),
            correct_flag_count=int(getattr(submitted, "correct_flag_count", 0) or 0),
            total_flag_count=int(getattr(submitted, "total_flag_count", 0) or 0),
            matched_flag_index=getattr(submitted, "matched_flag_index", None),
            detail="correct" if getattr(submitted, "correct", False) else "rejected",
        )
        self._record(
            challenge=code,
            event="submit_flag",
            outcome="accepted" if verdict.accepted else "rejected",
        )
        return verdict

    async def close_challenge(self, code: str) -> None:
        assert self._registry is not None
        if code not in self._registry.open_codes:
            return
        try:
            await self._call(
                self._client.close_challenge, code, challenge=code, event="close_challenge"
            )
        finally:
            self._registry.release(code)


def platform_failure_kind(exc: BaseException) -> str | None:
    """Classify either our own wrapper or a raw SDK exception."""

    if isinstance(exc, PlatformUnusable):
        return exc.kind
    return classify_platform_failure(exc)
