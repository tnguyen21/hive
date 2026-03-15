"""Patch-friendly access to orchestrator-level collaborators.

This keeps dependency usage explicit inside orchestrator mixins without
repeating late imports in every method. Attributes are resolved lazily from
the package module so existing tests can keep patching `hive.orchestrator.*`.
"""

from importlib import import_module
from types import ModuleType
from typing import Any


class _OrchestratorDeps:
    """Resolve collaborators from the `hive.orchestrator` package lazily."""

    @staticmethod
    def _module() -> ModuleType:
        return import_module("hive.orchestrator")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._module(), name)


deps = _OrchestratorDeps()
