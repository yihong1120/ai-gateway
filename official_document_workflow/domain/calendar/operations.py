"""Deterministic calendar transforms after source-validated extraction.

This module does not make HTTP calls.  It prepares the CSV rows and an EIP
write preview; an authenticated EIP adapter can be introduced later without
giving the language model direct network authority.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from datetime import timedelta

from ..workflow import CalendarOperation
from ..workflow import JobPlan


@dataclass(frozen=True)
class CalendarOperationReport:
    """A human-safe result for one user-requested calendar operation."""

    title: str
    applied: int = 0
    clarification: str = ''


@dataclass(frozen=True)
class EipSyncPreview:
    """No-side-effect preview for a future EIP write adapter."""

    unique_event_count: int
    requires_confirmation: bool


@dataclass(frozen=True)
class CalendarTransformResult:
    rows: tuple[dict[str, str], ...]
    reports: tuple[CalendarOperationReport, ...]
    eip_sync_preview: EipSyncPreview | None


def _event_key(row: Mapping[str, str]) -> str:
    """Stable idempotency material; never expose the raw key to users."""
    digest = hashlib.sha256()
    for key in ('Custom Field1', 'Start Date', 'Start Time(24hr)', 'Subject', 'Description'):
        digest.update(row.get(key, '').strip().encode('utf-8'))
        digest.update(b'\x1f')
    return digest.hexdigest()


def _relative_reminder(row: Mapping[str, str], operation: CalendarOperation) -> dict[str, str]:
    base_date = row.get('Start Date', '').strip()
    parsed_date = date(*map(int, base_date.split('/')))
    reminder_date = parsed_date + timedelta(days=operation.offset_days)
    rendered_date = reminder_date.strftime('%Y/%m/%d')
    original_subject = row.get('Subject', '').strip()
    subject = operation.title if not original_subject else f"{operation.title}：{original_subject}"
    reminder = dict(row)
    # "Name" in the supplied CSV means a documented handler, not the person
    # who requested the reminder.  Never misattribute the new event.
    reminder['Name'] = ''
    reminder['Subject'] = subject
    reminder['Start Date'] = rendered_date
    reminder['End Date'] = rendered_date
    reminder['Start Time'] = ''
    reminder['Start Time(24hr)'] = ''
    reminder['End Time'] = ''
    reminder['End Time(24hr)'] = ''
    reminder['Work Hour'] = ''
    reminder['All day event'] = '真' if operation.all_day else row.get('All day event', '')
    original = row.get('Description', '').strip()
    direction = '後' if operation.offset_days >= 0 else '前'
    description = f"使用者建立的追蹤提醒：基準期限 {base_date}，{abs(operation.offset_days)} 天{direction}。"
    reminder['Description'] = f"{original}\n\n{description}" if original else description
    reminder['Report Status'] = ''
    reminder['Completed'] = '0'
    return reminder


def _apply_relative_reminder(
    base_rows: tuple[dict[str, str], ...],
    operation: CalendarOperation,
    seen_keys: set[str],
    *,
    selected_deadline_index: int | None = None,
) -> tuple[list[dict[str, str]], CalendarOperationReport]:
    if not base_rows:
        return [], CalendarOperationReport(
            title=operation.title,
            clarification='公文沒有已驗證的期限，無法建立相對提醒。',
        )
    if operation.target == 'single_verified_deadline' and selected_deadline_index is None and len(base_rows) != 1:
        return [], CalendarOperationReport(
            title=operation.title,
            clarification=(
                f"本批公文有 {len(base_rows)} 項已驗證期限；請指定要套用哪一項，"
                '或改為所有期限。'
            ),
        )

    if operation.target == 'all_verified_deadlines':
        targets = base_rows
    elif selected_deadline_index is None:
        targets = base_rows[:1]
    elif 0 <= selected_deadline_index < len(base_rows):
        targets = (base_rows[selected_deadline_index],)
    else:
        return [], CalendarOperationReport(
            title=operation.title,
            clarification='指定的期限不在本次已驗證範圍；未建立提醒。',
        )
    additions: list[dict[str, str]] = []
    for row in targets:
        reminder = _relative_reminder(row, operation)
        key = _event_key(reminder)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        additions.append(reminder)
    return additions, CalendarOperationReport(
        title=operation.title,
        applied=len(additions),
    )


def apply_calendar_operations(
    rows: list[dict[str, str]],
    plan: JobPlan,
    *,
    selected_deadline_indices: Mapping[int, int] | None = None,
) -> CalendarTransformResult:
    """Apply whitelisted operations only to already validated event rows.

    The original document-derived rows are preserved in order.  Each operation
    uses that fixed source snapshot, so one reminder never becomes another
    operation's implicit anchor.
    """

    base_rows = tuple(dict(row) for row in rows)
    output_rows = list(base_rows)
    seen_keys = {_event_key(row) for row in output_rows}
    reports: list[CalendarOperationReport] = []
    selections = selected_deadline_indices or {}
    for operation_index, operation in enumerate(plan.calendar_operations):
        additions, report = _apply_relative_reminder(
            base_rows,
            operation,
            seen_keys,
            selected_deadline_index=selections.get(operation_index),
        )
        output_rows.extend(additions)
        reports.append(report)
    final_rows = tuple(output_rows)
    preview = None
    if plan.eip_calendar.request != 'none':
        preview = EipSyncPreview(len(seen_keys), True)
    return CalendarTransformResult(
        rows=final_rows,
        reports=tuple(reports),
        eip_sync_preview=preview,
    )
