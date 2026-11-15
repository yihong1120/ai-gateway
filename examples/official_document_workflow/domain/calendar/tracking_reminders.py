"""Opt-in follow-up reminders for proven work with no stated deadline."""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from datetime import timedelta
from difflib import SequenceMatcher

from pydantic import TypeAdapter

from .contracts import TrackingReminder
from .relative_dates import ReceiptContext
from examples.shared.runtime.modules import ModuleReference

_workflow_calendar_semantics = ModuleReference('examples.official_document_workflow.domain.calendar.calendar_semantics')


TRACKING_TITLE = '【追蹤提醒】'
TRACKING_NOTICE = 'AI 推估追蹤提醒（非公文明定期限，請核對）'


def enabled_for(owner, identity):
    """A server-owned owner/identity allowlist; rebinding never grants consent."""
    pairs = {item.strip() for item in os.getenv('EIP_TRACKING_REMINDER_ACCOUNTS', '').split(',') if item.strip()}
    return bool(owner and identity and f'{owner}:{identity}' in pairs)


@dataclass(frozen=True)
class TrackingReminderProof:
    anchor_date: str
    anchor_source: str
    days_after_anchor: int
    reminder_date: str
    reason: str
    action_evidence: str
    reference_number: str

    def supports(self, value, evidence):
        if value != self.reminder_date or not evidence.strip():
            return False
        if (date.fromisoformat(self.anchor_date) + timedelta(days=self.days_after_anchor)).isoformat() != value:
            return False

        def normalize(s): return re.sub(r'\s+', '', unicodedata.normalize('NFKC', s))
        original, cited = normalize(self.action_evidence), normalize(evidence)
        if not original or not cited:
            return False
        if original in cited or cited in original:
            return True
        # Long repetitive OCR must not make evidence comparison quadratic.
        if len(original) > 8000 or len(cited) > 8000:
            return False
        return SequenceMatcher(None, original, cited, autojunk=False).find_longest_match().size >= min(4, len(original), len(cited))

    def audit(self):
        return {
            'estimated': True, 'kind': 'tracking_reminder', 'anchor_date': self.anchor_date,
            'anchor_source': self.anchor_source, 'days_after_anchor': self.days_after_anchor,
            'reminder_date': self.reminder_date, 'reason': self.reason,
            'receipt_date_recorded': self.anchor_source == 'EIP 收文日期',
            'reference_number': self.reference_number,
        }

    def notice(self):
        return '\n'.join([
            TRACKING_NOTICE, f'起算依據：{self.anchor_source} {self.anchor_date}',
            f'AI 建議 {self.days_after_anchor} 個曆日後追蹤：{self.reminder_date}',
            f'AI 推估理由：{self.reason}', '此日期僅供追蹤，不代表收文日或履行期限已確認。',
        ])


def estimate_tracking_reminder(event, sources, document, receipt_context=None):
    """Only source-backed established work may receive a suggested follow-up."""
    relation = event.get('assignment') or {}
    if (
        event.get('status') != 'current' or relation.get('date_basis') != 'unspecified'
        or not relation.get('tracking_reminder') or event.get('start_date')
        or event.get('end_date') or event.get('start_time') or event.get('end_time')
    ):
        return event
    try:
        suggestion = TypeAdapter(TrackingReminder).validate_python(relation['tracking_reminder'])
    except ValueError as error:
        raise ValueError('AI 追蹤提醒的天數或理由格式無效。') from error
    reason = suggestion['reason'].strip()
    if not reason or any(ord(c) < 32 for c in reason):
        raise ValueError('AI 追蹤提醒的天數或理由格式無效。')
    event_date_text, explicit_source_dates, resolve_spans = _workflow_calendar_semantics.attributes(
        'event_date_text', 'explicit_source_dates', 'resolve_spans',
    )

    refs = event.get('evidence_spans') or {}
    spans = refs.get('action')
    evidence = '\n'.join(part[3] for part in resolve_spans(spans, sources))
    if not evidence.strip():
        raise ValueError('AI 追蹤提醒缺少可確認的工作原文。')
    # An explicit date or relative period needs the normal source-backed path.
    if explicit_source_dates(event_date_text(evidence)) or re.search(
        r'(?:文到|函到|收文|送達|收到(?:本函|本公文)?|收受(?:本函|本公文)?|接獲本函|發文|發函|函發)[^。；;]{0,32}?(?<![0-9年月./-])[0-9零一二兩三四五六七八九十百千壹貳參肆伍陸柒捌玖拾佰仟]+(?:個)?(?:工作日|工作天|曆日|日曆天|日|天|週|星期)',
        unicodedata.normalize('NFKC', evidence),
    ):
        return event
    anchor = receipt_context.for_document(document) if isinstance(receipt_context, ReceiptContext) else None
    source = 'EIP 收文日期' if anchor else '公文發文日期（收文日未確認）'
    anchor = anchor or document.issued.gregorian.replace('/', '-')
    try:
        value = (date.fromisoformat(anchor) + timedelta(days=suggestion['days_after_anchor'])).isoformat()
    except (OverflowError, ValueError) as error:
        raise ValueError('AI 追蹤提醒超出可表示的日期範圍。') from error
    proof = TrackingReminderProof(
        anchor, source, suggestion['days_after_anchor'], value,
        reason, evidence, document.reference_number,
    )
    return {
        **event, 'start_date': value, 'end_date': None, 'tentative_schedule': None,
        '_tracking_reminder_proof': proof,
        'evidence_spans': {**refs, 'start_date': list(spans)},
    }
