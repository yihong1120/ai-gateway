"""Bounded presentation normalization; no fuzzy merging of calendar work."""
from __future__ import annotations

import re
import unicodedata

from .presentation import split_ai_title
from .csv_schema import CALENDAR_HEADERS


def text(value):
    return unicodedata.normalize('NFC', str(value).replace('\r\n', '\n')).strip()


def work(value):
    value = text(value)
    return value[2:].lstrip() if value.startswith('辦理') else value


def title(value):
    value = text(value)
    parsed = split_ai_title(value)
    if not parsed:
        return ('literal', value)
    project = text(parsed[1] or '')
    name = work(parsed[0])
    # Generated titles may quote the same known project prefix. Do not strip
    # punctuation from arbitrary work names, lot numbers or manual events.
    if project:
        for left, right in (('「', '」'), ('『', '』'), ('“', '”'), ('"', '"')):
            prefix = left + project + right
            if name.startswith(prefix):
                name = project + name[len(prefix):]
                break
    return ('ai', name, project)


def description(value):
    """Ignore generated estimates only when the full quoted source is present.

    Source clauses and the documented location/executor remain exact. A changed
    title must also match independently before this signature can be reused.
    """
    value = text(value)
    heading, separator, source = value.partition('\n\n公文內容：\n')
    if not separator:
        return ('literal', value)
    facts = tuple(
        match.group(0) for match in re.finditer(
            r'(?m)^(?:工程名稱|地點|辦理人／單位)：[^\n]*$', heading,
        )
    )
    return ('source', facts, source)


def csv_key(row):
    """Same full CSV facts; normalize only the generated optional work verb."""
    values = dict(row)
    parsed = split_ai_title(row['Subject'])
    heading, separator, _ = row['Description'].partition('\n\n公文內容：\n')
    if parsed and row['Custom Field1'].strip() and separator:
        values['Subject'] = title(row['Subject'])
        match = re.search(r'(?m)^辦理事項：([^\r\n]+)$', heading)
        if match:
            values['Description'] = row['Description'][:match.start(1)] + work(match[1]) + row['Description'][match.end(1):]
    return tuple(values[key] for key in CALENDAR_HEADERS)
