"""Typed contracts created once at the LLM boundary."""
from __future__ import annotations

from copy import deepcopy
from typing import Annotated
from typing import Literal
from typing import NotRequired

from pydantic import ConfigDict
from pydantic import Field
from pydantic import TypeAdapter
from typing_extensions import TypedDict


class OutputContract(TypedDict):
    __pydantic_config__ = ConfigDict(extra='forbid')


class SourceSpan(OutputContract):
    source_id: str
    start_line: int
    end_line: int


class RelativeDeadline(OutputContract):
    anchor: Literal['receipt', 'issue']
    days: Annotated[int, Field(strict=True, ge=1, le=3660)]
    day_type: Literal['calendar_days', 'working_days']
    include_anchor_day: bool


class TrackingReminder(OutputContract):
    days_after_anchor: Annotated[int, Field(strict=True, ge=1)]
    reason: Annotated[str, Field(min_length=1, max_length=160)]


class EventAssignment(OutputContract):
    executor: str | None
    date_basis: Literal['explicit', 'relative', 'unspecified']
    anchor_date: str | None
    relative_deadline: NotRequired[RelativeDeadline | None]
    tracking_reminder: NotRequired[TrackingReminder | None]


class EventEvidence(OutputContract):
    title: list[SourceSpan]
    action: list[SourceSpan]
    project_name: list[SourceSpan]
    location: list[SourceSpan]
    responsible_party: list[SourceSpan]
    relevance: list[SourceSpan]
    start_date: list[SourceSpan]
    start_time: list[SourceSpan]
    end_date: list[SourceSpan]
    end_time: list[SourceSpan]
    anchor_date: list[SourceSpan]


class PreparationReminder(OutputContract):
    days_before: Annotated[int, Field(strict=True, ge=1)]
    start_time: Annotated[str, Field(pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$', description='AI 建議開始時間，台北時間 HH:MM。')]
    end_time: Annotated[
        str, Field(
            pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$',
            description='AI 依工作所需時長建議的同日結束時間，HH:MM，須晚於開始時間。',
        ),
    ]
    action: Annotated[str, Field(min_length=1, max_length=120)]
    reason: Annotated[str, Field(min_length=1, max_length=200)]
    evidence_spans: Annotated[list[SourceSpan], Field(min_length=1, max_length=8)]


class TentativeSchedule(OutputContract):
    start_time: Annotated[str, Field(pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')]
    end_date: Annotated[str, Field(pattern=r'^\d{4}-\d{2}-\d{2}$')]
    end_time: Annotated[str, Field(pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')]
    reason: Annotated[str, Field(min_length=1, max_length=200)]


class CalendarEvent(OutputContract):
    status: Literal['current', 'needs_review', 'historical', 'cancelled', 'out_of_scope']
    title: str | None
    action: str | None
    project_name: str | None
    location: str | None
    responsible_party: str | None
    start_date: str | None
    start_time: str | None
    end_date: str | None
    end_time: str | None
    evidence_spans: EventEvidence
    description_spans: list[SourceSpan]
    assignment: NotRequired[EventAssignment]
    kind: NotRequired[str]
    review_reason: NotRequired[str]
    preparation_reminders: NotRequired[list[PreparationReminder]]
    tentative_schedule: NotRequired[TentativeSchedule | None]


class DocumentFields(OutputContract):
    issued_date: Annotated[str | None, Field(description='發文日期轉成西元 YYYY-MM-DD。')]
    reference_number: str | None
    subject: str | None
    filename_subject: str | None
    recipient: str | None
    handler_name: str | None
    handling_unit: str | None
    contact_phone: str | None
    contact_email: str | None
    sender_address: str | None
    primary_recipients: str | None


class DocumentEvidence(OutputContract):
    issued_date: Annotated[str | None, Field(description='逐字複製來源的發文日期，保留民國年與年月日文字；不可放西元轉換結果。')]
    reference_number: str | None
    subject: Annotated[str | None, Field(description='完整主旨的逐字原文，保留跨行內容、原字與標點，不含主旨欄名或其他段落；分組輸出直接採用此引文作正式主旨。')]
    filename_subject: str | None
    recipient: str | None
    handler_name: str | None
    handling_unit: str | None
    contact_phone: str | None
    contact_email: str | None
    sender_address: str | None
    primary_recipients: str | None


class GroupDocument(DocumentFields):
    evidence: Annotated[DocumentEvidence, Field(description='每個值都是主公文中的逐字原文，不能改寫、轉換或拼接。未知資料填 null。')]


class GroupAnalysis(OutputContract):
    calendar_contract_version: Literal[2]
    events: list[CalendarEvent]
    primary_source_id: str | None
    group_status: Literal['related', 'needs_review']
    read_source_ids: list[str]
    document: GroupDocument


class CalendarDescription(OutputContract):
    mode: Literal['verbatim', 'generated']
    value: str
    evidence: list[str]


class SingleDocumentEvidence(DocumentEvidence):
    calendar_subject: str | None


class SingleDocument(DocumentFields):
    calendar_subject: str | None
    calendar_description: CalendarDescription
    evidence: SingleDocumentEvidence


class SupportedValue(OutputContract):
    value: str | None
    evidence: str | None


class SingleEvent(OutputContract):
    start_date: str
    start_time: str | None
    end_date: str | None
    end_time: str | None
    all_day_event: Literal['真', '假'] | None
    work_hour: str | None
    action: str
    category: SupportedValue
    private: SupportedValue
    priority: SupportedValue
    completed: SupportedValue
    responsible_party: str | None
    evidence: str
    tentative_schedule: NotRequired[TentativeSchedule | None]


class SingleAnalysis(OutputContract):
    document: SingleDocument
    events: list[SingleEvent]


class ReviewCheck(OutputContract):
    index: int
    decision: Literal['keep', 'merge', 'reject']
    target: int | None
    reason: str
    evidence_spans: list[SourceSpan]
    blocking_fields: NotRequired[list[str]]


class CalendarReview(OutputContract):
    issues: list[str]
    checks: list[ReviewCheck]


class EvidenceScan(OutputContract):
    read_count: int
    first_id: str
    last_id: str
    keep_ranges: list[list[int]]
    uncertain: bool


def require_tentative_schedule_decision(schema):
    """Require new model responses to decide, while legacy readers stay valid.

    Omission silently selects the old fixed-duration fallback. A nullable but
    required output field makes the model explicitly propose a content-based
    interval or choose no estimate. Keep cached/shared input schemas untouched.
    """
    result = deepcopy(schema)
    pending = [result]
    while pending:
        match pending.pop():
            case dict() as node:
                if 'tentative_schedule' in node.get('properties', {}):
                    required = node.setdefault('required', [])
                    if 'tentative_schedule' not in required:
                        required.append('tentative_schedule')
                pending.extend(node.values())
            case list() as node:
                pending.extend(node)
    return result


GROUP_ANALYSIS_SCHEMA = TypeAdapter(GroupAnalysis).json_schema()
CALENDAR_REVIEW_SCHEMA = TypeAdapter(CalendarReview).json_schema()
SINGLE_ANALYSIS_SCHEMA = TypeAdapter(SingleAnalysis).json_schema()
EVIDENCE_SCAN_SCHEMA = TypeAdapter(EvidenceScan).json_schema()
