"""Auditable EIP tracking-date estimates, independent of model arithmetic.

Only an established obligation with a quoted relative period can acquire a
date here. An issue date used instead of delivery is always an estimate.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from datetime import datetime
from datetime import timedelta
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter

from .contracts import RelativeDeadline


ESTIMATE_TITLE = '【期限推估】'
ESTIMATE_NOTICE = 'AI 推估期限（供追蹤提醒，待核對）'


def normalized(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value or ''))


def form_date(value):
    match = re.fullmatch(r'(\d{2,4})年(\d{1,2})月(\d{1,2})日', normalized(value))
    if match is None:
        return None
    year, month, day = map(int, match.groups())
    try:
        return date(year + (1911 if year < 1911 else 0), month, day).isoformat()
    except ValueError:
        return None


@dataclass(frozen=True)
class ReceiptContext:
    flow_id: str
    reference_number: str
    received_date: str | None

    @classmethod
    def from_detail(cls, flow_id, detail):
        fields = {r['label']: r['value'] for r in detail.get('fields', [])}
        return cls(flow_id, fields.get('發文字號', ''), form_date(fields.get('收文日期')))

    def record(self):
        return {'flow_id': self.flow_id, 'reference_number': self.reference_number,
                'received_date': self.received_date, 'source': 'EIP 收文日期'}

    def for_document(self, document):
        if not self.received_date or not self.reference_number:
            return None
        if normalized(self.reference_number) != normalized(document.reference_number):
            return None
        value = date.fromisoformat(self.received_date)
        if value < date.fromisoformat(document.issued.gregorian.replace('/', '-')) or value > datetime.now(ZoneInfo('Asia/Taipei')).date():
            return None
        return self.received_date


def integer(value):
    value = value.translate(str.maketrans('壹貳參肆伍陸柒捌玖拾佰仟', '一二三四五六七八九十百千'))
    if value.isdecimal():
        return int(value)
    digits = {'零': 0, '一': 1, '二': 2, '兩': 2, '三': 3, '四': 4,
              '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
    total = current = 0
    for char in value:
        if char in digits:
            current = digits[char]
        elif char in ('十', '百', '千'):
            total += (current or 1) * {'十': 10, '百': 100, '千': 1000}[char]
            current = 0
        else:
            raise ValueError('相對期限的天數無法核對。')
    return total + current


def period_matches(evidence, spec):
    """Check only the quoted period selected for this work, not the corpus."""
    anchor = (r'(?:文到|函到|收文|送達|收到(?:本函|本公文)?|收受(?:本函|本公文)?|接獲本函)'
              if spec['anchor'] == 'receipt' else r'(?:發文|發函|函發)')
    pattern = anchor + r'[^。；;]{0,32}?(?<![0-9年月./-])([0-9零一二兩三四五六七八九十百千壹貳參肆伍陸柒捌玖拾佰仟]+)(?:個)?(工作日|工作天|曆日|日曆天|日|天|週|星期)'
    periods = set()
    for match in re.finditer(pattern, normalized(evidence)):
        count = integer(match[1])
        unit = match[2]
        periods.add((count * (7 if unit in ('週', '星期') else 1),
                     'working_days' if unit in ('工作日', '工作天') else 'calendar_days'))
    if periods != {(spec['days'], spec['day_type'])}:
        return False
    inclusive = bool(re.search(r'(?<!不)含(?:當|收文|發文)日|當日計入', normalized(evidence)))
    return spec['include_anchor_day'] == inclusive


def add_days(anchor, spec):
    current = date.fromisoformat(anchor)
    remaining = spec['days']
    if spec['include_anchor_day'] and (spec['day_type'] == 'calendar_days' or current.weekday() < 5):
        remaining -= 1
    while remaining:
        current += timedelta(days=1)
        if spec['day_type'] == 'calendar_days' or current.weekday() < 5:
            remaining -= 1
    return current.isoformat()


@dataclass(frozen=True)
class RelativeDateProof:
    anchor_date: str
    anchor_source: str
    spec_items: tuple
    deadline: str
    reference_number: str
    source_digest: str

    @property
    def spec(self):
        return dict(self.spec_items)

    def supports(self, value, evidence):
        return value == self.deadline and period_matches(evidence, self.spec)

    def audit(self):
        return {'estimated': True, 'anchor_date': self.anchor_date,
                'anchor_source': self.anchor_source, 'deadline': self.deadline,
                'receipt_date_recorded': self.anchor_source == 'EIP 收文日期',
                'holiday_rules_verified': False,
                'reference_number': self.reference_number, **self.spec}

    def notice(self):
        spec = self.spec
        rule = '含起算日' if spec['include_anchor_day'] else '次日起算'
        unit = '個工作日' if spec['day_type'] == 'working_days' else '個曆日'
        caveat = ('工作日僅排除週末，公司假日與補班待核對。' if spec['day_type'] == 'working_days'
                  else '依曆日試算，假日順延待核對。')
        return '\n'.join([ESTIMATE_NOTICE, f'起算依據：{self.anchor_source} {self.anchor_date}',
                          f'計算方式：{rule} {spec["days"]} {unit} → {self.deadline}', caveat])


def estimate_event(event, sources, document, receipt_context=None):
    # No promotion of cancelled, unapproved, unrelated or ambiguous obligations.
    if event.get('status') != 'current':
        return event
    relation = event.get('assignment') or {}
    if relation.get('date_basis') != 'relative' or relation.get('relative_deadline') is None:
        return event
    try:
        spec = TypeAdapter(RelativeDeadline).validate_python(relation['relative_deadline'])
    except ValueError as error:
        raise ValueError('相對期限推估欄位格式不完整。') from error
    from .calendar_semantics import resolve_spans

    refs = event.get('evidence_spans') or {}
    spans = refs.get('start_date') or refs.get('action')
    evidence = '\n'.join(part[3] for part in resolve_spans(spans, sources))
    if not period_matches(evidence, spec):
        raise ValueError('相對期限的天數、起算條件或計日方式與原文不符。')
    supplied = event.get('start_date')
    if event.get('end_date') not in (None, '', supplied):
        raise ValueError('相對期限推估不能取代原文明示的持續區間。')
    anchor = None
    if spec['anchor'] == 'receipt' and isinstance(receipt_context, ReceiptContext):
        anchor = receipt_context.for_document(document)
    if anchor:
        source = 'EIP 收文日期'
    else:
        anchor = document.issued.gregorian.replace('/', '-')
        source = '公文發文日期' + ('（暫代送達日，實際送達日待確認）' if spec['anchor'] == 'receipt' else '')
    deadline = add_days(anchor, spec)
    proof = RelativeDateProof(anchor, source, tuple(spec.items()), deadline,
                              document.reference_number, hashlib.sha256(evidence.encode()).hexdigest())
    updated = {**event, 'start_date': deadline, 'end_date': deadline if event.get('end_date') else None,
               'tentative_schedule': None, '_relative_date_proof': proof,
               'evidence_spans': {**refs, 'start_date': list(spans)}}
    updated['assignment'] = {**relation, 'anchor_date': anchor}
    return updated
