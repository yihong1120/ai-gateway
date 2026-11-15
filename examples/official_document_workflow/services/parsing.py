"""DocumentParsing responsibilities for the shared document processor."""

from __future__ import annotations

from typing import Any

from examples.official_document_workflow.domain.documents import (
    CloudDocumentExtraction,
    CloudExtractionError,
    CloudSemanticSource,
    Deadline,
    ProcessingText,
)

from .components import ConfiguredService
from examples.official_document_workflow.domain.calendar.rows import CalendarRows


class DocumentParsing(ConfiguredService):
    def __init__(self, request, values):
        super().__init__(request)
        self.values = values

    def _parse_cloud_ocr_text_payload(
        self,
        payload: dict[str, Any],
        model: str,
        source: CloudSemanticSource,
    ) -> CloudDocumentExtraction:
        """Validate structured cloud output against the complete local source.

        This request does not ask the model to echo an evidence bundle: the
        exact complete input remains in
        memory here and is the literal provenance base.  It is request-scoped
        and never written into a downloadable artifact by this method.
        """
        document_payload = payload.get('document')
        if not isinstance(document_payload, dict):
            raise CloudExtractionError('text-cloud response lacks document')
        document = self.values.cloud_document_from_payload(
            document_payload,
            source.text,
            model,
            extraction_source=f'雲端模型 {model} 根據本機 OCR/PDF 文字判讀',
        )
        deadlines = self._parse_cloud_calendar_payload(
            payload,
            source.text,
            maximum_events=self.valves.cloud_max_events,
        )
        return CloudDocumentExtraction(
            model=model,
            document=document,
            deadlines=deadlines,
        )

    def _parse_cloud_calendar_payload(
        self,
        payload: dict[str, Any],
        source_text: str,
        *,
        maximum_events: int | None = None,
    ) -> tuple[Deadline, ...]:
        """Parse the deadline subset after the cloud response JSON is decoded."""
        events = payload.get('events')
        if not isinstance(events, list):
            raise CloudExtractionError('calendar response lacks events')

        source = self.values.normalise_text(source_text)
        if not source:
            raise CloudExtractionError('calendar source text is empty')

        deadlines: list[Deadline] = []
        seen: set[tuple[str, str, str, str, str]] = set()
        for event in events[: maximum_events or self.valves.cloud_max_events]:
            deadline = self.values._cloud_deadline_from_event(event, source)
            if deadline is None:
                continue
            key = (
                deadline.date.gregorian,
                deadline.time.twenty_four_hour if deadline.time else '',
                deadline.end_date.gregorian if deadline.end_date else '',
                deadline.end_time.twenty_four_hour if deadline.end_time else '',
                ProcessingText.clean(deadline.action),
            )
            if key in seen:
                continue
            seen.add(key)
            deadlines.append(deadline)
        return tuple(deadlines)

    def _calendar_rows(self, *, owner_name, document, deadlines):
        return self.calendar_rows.render(owner_name=owner_name, document=document, deadlines=deadlines)

    @property
    def calendar_rows(self):
        return CalendarRows(
            self.valves.calendar_default_private,
            self.valves.calendar_default_priority,
            self.valves.calendar_default_completed,
        )
