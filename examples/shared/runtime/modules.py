"""Declare optional host/worker dependencies without starting their runtime.

Ordinary dependencies belong in the import block. A module reference is for an
optional host or a dependency that can only load after application configuration,
or for an explicit cycle between request-time services. Resolution uses Python's
normal import lock/cache, and observes the current module during isolated tests.
"""
from __future__ import annotations

import builtins
import sys
from dataclasses import dataclass
from types import ModuleType


@dataclass(frozen=True)
class ModuleReference:
    name: str

    def load(self, *, top_level: bool = False) -> ModuleType:
        builtins.__import__(self.name)
        return sys.modules[self.name.split('.')[0] if top_level else self.name]

    def attribute(self, name: str):
        module = builtins.__import__(self.name, fromlist=[name])
        try:
            return getattr(module, name)
        except AttributeError:
            child = sys.modules.get(self.name + '.' + name)
            if child is not None:
                return child
            raise ImportError(f'cannot import name {name!r} from {self.name!r}') from None

    def attributes(self, *names: str) -> tuple:
        return tuple(self.attribute(name) for name in names)
