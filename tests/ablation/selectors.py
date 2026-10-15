"""Local copy of the retired ``StateSelector`` contract.

The live World model no longer carries selectors: skills became plain markdown
and the goal-driven frontier was removed. The offline baseline keeps its own
copy so the algorithm stays executable without re-introducing the contract into
the package.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

WorldObjectKind = Literal[
    "entity",
    "relation",
    "observation",
    "artifact",
    "capability",
    "hypothesis",
    "goal",
    "action",
    "constraint",
    "failure",
]


class StateSelector(BaseModel):
    """A declarative reference to a World record at a minimum trust level."""

    model_config = ConfigDict(extra="forbid")

    kind: WorldObjectKind
    type: str = Field(min_length=1)
    scope: str | None = None
    minimum_trust: Literal["claim", "evidence", "verified"] = "claim"
