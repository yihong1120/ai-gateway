"""Strict, side-effect-free contracts for the controlled workflow.

These models are intentionally much narrower than natural-language requests.
The instruction model may choose only supported output preferences and
calendar operations; it cannot request arbitrary tools, URLs, file access, or
model changes.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator


class _StrictModel(BaseModel):
    """Reject fields that are not part of the explicitly supported contract."""

    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)

    @field_validator('version', mode='before', check_fields=False)
    @classmethod
    def _version_is_canonical(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError('version must be the integer 1')
        return value

    @field_validator('clarification', check_fields=False)
    @classmethod
    def _clarification_is_single_line(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(character in value for character in ('\r', '\n', '\x00')):
            raise ValueError('clarification must be a single line')
        return value


class OutputSelection(_StrictModel):
    """User-selectable derived artifacts; original uploads are never deleted."""

    renamed_pdf: bool = True
    editable_docx: bool = True
    calendar_csv: bool = True

    @model_validator(mode='before')
    @classmethod
    def _discard_retired_output(cls, value):
        # Accept old saved plans without exposing the retired option in new schemas.
        if value and 'searchable_ocr_pdf' in value:
            return {key: item for key, item in value.items() if key != 'searchable_ocr_pdf'}
        return value


class CalendarOperation(_StrictModel):
    """A bounded user-created calendar operation.

    ``anchor`` deliberately accepts only verified document deadlines.  This
    prevents a planner from inventing a date or treating an issue date as a
    deadline.  Date arithmetic happens later in deterministic Python code.
    """

    operation: Literal['add_relative_reminder']
    anchor: Literal['verified_document_deadline'] = 'verified_document_deadline'
    target: Literal['single_verified_deadline', 'all_verified_deadlines']
    # ``strict=True`` rejects booleans, numeric strings, and floats from an
    # untrusted model response.  Otherwise Pydantic would coerce e.g. ``true``
    # to ``1`` and silently create an unintended reminder date.
    offset_days: int = Field(strict=True, ge=-365, le=365)
    title: str = Field(min_length=1, max_length=120)
    # A relative reminder currently has no supported user-controlled clock
    # field.  Keeping it all-day avoids emitting a calendar record with an
    # unspecified time but ``All day event=假``.  Timed reminders can be added
    # later only together with a validated explicit-time contract.
    all_day: Literal[True] = True

    @field_validator('title')
    @classmethod
    def _title_is_single_line(cls, value: str) -> str:
        if any(character in value for character in ('\r', '\n', '\x00')):
            raise ValueError('title must be a single line')
        return value


class CalendarSyncRequest(_StrictModel):
    """Requested EIP behaviour, not authority to write to EIP.

    ``write_after_confirmation`` is converted to a preview/confirmation state
    by the adapter.  A model cannot directly grant itself external-write
    authority.
    """

    request: Literal['none', 'preview', 'write_after_confirmation'] = 'none'


class PendingReminderResolution(_StrictModel):
    """One bounded answer to an already-created reminder clarification.

    This model is deliberately separate from :class:`JobPlan`.  The initial
    planner never knows how many verified deadlines a document contains, so it
    cannot safely select one.  Only the short-lived follow-up flow receives a
    numbered, server-created list of dates and may return one of these three
    decisions.
    """

    version: Literal[1] = 1
    decision: Literal[
        'all_verified_deadlines',
        'selected_verified_deadline',
        'needs_clarification',
    ]
    # An index is an authority boundary into the server-created date list, so
    # it must be an actual integer rather than a coerced bool/string/float.
    selected_deadline_index: int | None = Field(default=None, strict=True, ge=0)
    clarification: str | None = Field(default=None, max_length=300)

    @model_validator(mode='after')
    def _decision_is_consistent(self) -> PendingReminderResolution:
        if self.decision == 'selected_verified_deadline':
            if self.selected_deadline_index is None or self.clarification is not None:
                raise ValueError('a selected deadline requires exactly one index')
        elif self.decision == 'all_verified_deadlines':
            if self.selected_deadline_index is not None or self.clarification is not None:
                raise ValueError('all deadlines cannot include an index or clarification')
        elif self.selected_deadline_index is not None or not self.clarification:
            raise ValueError('an unresolved decision requires clarification only')
        return self


class JobPlan(_StrictModel):
    """The only accepted output of the instruction-planning Agent."""

    version: Literal[1] = 1
    scope: Literal['current_uploads'] = 'current_uploads'
    outputs: OutputSelection = Field(default_factory=OutputSelection)
    calendar_operations: list[CalendarOperation] = Field(default_factory=list, max_length=10)
    eip_calendar: CalendarSyncRequest = Field(default_factory=CalendarSyncRequest)
    needs_clarification: bool = False
    clarification: str | None = Field(default=None, max_length=300)

    @model_validator(mode='after')
    def _clarification_is_consistent(self) -> JobPlan:
        if self.needs_clarification and not self.clarification:
            raise ValueError('clarification is required when needs_clarification is true')
        if not self.needs_clarification and self.clarification:
            raise ValueError('clarification requires needs_clarification=true')
        if (
            self.calendar_operations
            and not self.outputs.calendar_csv
            and self.eip_calendar.request == 'none'
        ):
            raise ValueError(
                'calendar operations require a CSV export or an EIP preview request',
            )
        return self
