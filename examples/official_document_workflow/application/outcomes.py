"""Shared group and calendar results for upload and EIP entry points."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from collections.abc import Mapping

from examples.official_document_workflow.domain.documents import (
    OfficialDocument,
    PreparedDocument,
)

from examples.official_document_workflow.domain.calendar.csv_schema import (
    CALENDAR_CSV_FORMAT,
    CALENDAR_CSV_FORMAT_KEY,
)
from examples.official_document_workflow.infrastructure.eip.calendar_csv import (
    direct_import_notice,
)

from .analysis_state import AnalysisState


@dataclass(frozen=True)
class DocumentResult:
    group_index: int
    primary: str | None
    document: OfficialDocument | None
    members: tuple[PreparedDocument, ...]
    native_source_ids: frozenset[str] = frozenset()

    @classmethod
    def from_analysis(cls, analysis):
        members = tuple(analysis.get('members', ()))
        sources = analysis.get('sources', {})
        return cls(
            analysis['group_index'],
            analysis.get('primary'),
            analysis.get('document'),
            members,
            frozenset(
                member.source_file.id for index, member in enumerate(members, 1)
                if (source := sources.get(f'S{index}')) is not None and source.kind == 'native_pdf_text'
            ),
        )


@dataclass(frozen=True)
class CalendarResult:
    rows: tuple[Mapping[str, str], ...]
    issues: tuple[str, ...]
    reviewed: bool

    def __post_init__(self):
        object.__setattr__(
            self, 'rows', tuple(MappingProxyType(dict(row)) for row in self.rows)
        )
        object.__setattr__(self, 'issues', tuple(self.issues))

    @classmethod
    def from_analysis(cls, analysis):
        return cls(
            tuple(analysis.get('rows', ())),
            tuple(analysis.get('csv_errors', ())),
            analysis.get('semantic_review', {}).get('approved') is True,
        )

    def with_reminders(self, rows):
        return CalendarResult((*self.rows, *rows), self.issues, self.reviewed)

    def export_rows(self):
        return [dict(row) for row in self.rows]


@dataclass(frozen=True)
class GroupResult:
    document: DocumentResult
    calendar: CalendarResult
    notes: tuple[str, ...]

    @classmethod
    def from_analysis(cls, analysis: AnalysisState, notes=()):
        return cls(
            DocumentResult.from_analysis(analysis),
            CalendarResult.from_analysis(analysis),
            tuple(notes),
        )


@dataclass(frozen=True)
class DocumentExportResult:
    """Only acknowledged files are downloads; failed/unknown writes remain errors."""

    files: tuple[dict, ...] = ()
    errors: tuple[str, ...] = ()


class CalendarExporter:
    def __init__(self, processor, owner):
        self.processor, self.owner = processor, owner

    async def save(self, rows, name, source_id, metadata):
        payload = self.processor.calendar_csv(list(rows))
        notice = direct_import_notice(payload)
        output = await self.processor.store_output(
            user_id=self.owner,
            payload=payload,
            display_name=name,
            content_type='text/csv; charset=utf-8',
            source_file_id=source_id,
            kind='calendar_csv',
            extra_metadata={**metadata, CALENDAR_CSV_FORMAT_KEY: CALENDAR_CSV_FORMAT},
        )
        return output, notice
