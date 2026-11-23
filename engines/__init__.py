"""Vajrastra engines: the adversarial debugger and architect.

Engines may use optional heavy libraries (``z3``, ``sympy``, ``hypothesis``)
but only lazily, inside the functions that need them, so importing an engine on
Android without those packages always succeeds.
"""

from __future__ import annotations

__all__ = ["hyper_debugger", "code_architect"]
