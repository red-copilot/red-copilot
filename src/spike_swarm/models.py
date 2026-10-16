"""Value objects, selection rules and errors for the Pi-swarm spike.

Nothing here touches Docker, the network or the TSec SDK, so every selection
rule can be unit tested offline.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

SPIKE_ENV_PREFIX = "SPIKE_"

MAX_SESSIONS_PER_TASK = 3
MAX_PARALLEL_TASKS = 3
MAX_SUBGOALS = 16
MAX_OBJECTIVE_CHARS = 8000
MAX_DESCRIPTION_CHARS = 500

DEFAULT_ROUND_SECONDS = 180
DEFAULT_WALL_TIME_SECONDS = 3600
DEFAULT_COUNT = 3
DEFAULT_PARALLEL = 3

DEFAULT_IMAGE = "harness/pi-kali:local"
DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_TOOLS = ("read", "bash", "edit", "write")

# Ordered difficulty ladder. Unrecognised labels land in the middle so an
# unknown difficulty neither jumps the selection queue nor sinks to the bottom.
DIFFICULTY_LADDER: tuple[tuple[tuple[str, ...], float], ...] = (
    (("trivial", "intro", "warmup", "warm-up"), 0.0),
    (("easy",), 1.0),
    (("easy-medium", "medium-easy"), 2.0),
    (("medium",), 3.0),
    (("medium-hard", "hard-medium"), 4.0),
    (("hard",), 5.0),
    (("insane", "expert", "extreme"), 6.0),
)
UNKNOWN_DIFFICULTY_RANK = 3.0

_DIFFICULTY_EXACT: dict[str, float] = {
    key: rank for keys, rank in DIFFICULTY_LADDER for key in keys
}
_LABEL_SEPARATORS = re.compile(r"[\s_]+")


class SpikeError(RuntimeError):
    """Base class for spike failures that must stop the sweep."""


class SelectionError(SpikeError):
    """The requested challenge set cannot be built."""


class LayoutError(SpikeError):
    """A run directory cannot be prepared safely."""


def _normalise_label(value: str) -> str:
    return _LABEL_SEPARATORS.sub("-", value.strip().lower())


def difficulty_rank(value: object) -> float:
    """Map a platform difficulty onto the ordered ladder (lower is easier)."""

    if value is None or isinstance(value, bool):
        return UNKNOWN_DIFFICULTY_RANK
    if isinstance(value, (int, float)):
        return float(value)
    text = _normalise_label(str(value))
    if not text:
        return UNKNOWN_DIFFICULTY_RANK
    try:
        return float(text)
    except ValueError:
        pass
    if text in _DIFFICULTY_EXACT:
        return _DIFFICULTY_EXACT[text]
    longest: tuple[int, float] | None = None
    for key, rank in _DIFFICULTY_EXACT.items():
        if key in text and (longest is None or len(key) > longest[0]):
            longest = (len(key), rank)
    return longest[1] if longest is not None else UNKNOWN_DIFFICULTY_RANK


def _as_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Challenge:
    """The subset of a platform challenge the sweep reasons about."""

    code: str
    description: str = ""
    difficulty: object | None = None
    level: int | None = None
    total_score: int = 0
    flag_count: int = 0
    correct_flag_count: int = 0
    is_completed: bool = False

    @property
    def difficulty_label(self) -> str:
        return "" if self.difficulty is None else str(self.difficulty)

    @property
    def remaining_flags(self) -> int:
        return max(0, self.flag_count - self.correct_flag_count)

    @property
    def selection_key(self) -> tuple[float, int, int, int, str]:
        """Easiest first, then the most deterministic tie-breaks available."""

        return (
            difficulty_rank(self.difficulty),
            _as_int(self.level, 0),
            _as_int(self.total_score, 0),
            _as_int(self.flag_count, 0),
            self.code,
        )


def challenge_from_sdk(raw: Any) -> Challenge:
    """Adapt an SDK challenge object without importing the SDK type."""

    code = getattr(raw, "unique_code", None) or getattr(raw, "code", None)
    if not code:
        raise SelectionError(f"challenge object without a unique code: {raw!r}")
    return Challenge(
        code=str(code),
        description=str(getattr(raw, "description", "") or ""),
        difficulty=getattr(raw, "difficulty", None),
        level=getattr(raw, "level", None),
        total_score=_as_int(getattr(raw, "total_score", 0)),
        flag_count=_as_int(getattr(raw, "flag_count", 0)),
        correct_flag_count=_as_int(getattr(raw, "correct_flag_count", 0)),
        is_completed=bool(getattr(raw, "is_completed", False)),
    )


def select_challenges(
    challenges: Sequence[Challenge],
    *,
    count: int = DEFAULT_COUNT,
    overrides: Iterable[str] = (),
) -> list[Challenge]:
    """Pick the challenge set for a sweep, easiest first.

    ``overrides`` keeps the operator's explicit order and fails closed on
    unknown codes or duplicates; the automatic path drops completed challenges
    and refuses to run a partial sweep.
    """

    if count < 1:
        raise SelectionError("count must be at least 1")
    requested = [code.strip() for code in overrides if code and code.strip()]
    by_code = {challenge.code: challenge for challenge in challenges}
    if requested:
        if len(requested) > count:
            raise SelectionError(
                f"{len(requested)} explicit challenges exceed the requested count of {count}"
            )
        if len(set(requested)) != len(requested):
            raise SelectionError("explicit challenges contain duplicates")
        unknown = [code for code in requested if code not in by_code]
        if unknown:
            raise SelectionError("unknown challenge code(s): " + ", ".join(sorted(unknown)))
        return [by_code[code] for code in requested]

    eligible = [challenge for challenge in challenges if not challenge.is_completed]
    ordered = sorted(eligible, key=lambda challenge: challenge.selection_key)
    if len(ordered) < count:
        raise SelectionError(
            f"only {len(ordered)} unfinished challenge(s) available, {count} required"
        )
    return ordered[:count]
