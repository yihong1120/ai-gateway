"""Evidence-addressed calendar contract, independent of file naming metadata.

The LLM selects events and writes short titles. Code resolves source references,
checks the claimed date/time against those references and builds CSV rows. No
source text is discarded or executed, and no dates are inferred from filenames.
"""
from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from calendar import monthrange
from dataclasses import replace
from datetime import date
from datetime import datetime
from datetime import timedelta

from official_document_workflow.domain.calendar.presentation import clean_ocr_presentation
from official_document_workflow.domain.calendar.presentation import event_description
from official_document_workflow.domain.calendar.presentation import format_ai_title


def resolve_spans(spans, sources, limit=16):
    if spans is None or not 1 <= len(spans) <= limit:
        raise ValueError('行程欄位缺少有效的來源行號。')
    source_lines = {}
    structural_lines = {}
    resolved = []
    ranges = {source_id: [] for source_id in sources}
    for span in spans:
        sid, start, end = (
            span.get('source_id'),
            span.get('start_line'),
            span.get('end_line'),
        )
        if sid not in sources:
            raise ValueError('行程引用未授權或不存在的附件來源。')
        if sid not in source_lines:
            lines = sources[sid].text.splitlines()
            structural = set(getattr(sources[sid], 'structural_lines', ()))
            if any(n is True or n is False or not 1 <= n <= len(lines) for n in structural):
                raise ValueError('行程版面標記來源紀錄無效。')
            source_lines[sid] = lines
            structural_lines[sid] = structural
        lines = source_lines[sid]
        if (
            start is True
            or start is False
            or end is True
            or end is False
            or not 1 <= start <= end
            or end > len(lines) + 1
        ):
            raise ValueError('行程來源行號越界、重疊或順序不符。')
        end = min(end, len(lines))
        ranges[sid].append((start, end))
    merged = []
    for sid, source_ranges in ranges.items():
        for start, end in sorted(source_ranges):
            if merged and merged[-1][0] == sid and start <= merged[-1][2] + 1:
                merged[-1] = (sid, merged[-1][1], max(end, merged[-1][2]))
            else:
                merged.append((sid, start, end))
    for sid, start, end in merged:
        lines = source_lines[sid]
        structural = structural_lines[sid]
        content = []
        for number in range(start, end + 1):
            if number in structural:
                continue
            line = lines[number - 1]
            if line.strip().startswith(
                ('<page', '</page', '<ocr-block', '</ocr-block'),
            ):
                raise ValueError('行程原文包含來源未確認的版面標記，請檢查。')
            content.append(line)
        text = '\n'.join(content).strip()
        if not text:
            raise ValueError('行程來源只有空白或系統標記。')
        resolved.append((sid, start, end, text))
    return resolved


def compact(text):
    return re.sub(
        r'\s+', '', unicodedata.normalize('NFKC', clean_ocr_presentation(text)),
    )


def span_order(span):
    return span['source_id'], span['start_line'], span['end_line']


class EventEvidence:
    def __init__(self, references, sources):
        self.references = references
        self.sources = sources
        self.cache = {}

    def get(self, field):
        if field not in self.cache:
            self.cache[field] = '\n'.join(
                part[3]
                for part in resolve_spans(self.references.get(field), self.sources)
            )
        return self.cache[field]


def literal_source_candidates(events, sources):
    """Locate exact quoted values for a bounded model correction, not approval.

    The model must still choose a passage belonging to the same event. Do not
    fuzzy-match OCR typos, infer missing names, or mutate the original evidence.
    """
    result = []
    search = {}
    for sid, source in sources.items():
        lines = [compact(line) for line in source.text.splitlines()]
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        search[sid] = (''.join(lines), offsets)
    for index, event in enumerate(events or [], 1):
        for field in ('project_name', 'location', 'responsible_party'):
            value = event.get(field)
            if not value or not value.strip():
                continue
            target = compact(value)
            if not target:
                continue
            matches = []
            for sid, (text, offsets) in search.items():
                start = text.find(target)
                while start >= 0 and len(matches) < 3:
                    matches.append(
                        {
                            'source_id': sid,
                            'start_line': bisect_right(offsets, start),
                            'end_line': bisect_right(offsets, start + len(target) - 1),
                        },
                    )
                    start = text.find(target, start + len(target))
            result.append(
                {
                    'event': index,
                    'field': field,
                    'value': value,
                    'exact_source_candidates': matches,
                },
            )
    return result


_TEMPORAL_LINE = re.compile(
    r'(?:民國\s*)?\d{2,4}\s*年\s*\d{1,2}\s*月'
    r'|\d{4}[./-]\d{1,2}[./-]\d{1,2}'
    r'|\d{1,2}\s*月\s*\d{1,2}\s*日'
    r'|(?:上午|下午|晚間|晚上|夜間|凌晨)?\s*\d{1,2}\s*(?:時|點)(?:\s*\d{1,2}\s*分)?'
    r'|\d{1,2}:\d{2}|翌日|次日',
)


def temporal_source_candidates(sources, limit=48):
    """Return a bounded, literal index for the model's completeness audit.

    This is only an attention aid. Calendar semantics and every selected span
    still pass the normal event-specific provenance checks.
    """
    candidates = []
    for source_id, source in sources.items():
        for line_number, line in enumerate(source.text.splitlines(), 1):
            if _TEMPORAL_LINE.search(clean_ocr_presentation(line)):
                candidates.append(
                    {'source_id': source_id, 'line': line_number, 'text': line},
                )
                if len(candidates) == limit:
                    return candidates
    return candidates


def uncovered_time_candidates(events, sources, limit=12):
    """Return nearby explicit clocks not covered by current event intervals.

    This is deliberately only a bounded completeness signal for one model
    correction.  It neither creates a calendar event nor decides that a clock
    is an activity: the correction must still read the source, classify it and
    provide normal event-specific evidence.  Restricting the scan to the
    vicinity of an already selected timed event avoids treating remote history
    elsewhere in a long attachment as a missing current schedule.
    """
    known = []
    nearby = set()
    has_current = False
    for event in events:
        if event.get('status') != 'current':
            continue
        has_current = True
        for field in ('start_time', 'end_time'):
            if event.get(field):
                known.append(event[field])
        refs = event.get('evidence_spans') or {}
        for field in ('start_date', 'start_time', 'end_date', 'end_time', 'relevance'):
            for span in refs.get(field) or ():
                start, end = int(span['start_line']), int(span['end_line'])
                nearby.update(
                    (span['source_id'], line)
                    for line in range(max(1, start - 6), end + 7)
                )
    if not has_current or known and not nearby:
        return []
    result = []
    for source_id, source in sources.items():
        for line_number, line in enumerate(source.text.splitlines(), 1):
            if known and (source_id, line_number) not in nearby:
                continue
            readable = clean_ocr_presentation(line)
            for token, value in clock_values(readable):
                if value not in known:
                    result.append(
                        {
                            'source_id': source_id,
                            'line': line_number,
                            'time': token,
                            'text': line,
                        },
                    )
                    if len(result) >= limit:
                        return result
    return result


def context_spans(description_spans, refs, sources):
    """Union selected narrative with every validated fact's source passage.

    A model selecting only formal remarks must not omit a meeting header from
    the persisted detail. Merge only overlapping/adjacent ranges, never fill
    unselected gaps, and preserve original order within each source.
    """
    # Validate the model-owned narrative range with the normal strict bound.
    resolve_spans(description_spans, sources)
    selected = list(description_spans)
    for spans in refs.values():
        if spans:
            selected.extend(spans)
    # A broad narrative range often already contains the individual fact
    # ranges.  Collapse overlaps before applying resolve_spans' bounded input
    # contract, so repeated evidence fields do not turn one passage into an
    # artificial "too many spans" failure.
    merged = []
    for span in sorted(selected, key=span_order):
        if (
            merged
            and merged[-1]['source_id'] == span['source_id']
            and span['start_line'] <= merged[-1]['end_line'] + 1
        ):
            merged[-1]['end_line'] = max(merged[-1]['end_line'], span['end_line'])
        else:
            merged.append(dict(span))
    # Each fact range was already validated independently by EventEvidence;
    # this larger bound only covers their union for the persisted description.
    return resolve_spans(merged, sources, 256)


def verify_date(value: str | None, evidence):
    """Check normalized model values, not search the corpus for event candidates."""
    if value is None or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return False
    year, month, day = map(int, value.split('-'))
    if not (
        1 <= year <= 9999
        and 1 <= month <= 12
        and 1 <= day <= monthrange(year, month)[1]
    ):
        return False
    date = datetime.strptime(value, '%Y-%m-%d')
    if date.strftime('%Y-%m-%d') != value:
        return False
    # Explicit full dates only. Ambiguous/OCR-damaged dates require review.
    for y, m, d in re.findall(
        r'(?<!\d)(\d{2,4})[年/.-](\d{1,2})[月/.-](\d{1,2})(?:日|號)?(?!\d)',
        compact(evidence),
    ):
        year = int(y) + (1911 if int(y) < 1911 else 0)
        if (year, int(m), int(d)) == (date.year, date.month, date.day):
            return True
    return False


def explicit_source_dates(evidence):
    """Return valid full dates literally present outside administrative rows."""
    dates = set()
    for y, m, d in re.findall(
        r'(?<!\d)(\d{2,4})[年/.-](\d{1,2})[月/.-](\d{1,2})(?:日|號)?(?!\d)',
        compact(event_date_text(evidence)),
    ):
        value = date(int(y) + (1911 if int(y) < 1911 else 0), int(m), int(d))
        dates.add(value.isoformat())
    return dates


def verify_time(value: str | None, evidence):
    if value is None or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        return False
    return value in {normalized for _, normalized in clock_values(evidence)}


def clock_values(evidence):
    """Return literal clock tokens with inherited periods normalized once."""
    pattern = r'(上午|下午|中午|晚上|晚間|凌晨|早上)?(\d{1,2})(?:[:：](\d{2})|[時點](?:(\d{1,2})分?|整)?)(?!\d)'
    text = compact(evidence)
    previous = None
    values = []
    for match in re.finditer(pattern, text):
        period, hour, colon_minute, chinese_minute = match.groups()
        hour, minute = int(hour), int(colon_minute or chinese_minute or 0)
        inherited = False
        if (
            not period
            and previous
            and hour < 12
            and text[previous[0]: match.start()]
            in ('至', '到', '~', '～', '-', '－', '–', '—')
        ):
            period = previous[1]
            inherited = bool(period)
        if period in ('下午', '晚上', '晚間') and hour < 12:
            hour += 12
        elif period in ('上午', '凌晨', '早上') and hour == 12:
            hour = 0
        elif period == '中午' and hour < 11:
            hour += 12
        # Share a period only within an adjacent forward interval, never
        # across clauses or unrelated clocks. Explicit periods take priority.
        valid = (
            hour < 24
            and minute < 60
            and (not inherited or (hour, minute) >= previous[2])
        )
        if valid:
            values.append((match.group(), f"{hour:02d}:{minute:02d}"))
        previous = (match.end(), period, (hour, minute)) if valid else None
    return values


def _schedule_context_years(source_texts):
    """Return the unambiguous ROC/Gregorian years of referenced schedule sources.

    A published schedule commonly puts the year only in its table caption and
    writes each row as ``9月16日``.  That is different from inferring a due
    date from a document's issue date: the row still supplies its own month and
    day, and the same referenced source must explicitly identify itself as a
    dated schedule.  Do not use a general document subject or an unrelated
    attachment as an anchor.
    """
    years = set()
    for source_text in source_texts or ():
        text = clean_ocr_presentation(source_text)
        matches = re.findall(
            r'(?<!\d)(\d{2,4})年\s*\d{1,2}月(?:份)?[^\n。]{0,48}(?:預定)?(?:排程|行程表|時程表)',
            text,
        )
        years.update(int(year) + (1911 if int(year) < 1911 else 0) for year in matches)
    return years


def event_date_text(evidence):
    """Exclude administrative timestamps, preserving actual event clauses."""
    lines = clean_ocr_presentation(evidence).splitlines()
    eligible = []
    header_pending = False
    for line in lines:
        if re.search(r'(發文日期|收文日期|文件匣紀錄時間|列印時間)\s*[:：]', line):
            header_pending = not bool(
                re.search(r'\d', line.split('：')[-1].split(':')[-1]),
            )
            continue
        if header_pending and line.strip():
            header_pending = False
            continue
        eligible.append(line)
    return '\n'.join(eligible)


def verify_event_date(value, evidence, source_texts=()):
    """Check event dates without treating administrative timestamps as proof."""
    eligible_text = event_date_text(evidence)
    if verify_date(value, eligible_text):
        return True

    # A row in a dated schedule may deliberately omit the repeated year.  The
    # event's own evidence must still literally contain its month/day, and the
    # source(s) referenced by that field must have exactly one dated schedule
    # year.  A schedule may cross a month boundary, so the caption month is
    # not used to overwrite the row's explicit month.
    target = date.fromisoformat(value)
    for month, day in re.findall(
        r'(?<!\d)(\d{1,2})月\s*(\d{1,2})(?:日|號)?(?!\d)', compact(eligible_text),
    ):
        if (int(month), int(day)) != (target.month, target.day):
            continue
        if _schedule_context_years(source_texts) == {target.year}:
            return True
    return False


def verify_next_day_end(value, evidence, event, source_texts=()):
    """Verify a claimed next-day end against one explicit dated interval.

    This checks a model claim, never supplies a missing date or receipt anchor.
    The same passage must support the start date and both clocks; unrelated
    dates or a next-day word elsewhere cannot justify the arithmetic.
    """
    start = event.get('start_date')
    if not verify_event_date(start, evidence, source_texts):
        return False
    if date.fromisoformat(value) != date.fromisoformat(start) + timedelta(days=1):
        return False
    clock = r'(?:上午|下午|中午|晚上|晚間|凌晨|早上)?\d{1,2}(?:[:：]\d{2}|[時點](?:\d{1,2}分?|整)?)'
    pattern = rf"({clock})(?:至|到|[~～－–—-])(?:翌日|次日|隔日)({clock})(?!\d)"
    return any(
        verify_time(event.get('start_time'), match[1])
        and verify_time(event.get('end_time'), match[2])
        for match in re.finditer(pattern, compact(evidence))
    )


def invalid_event_interval(event):
    start_date, end_date = event.get('start_date'), event.get('end_date')
    start_time, end_time = event.get('start_time'), event.get('end_time')
    if start_date and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', start_date):
        return True
    if end_date and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', end_date):
        return True
    if start_time and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', start_time):
        return True
    if end_time and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', end_time):
        return True
    if start_date and end_date and end_date < start_date:
        return True
    same_day = not end_date or end_date == start_date
    return bool(same_day and start_time and end_time and end_time <= start_time)


def partition_calendar_events(pipe, module, payload, sources, document, accepted):
    """Validate calendar candidates without blocking document exports."""
    events = payload.get('events')
    if len(events) > pipe.valves.cloud_max_events:
        raise ValueError('行程陣列格式或數量不符。')
    valid = []
    schedulable = []
    pending = []
    for index, event in enumerate(events, 1):
        candidate = optional_event_fields(
            assignment_event(event, sources, payload.get('primary_source_id')), sources,
        )
        if candidate.get('status') in ('current', 'needs_review') and candidate.get(
            'start_date',
        ) in (None, ''):
            pending.append(
                f"行程 {index} 日期待補：沒有可確認日期；請提供明確辦理日期，"
                '若為文到後期限，須確認實際送達日。未以發文日或 EIP 文件匣時間代填，未自動匯入。',
            )
        elif candidate.get('status') == 'current' and invalid_event_interval(candidate):
            pending.append(
                f"行程 {index} 起訖待核對：結束時間早於或等於起始時間，或日期時間格式無效；未自動匯入。",
            )
        else:
            schedulable.append(candidate)
        valid.append(candidate)
    deadlines, rows = calendar_events(
        pipe, module, {**payload, 'events': schedulable}, sources, document,
    )
    accepted.extend(e for e in schedulable if e.get('status') == 'current')
    return deadlines, rows, pending


def assignment_event(event, sources, primary_source_id=None):
    """Validate source relations, retaining unknown timing instead of guessed dates.

    Legacy candidates without assignment still pass the existing evidence and
    independent semantic checks. The original model payload is never mutated.
    This does not infer an executor or calculate a relative deadline.
    """
    candidate = event
    if (
        event.get('status') == 'historical'
        and event.get('kind') == 'project_milestone'
        and event.get('start_date')
        and any(
            span.get('source_id') == primary_source_id
            for span in (event.get('evidence_spans') or {}).get('relevance') or ()
        )
    ):
        candidate = {**event, 'status': 'current'}
    if candidate.get('status') != 'current' or 'assignment' not in candidate:
        return candidate
    relation = candidate['assignment']
    if relation.get('date_basis') not in ('explicit', 'relative', 'unspecified'):
        raise ValueError('行程 1 的來源關係格式無效。')
    candidate = candidate.copy()
    refs = {field: list(spans) for field, spans in (candidate.get('evidence_spans') or {}).items()}
    candidate['evidence_spans'] = refs
    executor = relation.get('executor')
    if executor != candidate.get('responsible_party'):
        raise ValueError('行程 1 的辦理責任與來源關係不一致，請核對。')
    anchor = relation.get('anchor_date')
    if relation['date_basis'] == 'relative' and anchor:
        evidence = '\n'.join(
            p[3] for p in resolve_spans(refs.get('anchor_date'), sources)
        )
        if not verify_date(anchor, evidence):
            raise ValueError('行程 1 的起算日期缺少逐字來源，請核對。')
    # Inspect the cited source, not a second model-written transcription of it.
    # Missing/invalid references still fail; an actual conflicting date remains
    # subject to verify_event_date. Relative-only source text stays pending.
    has_calendar_day = False
    if relation['date_basis'] == 'explicit':
        if (
            not candidate.get('start_date')
            and candidate.get('end_date')
            and not candidate.get('start_time')
            and refs.get('end_date')
        ):
            end_proof = '\n'.join(
                p[3] for p in resolve_spans(refs['end_date'], sources)
            )
            if verify_event_date(
                candidate['end_date'],
                end_proof,
                [source.text for source in sources.values()],
            ):
                candidate['start_date'] = candidate['end_date']
                refs['start_date'] = list(refs['end_date'])
        if not candidate.get('start_date'):
            candidate.update(
                status='needs_review',
                start_date=None,
                start_time=None,
                end_date=None,
                end_time=None,
            )
            return candidate
        proof = '\n'.join(p[3] for p in resolve_spans(refs.get('start_date'), sources))
        # A single literal full date makes ROC/Gregorian conversion
        # deterministic. Correct only a supplied model value; never create a
        # missing date or choose between multiple source dates.
        supplied = candidate.get('start_date')
        source_dates = explicit_source_dates(proof)
        if (
            supplied
            and not verify_event_date(
                supplied, proof, [source.text for source in sources.values()],
            )
            and len(source_dates) == 1
        ):
            corrected = next(iter(source_dates))
            candidate['start_date'] = corrected
            if candidate.get('end_date') == supplied:
                candidate['end_date'] = corrected
        has_calendar_day = bool(
            re.search(r'\d{1,4}[年/月.\-]\d{1,2}', compact(event_date_text(proof))),
        )
    if (
        relation['date_basis'] == 'unspecified'
        or relation['date_basis'] == 'relative'
        and not anchor
        or relation['date_basis'] == 'explicit'
        and not has_calendar_day
    ):
        candidate.update(
            status='needs_review',
            start_date=None,
            start_time=None,
            end_date=None,
            end_time=None,
        )
        return candidate
    return candidate


def optional_event_fields(event, sources):
    """Unproven optional labels become unknown, never invented assignments.

    Invalid references still fail; the full source text remains in the narrative.
    Do not apply this to dates, actions, or project identity.
    """
    if event.get('status') != 'current':
        return event
    candidate = event.copy()
    if candidate.get('assignment') is not None:
        candidate['assignment'] = candidate['assignment'].copy()
    raw_refs = event.get('evidence_spans')
    if raw_refs is None:
        return candidate
    refs = {field: list(spans) for field, spans in raw_refs.items()}
    candidate['evidence_spans'] = refs
    if refs.get('title') in (None, []):
        refs['title'] = list(refs.get('action') or ())
    # A same-day end is the same calendar fact, not a second date to infer.
    # Explicit, conflicting end evidence is still validated independently.
    if candidate.get('end_date') == candidate.get('start_date'):
        end_refs = refs.get('end_date') or []
        end_proof = '\n'.join(p[3] for p in resolve_spans(end_refs, sources)) if end_refs else ''
        if not explicit_source_dates(end_proof):
            combined = {
                (span['source_id'], span['start_line'], span['end_line']): span
                for span in [*(refs.get('start_date') or ()), *end_refs]
            }
            refs['end_date'] = list(combined.values())
    project_matches = next(
        (
            item['exact_source_candidates']
            for item in literal_source_candidates([candidate], sources)
            if item['field'] == 'project_name'
        ),
        [],
    )
    if len(project_matches) == 1:
        refs['project_name'] = project_matches
    notices = []
    for field, label in (('responsible_party', '負責人／單位'), ('location', '地點')):
        value = candidate.get(field)
        if value in (None, '', '未載明'):
            continue
        proof = '\n'.join(p[3] for p in resolve_spans(refs.get(field), sources))
        action_proof = (
            '\n'.join(p[3] for p in resolve_spans(refs.get('action'), sources))
            if field == 'responsible_party'
            else ''
        )
        valid = len(value) <= 300 and compact(value) in compact(proof)
        if field == 'responsible_party' and valid:
            valid = compact(value) in compact(action_proof)
            if valid:
                refs[field] = list(refs.get('action') or ())
        if not valid:
            candidate[field] = None
            refs[field] = []
            if field == 'responsible_party' and candidate.get('assignment') is not None:
                candidate['assignment']['executor'] = None
            notices.append(label + '未能逐字核對，未自動填入；請依下方原文確認。')
    candidate['_field_notices'] = notices
    return candidate


def calendar_events(pipe, module, payload, sources, document):
    events = payload.get('events')
    if len(events) > pipe.valves.cloud_max_events:
        raise ValueError('行程陣列格式或數量不符。')
    deadlines, rows, seen = [], [], {}
    for index, event in enumerate(events, 1):
        refs = event.get('evidence_spans')
        if refs is None:
            raise ValueError(f"行程 {index} 缺少欄位來源。")

        evidence = EventEvidence(refs, sources)
        evidence.get('relevance')
        status = event.get('status')
        if status in ('historical', 'cancelled', 'out_of_scope'):
            continue
        if status in ('current', 'needs_review') and event.get('start_date') in (
            None,
            '',
        ):
            raise ValueError(
                f"行程 {index} 日期待補：沒有可確認日期；請提供明確辦理日期，"
                '若為文到後期限，須確認實際送達日。未以發文日或 EIP 文件匣時間代填，未自動匯入。',
            )
        if status != 'current':
            raise ValueError(f"行程 {index} 日期或附件關係待確認，未自動匯入。")
        title, action = event.get('title') or '', event.get('action') or ''
        if (
            not title.strip()
            or len(title.encode('utf-8')) > 200
            or any(ord(c) < 32 for c in title)
            or title.rstrip().endswith(('…', '...'))
        ):
            raise ValueError(f"行程 {index} 精簡事項名稱無效或超過 EIP 長度限制。")
        if not action.strip():
            raise ValueError(f"行程 {index} 缺少具體工作。")
        if len(action) > 120:
            raise ValueError(
                f"行程 {index} 工作摘要超過 120 字；請精簡 action 並保留完整原文。",
            )
        evidence.get('title')
        action_evidence = evidence.get('action')
        # Do not turn an explicitly overnight interval into the default 60 min
        # reservation just because the model omitted its end fields.
        time_context = action_evidence
        for field in ('start_date', 'start_time'):
            if event.get(field):
                time_context += '\n' + evidence.get(field)
        if (
            event.get('start_time')
            and re.search(
                r'(翌日|次日|隔日)(?:上午|下午|中午|晚上|晚間|凌晨|早上)?\d{1,2}[時點:：]',
                compact(time_context),
            )
            and (not event.get('end_date') or not event.get('end_time'))
        ):
            raise ValueError('行程原文明載跨日結束時間，但回應未提供完整起訖。')
        for field in ('start_date', 'start_time', 'end_date', 'end_time'):
            value = event.get(field)
            if value in (None, ''):
                continue
            parts = resolve_spans(refs.get(field), sources)
            if field.endswith('date'):
                verified = verify_event_date(
                    value,
                    '\n'.join(p[3] for p in parts),
                    [sources[p[0]].text for p in parts],
                )
                if not verified and field == 'end_date':
                    verified = any(
                        verify_next_day_end(value, p[3], event, [sources[p[0]].text])
                        for p in parts
                    )
            else:
                verified = verify_time(value, '\n'.join(p[3] for p in parts))
            if not verified:
                raise ValueError(
                    f"行程 {index} 的 {field} 與所引原文不符或日期格式待確認。",
                )
        for field in ('location', 'responsible_party'):
            value = event.get(field)
            if value not in (None, '', '未載明'):
                if len(value) > 300 or compact(value) not in compact(
                    evidence.get(field),
                ):
                    raise ValueError(f"行程 {index} 的 {field} 缺少逐字來源。")
        project = event.get('project_name')
        if project not in (None, '', '未載明'):
            if len(project) > 200 or compact(project) not in compact(
                evidence.get('project_name'),
            ):
                raise ValueError(f"行程 {index} 的工地名稱缺少逐字來源。")
            project = clean_ocr_presentation(project)
        else:
            project = None
        # Existing parser handles date ordering and display formats. Do not let
        # its legacy optional-end fallback silently drop an invalid explicit end.
        legacy = {**event, 'evidence': action_evidence[:650]}
        deadline = pipe._cloud_deadline_from_event(
            legacy, pipe._normalise_text(action_evidence),
        )
        if deadline is None:
            raise ValueError(f"行程 {index} 起訖資料無效。")
        if (event.get('end_date') and deadline.end_date is None) or (
            event.get('end_time') and deadline.end_time is None
        ):
            raise ValueError(f"行程 {index} 結束時間早於起始時間或區間無效。")
        parts = context_spans(event.get('description_spans'), refs, sources)
        title = clean_ocr_presentation(title)
        # The new prompt prohibits project prefixes. Exact, verified project
        # prefixes from old model responses are removable without guessing.
        if '｜' in title or '|' in title:
            prefix, _, rest = re.split(r'([｜|])', title, maxsplit=1)
            if project and compact(prefix) == compact(project) and rest.strip():
                title = rest.strip()
            else:
                raise ValueError(
                    f"行程 {index} 名稱含未確認的專案前綴，請核對；未自動猜測簡稱。",
                )
        description = event_description(
            title,
            project,
            event,
            parts,
            payload.get('primary_source_id') or next(iter(sources)),
        )
        if event.get('_field_notices'):
            description = '\n'.join(event['_field_notices']) + '\n\n' + description
        formatted_title, title_notice = format_ai_title(title, project)
        if title_notice:
            description = title_notice + '\n\n' + description
        if project is None:
            description = (
                '工地名稱未能從文件來源確認，暫標示「工地未確認」；未猜測工地。\n\n'
                + description
            )
        deadline = replace(
            deadline,
            calendar_title=formatted_title,
            calendar_description=description,
            private='真',
        )
        identity = (
            deadline.date.gregorian,
            deadline.time.twenty_four_hour if deadline.time else '',
            compact(action),
            compact(event.get('location') or ''),
        )
        timing = (
            deadline.end_date.gregorian if deadline.end_date else '',
            deadline.end_time.twenty_four_hour if deadline.end_time else '',
        )
        if identity in seen:
            if seen[identity] != timing:
                raise ValueError(f"行程 {index} 與同一事件的結束時間衝突，未自動匯入。")
            continue
        seen[identity] = timing
        deadlines.append(deadline)
        rows.extend(
            pipe._calendar_rows(
                owner_name=pipe._calendar_owner_name(document) if document else '',
                document=document,
                deadlines=[deadline],
            ),
        )
    return deadlines, rows
