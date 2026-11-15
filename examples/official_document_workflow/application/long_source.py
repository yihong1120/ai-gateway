"""Complete long-source reading with provenance-preserving evidence selection.

The model may select passages, never rewrite them. Every input fragment must
be acknowledged before synthesis; final validation still uses the full source.
Per-request context budgets are not total file/text acceptance limits.
"""
from __future__ import annotations

from examples.official_document_workflow.domain import documents as module

import json
import re
from functools import partial

from .prompts import SCAN_PROMPT
from examples.official_document_workflow.services.analysis_reuse import analyze_content
from examples.official_document_workflow.domain.calendar.contracts import EVIDENCE_SCAN_SCHEMA
from examples.official_document_workflow.domain.calendar.contracts import require_tentative_schedule_decision
from examples.official_document_workflow.domain.validation import ContentValidationError


def evidence_selection(answer, ids):
    """A cached selector must acknowledge exactly the same complete fragments."""
    if (
        not isinstance(answer, dict)
        or answer.get('uncertain') is not False
        or type(answer.get('read_count')) is not int
        or answer['read_count'] != len(ids)
        or answer.get('first_id') != ids[0]
        or answer.get('last_id') != ids[-1]
        or not isinstance(answer.get('keep_ranges'), list)
    ):
        raise ContentValidationError('source_reading_unverified')
    selected, previous = set(), 0
    for span in answer['keep_ranges']:
        if (
            not isinstance(span, list) or len(span) != 2
            or any(type(value) is not int for value in span)
            or not previous < span[0] <= span[1] <= len(ids)
        ):
            raise ContentValidationError('source_reading_unverified')
        selected.update(range(span[0] - 1, span[1]))
        previous = span[1]
    return selected


def source_lines(source):
    """Omit empty/layout-only entries without changing document line numbers.

    Sparse line numbers retain block boundaries. Page markers, all document
    text and full originals used by evidence validation remain intact.
    """
    return [
        (n, line) for n, line in enumerate(source.text.splitlines(), 1)
        if line.strip() and not (
            n in source.structural_lines and line.strip() in {'<ocr-block>', '</ocr-block>'}
        )
    ]


def windows(sources, budget):
    if budget < 2000:
        raise ValueError('單次模型分段設定過小，請由管理員修正；未截斷原文。')
    current = []
    size = 0
    # Fragment very long OCR lines as well. IDs retain exact character offsets
    # into the ORIGINAL line; a line is never shortened to fit a request.
    for sid, source in sources.items():
        for number, line in source_lines(source):
            offset = 0
            while offset < len(line) or (not line and offset == 0):
                end = min(len(line), offset + max(1, budget // 4))
                item = {
                    'id': f"{sid}:{number}:{offset}:{end}",
                    'source_id': sid,
                    'line': number,
                    'start': offset,
                    'end': end,
                    'text': line[offset:end],
                }
                cost = len(json.dumps(item, ensure_ascii=False, separators=(',', ':'))) + 1
                if current and size + cost > budget - 512:
                    yield current
                    current, size = [], 0
                if cost > budget - 512:
                    raise ValueError('單次模型分段設定無法容納來源座標，未截斷原文。')
                current.append(item)
                size += cost
                if end == len(line):
                    break
                offset = end
    if current:
        yield current


async def group_text(pipe,  members, sources, emitter=None, *, allow_chunking=False):
    budget = pipe.valves.cloud_ocr_text_max_input_characters
    source_size = sum(len(source.text) for source in sources.values())
    if source_size <= budget:
        full = {
            'sources': [
                {
                    'source_id': sid,
                    'name': p.source_file.filename,
                    'lines': source_lines(sources[sid]),
                }
                for sid, p in zip(sources, members, strict=True)
            ],
        }
        text = json.dumps(full, ensure_ascii=False, separators=(',', ':'))
        if len(text) <= budget * 2 + 4096:
            return text, {'mode': 'complete', 'windows': 0}
    if not (allow_chunking or pipe.valves.eip_large_sources):
        raise ValueError('整組原文超過雲端輸入上限，未截斷送出；請啟用完整分段處理。')
    kept = []
    count = 0
    protected = set()
    for sid, source in sources.items():
        lines = source.text.splitlines()
        if len(source.text) <= budget // 4:
            protected.update((sid, n) for n in range(1, len(lines) + 1))
        else:
            for n, line in enumerate(lines, 1):
                # Retrieval safeguard, NOT event creation/date interpretation:
                # retain all date-looking passages (including issue/history)
                # and neighbouring context even when the selector misses them.
                if re.search(
                    r'\d{2,4}[年/.-]\d{1,2}[月/.-]\d{1,2}|文到|期限|取消|延期|翌日|次日|一週內',
                    line,
                ):
                    protected.update((sid, i) for i in range(max(1, n - 3), min(len(lines), n + 3) + 1))
    # These passages cannot be removed by the selector. If they alone exceed
    # the final budget, paid window calls cannot make this group fit.
    protected_size = sum(
        len(line) for sid, source in sources.items()
        for n, line in source_lines(source)
        if (sid, n) in protected
    )
    if protected_size > budget:
        raise ContentValidationError('source_evidence_capacity_exceeded')
    window_budget = min(budget, pipe.valves.eip_source_window_characters)
    for count, chunk in enumerate(windows(sources, window_budget), 1):
        await pipe.emit_status(emitter, f"長文件完整分段判讀：第 {count} 段；保留原始來源座標")
        request_text = json.dumps(
            {
                'manifest': {
                    'fragment_count': len(chunk),
                    'first_id': chunk[0]['id'],
                    'last_id': chunk[-1]['id'],
                },
                'fragments': chunk,
            },
            ensure_ascii=False,
            separators=(',', ':'),
        )
        ids = [item['id'] for item in chunk]

        async def scan(text=request_text, fragment_ids=ids):
            answer = await analyze_content(
                pipe,
                module.CloudSemanticSource(text, 'paddle_layout_ocr'),
                stage='source-evidence',
                prompt=SCAN_PROMPT,
                schema=EVIDENCE_SCAN_SCHEMA,
            )
            evidence_selection(answer, fragment_ids)
            return answer

        shared = pipe.evidence_cache
        if shared is not None:
            # This selector sees only exact source fragments, not receipt dates,
            # today's preparation plan, owner prompts or existing calendar data.
            key = shared.key(
                'evidence-index-v2', request_text, SCAN_PROMPT,
                require_tentative_schedule_decision(EVIDENCE_SCAN_SCHEMA),
                *pipe.transport.content_identity(),
            )
            answer = await shared.aget_or_compute(
                key, scan, validate=partial(evidence_selection, ids=ids), replay_safe=False,
            )
        else:
            # A billed adapter may opt out of sharing its model traffic while
            # still reusing the original owner-isolated checkpoint contract.
            cache = getattr(pipe, '_eip_processing_cache', None)
            key = cache.key(
                'evidence-index-v1', request_text, SCAN_PROMPT,
                pipe.valves.cloud_ocr_text_model, pipe.valves.cloud_ocr_text_max_output_tokens,
            ) if cache else None
            answer = cache.get(key) if cache else None
            if answer is None:
                answer = await scan()
            evidence_selection(answer, ids)
            if cache:
                cache.put(key, answer)
        selected = evidence_selection(answer, ids)
        kept.extend(
            item for i, item in enumerate(chunk) if i in selected or (item['source_id'], item['line']) in protected
        )
    # Reassemble contiguous selected fragments without inserting generated text.
    # For partial very long lines send fragments with original line/offset labels.
    selected_sources = []
    for sid, p in zip(sources, members, strict=True):
        fragments = [item for item in kept if item['source_id'] == sid]
        entries = []
        for item in fragments:
            if entries and entries[-1][0] == item['line'] and entries[-1][3] == item['start']:
                entries[-1][1] += item['text']
                entries[-1][3] = item['end']
            else:
                entries.append([item['line'], item['text'], item['start'], item['end']])
        selected_sources.append(
            {
                'source_id': sid,
                'name': p.source_file.filename,
                'lines': [[e[0], e[1]] for e in entries],
                'fragment_offsets': [[e[0], e[2], e[3]] for e in entries],
            },
        )
    result = json.dumps(
        {
            'sources': selected_sources,
            'source_reading': {
                'mode': 'all_windows_read_verbatim_passages',
                'windows': count,
                'notice': '所有原文片段已逐段閱讀；以下為逐字保留的判讀依據，不是完整附件的逐字全文。行號仍指向完整原文，可能不連續。',
            },
        },
        ensure_ascii=False,
        separators=(',', ':'),
    )
    if sum(len(item['text']) for item in kept) > budget or len(result) > budget * 2 + 4096:
        # Never repeatedly summarize away events merely to fit a context. Dense
        # documents need multi-event synthesis, not silent evidence deletion.
        raise ContentValidationError('source_evidence_capacity_exceeded')
    return result, {'mode': 'all_windows_read', 'windows': count}
