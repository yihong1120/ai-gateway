"""Shared reviewed-calendar transformation and partial-success file export."""
from __future__ import annotations

from dataclasses import dataclass

from .outcomes import CalendarExporter, CalendarResult
from examples.official_document_workflow.domain.calendar.operations import apply_calendar_operations
from examples.official_document_workflow.domain.calendar.scheduling import TENTATIVE_NOTICE, EIP_REVIEW_NOTICE
from examples.official_document_workflow.domain.documents import ProcessingError


@dataclass(frozen=True)
class PreparedCalendar:
    calendar: CalendarResult
    notices: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()


@dataclass(frozen=True)
class CalendarExport:
    prepared: PreparedCalendar
    file: object | None = None
    notice: str = ''
    error: str = ''

    @property
    def state(self):
        return 'failed' if self.error else 'stored' if self.file else 'empty'


class CalendarStage:
    def __init__(self, processor, owner):
        self.exporter = CalendarExporter(processor, owner)

    @staticmethod
    def approved_rows(calendar):
        return calendar.export_rows() if calendar.reviewed else []

    @staticmethod
    def prepare(calendar, plan):
        rows = CalendarStage.approved_rows(calendar)
        transformed = apply_calendar_operations(rows, plan)
        notices, pending = [], []
        for report in transformed.reports:
            if report.clarification:
                pending.append('提醒待確認：' + report.clarification)
            if report.applied:
                notices.append(f'已加入 {report.applied} 項使用者提醒：{report.title}')
        if any(TENTATIVE_NOTICE in row.get('Description', '') for row in transformed.rows):
            notices.append('部分行程採用 AI 暫排，非公文明訂時間。' + EIP_REVIEW_NOTICE)
        return PreparedCalendar(CalendarResult(transformed.rows, calendar.issues, calendar.reviewed),
                                tuple(notices), tuple(pending))

    async def save(self, prepared, name, source_id, metadata):
        if not prepared.calendar.rows or not prepared.calendar.reviewed:
            return CalendarExport(prepared)
        try:
            output, notice = await self.exporter.save(prepared.calendar.export_rows(), name, source_id, metadata)
        except (ValueError, OSError, ProcessingError) as error:
            return CalendarExport(prepared, error=str(error))
        return CalendarExport(prepared, output, notice)
