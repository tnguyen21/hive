"""Patch-friendly access to orchestrator-level collaborators.

This keeps dependency usage explicit inside orchestrator mixins without
repeating late imports in every method. Attributes are resolved lazily from
the package module so existing tests can keep patching `hive.orchestrator.*`.
"""

from importlib import import_module
from typing import Any


class _OrchestratorDeps:
    """Resolve collaborators from the `hive.orchestrator` package lazily."""

    def __getattr__(self, name: str) -> Any:
        return getattr(import_module("hive.orchestrator"), name)


deps = _OrchestratorDeps()
