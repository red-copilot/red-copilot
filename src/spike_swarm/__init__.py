"""Standalone Pi-swarm spike runtime.

This package is deliberately disconnected from ``harness``: it never imports the
production package, shares none of its modules, and is not reachable from the
``harness`` CLI. It exists to test one hypothesis end to end - three Pi solvers
competing on one challenge while the runtime renders a shared intent-fact graph
that the solvers have to read for their goals to close.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
