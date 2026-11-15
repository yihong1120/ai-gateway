"""Explicit per-request dependencies for document services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .settings import DocumentSettings


@dataclass
class DocumentRequest:
    settings: DocumentSettings
    owner: str | None = None
    cache: Any = None
    outputs: Any = None
    content_cache: Any = None
    evidence_cache: Any = None
    analysis_results: Any = None


class ConfiguredService:
    def __init__(self, request: DocumentRequest):
        self.request = request

    @property
    def valves(self):
        return self.request.settings

    @property
    def _usage_owner(self):
        return self.request.owner

    @property
    def _eip_processing_cache(self):
        return self.request.cache if self.valves.document_analysis_cache_enabled else None

    @property
    def content_cache(self):
        return self.request.content_cache if self.valves.document_analysis_cache_enabled else None

