"""Deterministic calendar display rules, independent of EIP transport."""
from __future__ import annotations

import re
from itertools import chain
from itertools import repeat


AI_MARKER = '[AI產生]'
TITLE_LIMIT = 200
_WRAPPER = re.compile(r'(?:(?P<math>\$\$?)[ \t]*)?\\(?:underline|text|textbf|textit|textrm|textnormal|emph)[ \t]*\{')


def clean_ocr_presentation(text, _depth=0):
    """Remove only balanced known OCR formatting wrappers, never source text."""
    if _depth > 24:
        raise ValueError('OCR 排版巢狀過深，請核對；未截斷文字。')
    result = []
    cursor = 0
    math_mode = None
    delimiters = chain(re.finditer(r'(?<!\\)(?:\$\$|\$)', text), repeat(None))
    delimiter_match = next(delimiters)
    while match := _WRAPPER.search(text, cursor):
        while delimiter_match is not None and delimiter_match.start() < match.start():
            delimiter = delimiter_match.group()
            math_mode = delimiter if math_mode is None else None if math_mode == delimiter else math_mode
            delimiter_match = next(delimiters)
        pos = match.end()
        level = 1
        while pos < len(text) and level:
            if text[pos] == '\\' and pos+1 < len(text) and text[pos+1] in '{}':
                pos += 2
                continue
            if text[pos] == '{':
                level += 1
            elif text[pos] == '}':
                level -= 1
            pos += 1
        if level:
            result.append(text[cursor:])
            return ''.join(result)
        end = pos
        marker = match.group('math')
        if not marker and math_mode:
            result.append(text[cursor:pos])
            cursor = pos
            continue
        if marker:
            tail = re.match(r'[ \t]*'+re.escape(marker), text[pos:])
            if not tail:
                result.append(text[cursor:pos])
                cursor = pos
                continue
            end += tail.end()
            while delimiter_match is not None and delimiter_match.start() < end:
                delimiter_match = next(delimiters)
        result.extend((text[cursor:match.start()], clean_ocr_presentation(text[match.end():pos-1], _depth+1)))
        cursor = end
    result.append(text[cursor:])
    return ''.join(result)


def split_ai_title(title):
    match = re.fullmatch(r'(.*?)\[AI產生\](?:\((.+)\))?[。.]?', title.strip(), re.DOTALL)
    return (match[1].rstrip(), match[2] if match[2] else None) if match else None


def format_ai_title(title: str, project: str | None = None):
    if not title.strip() or any(ord(c) < 32 for c in title):
        raise ValueError('事項名稱格式無效。')
    base = title.strip().rstrip('。')
    old_project = None
    while parsed := split_ai_title(base):
        base, old_project = parsed[0], old_project or parsed[1]
    if not base or AI_MARKER in base:
        raise ValueError('事項名稱的 AI 標記格式無效。')
    candidate = project if project and project.strip() else old_project
    if candidate is not None:
        candidate = candidate.strip()
        if any(ord(c) < 32 for c in candidate) or AI_MARKER in candidate:
            candidate = None
    suffix = AI_MARKER+(f'({candidate})' if candidate else '')
    full = base+suffix
    if len(full.encode()) <= TITLE_LIMIT:
        return full, ''
    budget = TITLE_LIMIT-len((suffix+'…').encode())
    prefix = ''
    for char in base:
        if len((prefix+char).encode()) > budget:
            break
        prefix += char
    if not prefix:
        raise ValueError('事項名稱超過 EIP 名稱長度限制，未產生行程。')
    return prefix+'…'+suffix, '事項名稱因 EIP 長度限制縮短；完整事項名稱：\n'+full


def event_description(title, project, event, parts, primary_source_id):
    heading = ['辦理事項：'+title]
    if project:
        heading.append('工程名稱：'+project)
    day = event['start_date'].replace('-', '/')
    if event.get('start_time'):
        timing = day+' '+event['start_time']
        timing += (
            ' ～ '+(event.get('end_date') or event['start_date']).replace(
                '-',
                '/',
            )+' '+event['end_time']
        ) if event.get('end_time') else '（原文未載明結束時間）'
    else:
        timing = day+(
            ' ～ '+event['end_date'].replace('-', '/') if event.get('end_date')
            and event['end_date'] != event['start_date'] else ''
        )+'（全天；原文未載明時刻）'
    heading.append('日期：'+timing)
    for field, label in (('location', '地點'), ('responsible_party', '辦理人／單位')):
        if event.get(field) not in (None, '', '未載明'):
            heading.append(label+'：'+clean_ocr_presentation(event[field]))
    groups = {}
    for sid, _, _, text in parts:
        groups.setdefault(sid, []).append(clean_ocr_presentation(text))
    sections = ['\n'.join(heading)]
    if primary_source_id in groups:
        sections.append('公文內容：\n'+'\n\n'.join(groups.pop(primary_source_id)))
    sections.extend(f'相關附件 {index} 補充內容：\n' + '\n\n'.join(texts) for index, texts in enumerate(groups.values(), 1))
    return '\n\n'.join(sections)
