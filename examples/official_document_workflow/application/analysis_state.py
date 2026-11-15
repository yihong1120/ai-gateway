"""In-memory document analysis state; never a persistent credential container."""

from __future__ import annotations

from typing import TypedDict

from examples.official_document_workflow.domain.documents import (
    CloudSemanticSource,
    Deadline,
    OfficialDocument,
    PreparedDocument,
)


class AnalysisState(TypedDict, total=False):
    members: list[PreparedDocument]
    sources: dict[str, CloudSemanticSource]
    group_index: int
    group_count: int
    document_focus: dict | None
    allow_chunking: bool
    primary: str | None
    document: OfficialDocument | None
    deadlines: list[Deadline]
    rows: list[dict[str, str]]
    payload: dict | None
    analysis_status: str
    analysis_attempts: int
    repair_history: list[dict]
    analysis_input: dict
    accepted_events: list[dict]
    metadata_notices: list[str]
    csv_errors: list[str]
    validation_issues: list[dict]
    repair_had_events: bool
    repair_event_count: int
