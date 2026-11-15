"""Evidence for one calendar duty, independent of generated display wording.

Deterministic adoption is narrow. A possible same-source match can veto a new
write without granting permission to edit, merge, or delete an existing event.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date

from . import identity
from .presentation import split_ai_title

GENERIC_WORK = frozenset(('本案事項', '後續處理', '依規定辦理', '配合辦理', '相關事項', '本案辦理事項', '其他待辦', '辦理本案', '事項追蹤', '公文處理'))
_CJK = r'\u3400-\u9fff，。；：、（）「」『』'
_INSTANCE = re.compile(r'(?:第[0-9一二三四五六七八九十百]+次|\d{2,4}年\s*\d{1,2}月(?:份)?|[A-Za-z]+-?\d+(?:-\d+)?)')
_DATE = r'(?P<year>\d{2,4})\s*(?:年|[./-])\s*(?P<month>\d{1,2})\s*(?:月|[./-])\s*(?P<day>\d{1,2})日?'
_OLD_LABEL = re.compile(r'原訂|原定|原排定|原安排|原預定')
_OLD_DATE = re.compile(r'(?:原訂|原定|原排定|原安排|原預定)(?:於|在|為|時間|日期|召開|辦理|之|的|[：:（(\s])*' + _DATE)


def canonical_source(value):
    value = identity.text(value)
    # Join CJK line wrapping, but preserve Latin word and number boundaries.
    value = re.sub(rf'(?<=[{_CJK}])\s+(?=[{_CJK}0-9])', '', value)
    value = re.sub(rf'(?<=[0-9])\s+(?=[{_CJK}])', '', value)
    return re.sub(r'\s+', ' ', value).strip()


def source(event):
    parts = identity.description(event['description'])
    if parts[0] == 'source':
        return canonical_source(parts[-1])
    value = identity.text(event['description'])
    # A pasted official subject/body can match the generated full quotation.
    # Short notes, filenames and titles alone never constitute source evidence.
    if value.startswith(('主旨：', '主旨:')) and ('說明：' in value or '說明:' in value):
        return canonical_source(value)
    return None


def facts(event):
    parts = identity.description(event['description'])
    result = {}
    if parts[0] == 'source':
        for line in parts[1]:
            key, _, value = line.partition('：')
            result[key] = identity.text(value)
    if event['custom_fields'][3]:
        result.setdefault('辦理人／單位', identity.text(event['custom_fields'][3]))
    return result


def work(event, project=''):
    title = identity.title(event['title'])
    if title[0] == 'ai':
        project = title[2]
        name = title[1]
    else:
        name = identity.work(title[1])
        for left, right in (('「', '」'), ('『', '』'), ('“', '”'), ('"', '"')):
            if project and name.startswith(left + project + right):
                name = project + name[len(project)+2:]
                break
    return identity.text(name.removeprefix(project) if project else name)


def concrete_work(value):
    return len(value) >= 4 and value not in GENERIC_WORK


def compatible_facts(left, right):
    a, b = facts(left), facts(right)
    if any(a.get(k) and b.get(k) and a[k] != b[k] for k in ('工程名稱', '地點', '辦理人／單位')):
        return False
    for index in (2, 3):  # Different notice references alone are not different duties.
        if left['custom_fields'][index] and right['custom_fields'][index] and left['custom_fields'][index] != right['custom_fields'][index]:
            return False
    return True


def same_source_duty(left, right, *, exact_work=True):
    a, b = source(left), source(right)
    if not a or a != b or not compatible_facts(left, right):
        return False
    parsed = [split_ai_title(event['title']) for event in (left, right)]
    projects = [
        identity.text(p[1] or '') if p else facts(event).get('工程名稱', '')
        for p, event in zip(parsed, (left, right))
    ]
    project = projects[0] or projects[1]
    if not project or (all(projects) and projects[0] != projects[1]):
        return False
    # A manual item without a structured project must quote the same project
    # in its full original body; merely sharing a work word is insufficient.
    if any(not p for p in projects) and canonical_source(project) not in a:
        return False
    works = [work(event, project) for event in (left, right)]
    if not all(concrete_work(w) for w in works):
        return False
    instances = [set(_INSTANCE.findall(w)) for w in works]
    if all(instances) and instances[0] != instances[1]:
        return False
    if exact_work:
        return works[0] == works[1]
    return works[0] == works[1] or any(w in other for w, other in ((works[0], works[1]), (works[1], works[0])))


def same_day(left, right):
    return left['start_date'] == left['end_date'] == right['start_date'] == right['end_date']


def clock_compatible(left, right):
    if not same_day(left, right) or left['all_day'] != right['all_day']:
        return False
    if left['all_day'] == '1':
        return True
    if left['start_time'] != right['start_time']:
        return False
    return left['end_time'] == right['end_time'] or all('原文未載明結束時間' in e['description'] for e in (left, right))


def manual_preferred(incoming, existing):
    if split_ai_title(incoming['title']) is None or split_ai_title(existing['title']) is not None:
        return False
    # Identity is proven independently of the clock. A single manual schedule
    # wins even over an explicit AI clock; never edit the manual event.
    return same_event_schedule(incoming, existing)


def same_event_schedule(incoming, existing):
    """Clock-independent identity; callers must separately prove event origin."""
    return same_source_duty(incoming, existing) and same_day(incoming, existing)


def possible_same_duty(incoming, existing):
    return same_day(incoming, existing) and same_source_duty(incoming, existing, exact_work=False)


def stable_event_facts(event):
    title = identity.title(event['title'])
    quoted = source(event)
    if title[0] != 'ai' or not title[2] or not quoted or not event['custom_fields'][0] or not concrete_work(work(event)):
        return None
    # Keep actual assignments, locations, slots and all document fields; only
    # generated prose/formatting is excluded from this durable alias.
    return {
        'contract': 'calendar-source-work-v1', 'project': title[2], 'work': work(event),
        'source_sha256': hashlib.sha256(quoted.encode()).hexdigest(), 'facts': facts(event),
        'dates': [event['start_date'], event['end_date']], 'all_day': event['all_day'],
        'times': [] if event['all_day'] == '1' else [event['start_time'], event['end_time']],
        'custom_fields': [identity.text(value) for value in event['custom_fields']],
    }


def original_schedule_days(event):
    value = source(event) or canonical_source(event['description'])
    if not any(marker in value for marker in ('改期', '延期', '提前', '改訂', '變更日期')):
        return ()
    actual = date.fromisoformat(event['start_date'])
    result = set()
    matches = list(_OLD_DATE.finditer(value))
    if len(matches) != len(_OLD_LABEL.findall(value)):
        raise ValueError('改期來源的原訂日期不完整，請人工核對；未新增行程。')
    for match in matches:
        year, month, day = (int(match[key]) for key in ('year', 'month', 'day'))
        try:
            previous = date(year + 1911 if year < 1911 else year, month, day)
        except ValueError:
            raise ValueError('來源中的原訂日期無效，請先核對；未新增行程。') from None
        if abs((actual-previous).days) > 366:
            raise ValueError('改期跨度超出一年核對範圍，請人工確認；未新增行程。')
        if previous != actual:
            result.add(previous.isoformat())
    if len(result) > 2:
        raise ValueError('來源包含多筆原訂日期，無法唯一核對；未新增行程。')
    return tuple(sorted(result))


def revision_documented(incoming, existing):
    return existing['start_date'] in original_schedule_days(incoming)


def difference_proven(incoming, existing, evidence):
    """Positive differences, not raw spelling/document-number inequality."""
    for proof in evidence:
        for quote, event in ((proof.incoming_quote, incoming), (proof.existing_quote, existing)):
            if not any(quote in event[key] for key in ('title', 'description')):
                raise ValueError('不同事項的差異證據引用不在原文中，未新增。')
    projects = [
        identity.title(event['title'])[2] if split_ai_title(event['title']) else facts(event).get('工程名稱', '')
        for event in (incoming, existing)
    ]
    project = projects[0] or projects[1]
    left_work, right_work = (work(event, project) for event in (incoming, existing))
    if not all(concrete_work(value) for value in (left_work, right_work)):
        return None
    for proof in evidence:
        a, b = facts(incoming), facts(existing)
        if proof.kind == 'work':
            left, right = left_work, right_work
            if (
                all(concrete_work(v) for v in (left, right)) and left != right
                and _INSTANCE.sub('', left) != _INSTANCE.sub('', right)
                and all(v in (source(e) or '') for v, e in ((left, incoming), (right, existing)))
                and left in proof.incoming_quote and right in proof.existing_quote
            ):
                return 'work'
        elif proof.kind == 'instance':
            left, right = (set(_INSTANCE.findall(event['title'])) for event in (incoming, existing))
            if (
                not revision_documented(incoming, existing) and left and right and left.isdisjoint(right)
                and any(v in proof.incoming_quote for v in left) and any(v in proof.existing_quote for v in right)
            ):
                return 'instance'
        elif proof.kind in ('party', 'location'):
            key = '辦理人／單位' if proof.kind == 'party' else '地點'
            if a.get(key) and b.get(key) and a[key] != b[key] and a[key] in proof.incoming_quote and b[key] in proof.existing_quote:
                return proof.kind
    return None
