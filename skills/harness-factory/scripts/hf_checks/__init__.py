"""harness-factory audit checks.

HF_VERSION is defined here and only here; scripts import it from this package.
"""
from __future__ import annotations

HF_VERSION = "0.1.0"


def load_checks():
    """Import the rung modules (which register their checks) and return them in order."""
    from . import rung1, rung2, rung3, rung4, rung5  # noqa: F401  (import registers)
    from .agents import load_agents
    load_agents()  # agent_<id>.py modules register their evaluators
    from .registry import all_checks
    return all_checks()
