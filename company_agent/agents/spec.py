"""One immutable definition per role; no incremental prompt/tool mutation."""
from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files


@dataclass(frozen=True)
class AgentSpec:
    name: str
    tools: frozenset[str]

    def prompt(self) -> str:
        return (
            files(f"company_agent.agents.{self.name}")
            .joinpath('prompt.md')
            .read_text(encoding='utf-8')
            .rstrip('\n')
        )
