"""Pure calendar rendering configured by three explicit CSV defaults."""

from __future__ import annotations
from dataclasses import dataclass
from examples.official_document_workflow.domain.documents import (
    CALENDAR_PROJECT_CATEGORIES,
    Deadline,
    OfficialDocument,
)
from .scheduling import apply_tentative_schedule
from .values import CalendarValues


@dataclass(frozen=True)
class CalendarRows:
    default_private: str = ''
    default_priority: str = ''
    default_completed: str = ''

    def _calendar_private(self, deadline: Deadline) -> str:
        """A cloud-selected private state may raise, never lower, privacy."""
        if deadline.private == '真':
            return '真'
        if self.default_private:
            return self.default_private
        return deadline.private

    def _calendar_priority(self, deadline: Deadline) -> str:
        return deadline.priority or self.default_priority

    def _calendar_completed(self, deadline: Deadline) -> str:
        return deadline.completed or self.default_completed

    def render(
        self,
        *,
        owner_name: str,
        document: OfficialDocument | None,
        deadlines: list[Deadline],
    ) -> list[dict[str, str]]:
        handling_unit = (
            CalendarValues._csv_value(document.handling_unit) if document else ''
        )
        recipient = CalendarValues._csv_value(document.recipient) if document else ''
        rows: list[dict[str, str]] = []
        for deadline in deadlines:
            start_time = deadline.time
            end_time = deadline.end_time
            responsible_party = CalendarValues._csv_value(deadline.responsible_party)
            category = CalendarValues._csv_value(deadline.category)
            if category not in CALENDAR_PROJECT_CATEGORIES:
                category = ''
            calendar_subject = CalendarValues._calendar_subject(document, deadline)
            description = (
                (
                    CalendarValues._csv_description(document.explanation)
                    if document
                    else ''
                )
                or deadline.calendar_description
                or (CalendarValues._calendar_description(document) if document else '')
            )
            # The cloud may state an all-day value, but it is intentionally
            # derived again from validated timings here. A claim cannot turn a
            # timed event into an all-day one (or vice versa).
            is_all_day = start_time is None and end_time is None
            end_date = deadline.end_date or (
                deadline.date
                if is_all_day and not deadline.tentative_schedule
                else None
            )
            rows.append(
                {
                    'Name': owner_name,
                    'Subject': calendar_subject,
                    'Start Date': deadline.date.gregorian,
                    'Start Time': start_time.display if start_time else '',
                    'Start Time(24hr)': start_time.twenty_four_hour
                    if start_time
                    else '',
                    'End Date': end_date.gregorian if end_date else '',
                    'End Time': end_time.display if end_time else '',
                    'End Time(24hr)': end_time.twenty_four_hour if end_time else '',
                    'Work Hour': CalendarValues._calendar_duration_hours(
                        deadline.date,
                        start_time,
                        end_date,
                        end_time,
                    ),
                    'All day event': '真' if is_all_day else '假',
                    'Private': self._calendar_private(deadline),
                    'Description': description,
                    'Priority': self._calendar_priority(deadline),
                    'Categories': category,
                    'Custom Field1': CalendarValues._csv_value(
                        document.reference_number
                    )
                    if document
                    else '',
                    'Custom Field2': CalendarValues._csv_value(document.issued.roc)
                    if document
                    else '',
                    'Custom Field3': recipient,
                    'Custom Field4': responsible_party,
                    'Custom Field5': handling_unit,
                    'Report Status': '',
                    'Completed': self._calendar_completed(deadline),
                },
            )
        return [
            apply_tentative_schedule(row, deadline.tentative_schedule)
            for row, deadline in zip(rows, deadlines)
        ]
