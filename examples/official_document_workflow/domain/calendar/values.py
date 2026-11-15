"""Pure date, evidence and CSV cell values shared by document interpreters."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from decimal import ROUND_HALF_UP
from typing import Any
from typing import Literal

from examples.official_document_workflow.domain.documents import CALENDAR_PROJECT_CATEGORIES
from examples.official_document_workflow.domain.documents import CJK_INLINE_SPACE
from examples.official_document_workflow.domain.documents import Deadline
from examples.official_document_workflow.domain.documents import DocumentDate
from examples.official_document_workflow.domain.documents import DocumentTime
from examples.official_document_workflow.domain.documents import OfficialDocument
from examples.official_document_workflow.domain.documents import ProcessingText
from examples.official_document_workflow.domain.documents import WHITESPACE


class CalendarValues:
    @classmethod
    def _cloud_deadline_from_event(
        cls,
        event: Any,
        normalised_source: str,
    ) -> Deadline | None:
        if not isinstance(event, dict):
            return None
        # ``due_date`` / ``due_time`` were emitted by version 1.8.  Continue
        # accepting them during an in-flight model or browser transition, but
        # use the clearer start/end field names in new prompts.
        start_date = cls._cloud_date_from_event(
            event.get('start_date', event.get('due_date')),
        )
        if start_date is None:
            return None

        action = cls._cloud_text(event.get('action'), maximum_characters=120)
        evidence = cls._cloud_text(event.get('evidence'), maximum_characters=700)
        if not action or not evidence:
            return None
        normalised_evidence = cls.normalise_text(evidence)
        if not normalised_evidence or normalised_evidence not in normalised_source:
            return None

        start_time_value = (
            event.get('start_time') if 'start_time' in event else event.get('due_time')
        )
        start_time = cls._cloud_time_from_value(start_time_value)
        if start_time_value not in (None, '') and start_time is None:
            return None

        end_date, end_time = cls._cloud_end_fields_from_event(
            event=event,
            start_date=start_date,
            start_time=start_time,
        )
        has_times = start_time is not None or end_time is not None

        responsible_party = cls._cloud_text(
            event.get('responsible_party'), maximum_characters=120,
        )
        if not responsible_party or responsible_party == '未載明':
            responsible_party = '未載明'

        return Deadline(
            date=start_date,
            time=start_time,
            action=action,
            sentence=normalised_evidence,
            responsible_party=responsible_party,
            category=cls._cloud_category_from_event(
                event.get('category'), normalised_source,
            ),
            end_date=end_date,
            end_time=end_time,
            all_day_event=cls._cloud_all_day_from_event(
                event.get('all_day_event'), has_times=has_times,
            ),
            private=cls._cloud_private_from_event(
                event.get('private'), normalised_source,
            ),
            priority=cls._cloud_priority_from_event(
                event.get('priority'), normalised_source,
            ),
            completed=cls._cloud_completed_from_event(
                event.get('completed'), normalised_source,
            ),
            tentative_schedule=event.get('tentative_schedule'),
        )

    @classmethod
    def _cloud_date_from_event(cls, value: Any) -> DocumentDate | None:
        """Parse a model-supplied ISO date without accepting fuzzy formats."""
        if not isinstance(value, str):
            return None
        claimed = value.strip()
        try:
            parsed = datetime.strptime(claimed, '%Y-%m-%d')
        except ValueError:
            return None
        if parsed.strftime('%Y-%m-%d') != claimed:
            return None
        return cls._document_date(
            str(parsed.year - 1911), f'{parsed.month:02d}', f'{parsed.day:02d}',
        )

    @classmethod
    def _cloud_end_fields_from_event(
        cls,
        *,
        event: dict[str, Any],
        start_date: DocumentDate,
        start_time: DocumentTime | None,
    ) -> tuple[DocumentDate | None, DocumentTime | None]:
        """Accept only a structurally valid end supplied by the cloud model."""
        end_date_value = event.get('end_date')
        end_time_value = event.get('end_time')
        if end_date_value in (None, '') and end_time_value in (None, ''):
            return None, None

        end_date: DocumentDate | None = None
        if end_date_value not in (None, ''):
            end_date = cls._cloud_date_from_event(end_date_value)
            if end_date is None or cls._date_sort_key(end_date) < cls._date_sort_key(
                start_date,
            ):
                return None, None

        if end_time_value not in (None, ''):
            end_time = cls._cloud_time_from_value(end_time_value)
            if end_time is None:
                return None, None
            end_date = end_date or start_date
            if start_time is not None and not cls._calendar_duration_hours(
                start_date, start_time, end_date, end_time,
            ):
                # A same-day 22:00–02:00 interval is ambiguous without an
                # explicit next-day date, so do not silently roll it forward.
                return None, None
            return end_date, end_time

        if end_date is None:
            return None, None
        return end_date, None

    @staticmethod
    def _date_sort_key(value: DocumentDate) -> tuple[int, int, int]:
        return (
            int(value.gregorian[0:4]),
            int(value.gregorian[5:7]),
            int(value.gregorian[8:10]),
        )

    @staticmethod
    def _cloud_all_day_from_event(
        value: Any, *, has_times: bool,
    ) -> Literal['真', '假', '']:
        """Treat the model value as a consistency assertion, never authority."""
        if value not in {'真', '假'}:
            return ''
        expected = '假' if has_times else '真'
        return value if value == expected else ''

    @classmethod
    def _cloud_category_from_event(cls, value: Any, normalised_source: str) -> str:
        """Accept a model-selected category from the controlled CSV registry."""
        return cls._cloud_choice_with_provenance(
            value,
            allowed=set(CALENDAR_PROJECT_CATEGORIES),
            normalised_source=normalised_source,
            maximum_choice_characters=120,
        )

    @classmethod
    def _cloud_choice_with_provenance(
        cls,
        value: Any,
        *,
        allowed: set[str],
        normalised_source: str,
        maximum_choice_characters: int = 12,
    ) -> str:
        """Return an allowed LLM choice with a literal source audit trail.

        This intentionally does not interpret the evidence.  The model makes
        the semantic decision from full document context; the backend merely
        checks data shape, the allowed CSV vocabulary, and that an auditable
        source excerpt exists.
        """
        if not isinstance(value, dict):
            return ''
        choice = cls._cloud_text(
            value.get('value'), maximum_characters=maximum_choice_characters,
        )
        evidence = cls._cloud_text(value.get('evidence'), maximum_characters=700)
        if choice not in allowed or not evidence:
            return ''
        normalised_evidence = cls.normalise_text(evidence)
        if not normalised_evidence or normalised_evidence not in normalised_source:
            return ''
        return choice

    @classmethod
    def _cloud_private_from_event(
        cls, value: Any, normalised_source: str,
    ) -> Literal['真', '假', '']:
        choice = cls._cloud_choice_with_provenance(
            value,
            allowed={'真', '假'},
            normalised_source=normalised_source,
        )
        return choice if choice in {'真', '假'} else ''

    @classmethod
    def _cloud_priority_from_event(
        cls, value: Any, normalised_source: str,
    ) -> Literal['低', '普通', '高', '']:
        choice = cls._cloud_choice_with_provenance(
            value,
            allowed={'低', '普通', '高'},
            normalised_source=normalised_source,
        )
        return choice if choice in {'低', '普通', '高'} else ''

    @classmethod
    def _cloud_completed_from_event(
        cls,
        value: Any,
        normalised_source: str,
    ) -> Literal['0', '1', '']:
        choice = cls._cloud_choice_with_provenance(
            value,
            allowed={'0', '1'},
            normalised_source=normalised_source,
        )
        return choice if choice in {'0', '1'} else ''

    @staticmethod
    def _source_lines(value: str) -> list[str]:
        """Normalise whitespace-only layout artifacts without interpreting text."""
        lines: list[str] = []
        for raw_line in value.replace('\u3000', ' ').splitlines():
            line = raw_line.strip()
            if not line:
                continue
            lines.append(line)
        return lines

    @staticmethod
    def _cloud_text(value: Any, *, maximum_characters: int) -> str | None:
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > maximum_characters * 4
        ):
            return None
        if any(
            ord(character) < 32 and character not in '\t\r\n' for character in value
        ):
            return None
        cleaned = ProcessingText.clean(value)
        if not cleaned or len(cleaned) > maximum_characters:
            return None
        return cleaned

    @classmethod
    def _cloud_time_from_value(cls, value: Any) -> DocumentTime | None:
        """Parse the LLM's normalized time as a CSV-format check only."""
        if value in (None, ''):
            return None
        if not isinstance(value, str):
            return None
        claimed = value.strip()
        try:
            parsed = datetime.strptime(claimed, '%H:%M')
        except ValueError:
            return None
        if parsed.strftime('%H:%M') != claimed:
            return None
        return DocumentTime(display=f'{claimed}:00', twenty_four_hour=claimed)

    @classmethod
    def _calendar_duration_hours(
        cls,
        start_date: DocumentDate,
        start_time: DocumentTime | None,
        end_date: DocumentDate | None,
        end_time: DocumentTime | None,
    ) -> str:
        """Calculate a bounded work duration only from validated endpoints."""
        if start_time is None or end_date is None or end_time is None:
            return ''
        try:
            start = datetime(
                *cls._date_sort_key(start_date),
                *map(int, start_time.twenty_four_hour.split(':')),
            )
            end = datetime(
                *cls._date_sort_key(end_date),
                *map(int, end_time.twenty_four_hour.split(':')),
            )
        except ValueError:
            return ''
        minutes = int((end - start).total_seconds() // 60)
        # A negative/zero or month-scale duration is ambiguous for a calendar
        # task, and should never become a plausible-looking CSV work value.
        if not 0 < minutes <= 744 * 60:
            return ''
        hours = (Decimal(minutes) / Decimal(60)).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP,
        )
        return format(hours, 'f').rstrip('0').rstrip('.')

    @staticmethod
    def normalise_text(value: str) -> str:
        # Some scanned-government PDF text layers emit a standalone dot between
        # every visual line, or a leading dot before a line.  Treat those dots as
        # layout artifacts rather than document content. PDF text extraction also
        # inserts whitespace at visual line wraps, including immediately after
        # punctuation (for example ``成果，\n經確認``). Evidence comparison ignores
        # whitespace only; every non-whitespace character and punctuation mark
        # must still match exactly.
        return WHITESPACE.sub('', ''.join(CalendarValues._source_lines(value)))

    @staticmethod
    def _document_date(year_text: str, month_text: str, day_text: str) -> DocumentDate:
        year = int(year_text)
        month = int(month_text)
        day = int(day_text)
        gregorian_year = year + 1911 if year < 1911 else year
        datetime(gregorian_year, month, day)  # Validate the supplied date.
        roc_year = year if year < 1911 else year - 1911
        return DocumentDate(
            roc=f'{roc_year:03d}/{month:02d}/{day:02d}',
            compact_roc=f'{roc_year:03d}{month:02d}{day:02d}',
            gregorian=f'{gregorian_year:04d}/{month:02d}/{day:02d}',
        )

    @staticmethod
    def _csv_value(value: str | None) -> str:
        """Return a document value for CSV, with unknown values left blank."""
        if not isinstance(value, str):
            return ''
        cleaned = ProcessingText.clean(value)
        return '' if cleaned in {'', '未載明', '未指定'} else cleaned

    @staticmethod
    def _csv_subject(value: str | None) -> str:
        """Return a calendar title without losing meaningful punctuation.

        This accepts either the optional CSV-only semantic title or the
        source-faithful document subject.  It deliberately cannot influence
        filenames or DOCX content, which use ``OfficialDocument.subject``
        directly before any CSV rows are created.
        """
        if not isinstance(value, str):
            return ''
        cleaned = CJK_INLINE_SPACE.sub('', WHITESPACE.sub(' ', value).strip())
        return '' if cleaned in {'', '未載明', '未指定'} else cleaned

    @classmethod
    def _csv_description(cls, value: str | None) -> str:
        """Return a source paragraph while retaining its meaningful line breaks."""
        if not isinstance(value, str):
            return ''
        cleaned = '\n'.join(cls._source_lines(value)).strip()
        return '' if cleaned in {'', '未載明', '未指定'} else cleaned

    @classmethod
    def _calendar_description(cls, document: OfficialDocument) -> str:
        """Prefer formal explanation, then a source-grounded cloud summary.

        The single cloud extraction returns a generated description only when
        the formal explanation is absent, and only after literal evidence
        validation. Local code never assembles a substitute from metadata.
        """
        return cls._csv_description(
            document.explanation or document.generated_description,
        )

    @classmethod
    def _calendar_subject(
        cls, document: OfficialDocument | None, deadline: Deadline,
    ) -> str:
        """Use a cloud semantic title only in the CSV export.

        ``calendar_subject`` is intentionally separate from ``subject`` so a
        CSV-specific LLM judgement cannot affect the PDF/DOCX file names or
        the document's source-faithful visible title.
        """
        return (
            cls._csv_subject(deadline.calendar_title)
            or (cls._csv_subject(document.calendar_subject) if document else '')
            or (cls._csv_subject(document.subject) if document else '')
        )
