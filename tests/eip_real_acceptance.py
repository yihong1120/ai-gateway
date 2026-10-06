"""Independent acceptance for private real-document artifacts, never model input.

Run: python tests/eip_real_acceptance.py /private/evaluation/directory
The CLI only reads artifacts and reports failures; it cannot write to EIP.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

EXPECTED = {
    '59166': ('dated', ['2026/09/09']),
    '59136': ('dated', ['2026/09/08']),
    '59198': ('dated', ['2026/09/11', '2026/09/16']),
    # The reporting milestone is dated; the receiver's separately described
    # immediate/relative remediation has no independent calendar start date.
    # Only ONE dated row is permitted, with the original reporting agency.
    '59161': ('mixed', ['2026/09/10']),
    # S1 clause 2 assigns the reporting deadline to the copied agency. The
    # checklist's inspection / previous notices are not new remediation dates.
    '59217': ('mixed', ['2026/09/14']),
    '59141': ('pending', []),
    '59185': ('pending', []),
    '59187': ('none', []),
    '59137': ('mixed', ['2026/09/02', '2026/09/13']),
}


def check_case(key, result, preview=None):
    mode, dates = EXPECTED[key]
    rows = sorted(result.get('rows') or [], key=lambda r: (r['Start Date'], r['Start Time(24hr)'], r['Subject']))
    errors = result.get('csv_errors') or []
    events = (result.get('payload') or {}).get('events') or []
    review = result.get('semantic_review') or {}
    checks = {
        'source_metadata': bool(result.get('document')) and not result.get('metadata_notices'),
        'execution': not result.get('errors') and result.get('analysis_status') != 'failed',
    }
    if mode in ('dated', 'mixed'):
        checks.update(
            dates=[r['Start Date'] for r in rows] == dates,
            approved=review.get('approved') is True,
            ai_titles=bool(rows) and all(re.fullmatch(r'.+\[AI產生\](?:\(.+\))?', r['Subject']) for r in rows),
            private=bool(rows) and all(r['Private'] == '真' for r in rows),
            pending_only=not errors or mode == 'mixed' and all(
                '日期待補：' in e or '日期或附件關係待確認' in e
                or '行事曆語意核對待確認：' in e and bool(result.get('pending_events'))
                and all(
                    p.get('status') == 'needs_review' and p.get('start_date') is None
                    for p in result['pending_events']
                ) for e in errors
            ),
        )
        if key == '59136':
            checks['time'] = [r['Start Time(24hr)'] for r in rows] == ['10:00']
        if key == '59137':
            checks['intervals'] = [(r['Start Time(24hr)'], r['End Date'], r['End Time(24hr)']) for r in rows] == [
                ('21:00', '2026/09/03', '06:00'), ('21:00', '2026/09/14', '06:00'),
            ]
        if key in ('59161', '59217'):
            heading = rows[0]['Description'].split('公文內容：')[0] if len(rows) == 1 else ''
            accepted = result.get('accepted_events') or []
            checks['original_reporting_role'] = (
                '第一工務段' in heading
                and '提報第一工務段' not in heading and '請長達營造' not in heading
                and len(accepted) == 1 and accepted[0].get('responsible_party') == '第一工務段'
            )
        if preview is not None:
            checks['eip_parser'] = preview.get('accepted') is True
            # Product decision: an unmatched project may remain private. An
            # ambiguous/invalid mapping must not masquerade as that fallback.
            checks['project_mapping'] = bool(preview.get('projects')) and all(
                p in ('matched', 'unmatched') for p in preview['projects']
            )
            checks['replay_guard'] = preview.get('replay') == 'checked'
            if key in ('59166', '59136', '59161', '59198'):
                checks['existing_receipts'] = bool(preview.get('journal_states')) and all(
                    status == 'done' for status in preview['journal_states']
                )
    elif mode == 'pending':
        checks['retained_without_calendar'] = not rows and bool(events) and bool(errors)
        checks['not_format_failure'] = bool(errors) and all(
            '日期待補：' in e or '日期或附件關係待確認' in e or '行事曆語意核對待確認：' in e for e in errors
        )
    else:
        checks['no_unrequested_calendar'] = not rows and not errors and all(
            e.get('status') in ('historical', 'cancelled', 'out_of_scope') for e in events
        )
    return {'document_id': key, 'passed': all(checks.values()), 'checks': checks}


def main(root):
    reports = []
    for key in EXPECTED:
        folder = root/key
        result = json.loads((folder/'result.json').read_text()) if (folder/'result.json').exists() else {}
        preview = json.loads((folder/'preflight.json').read_text()) if (folder/'preflight.json').exists() else None
        reports.append(check_case(key, result, preview))
    summary = {'passed': sum(r['passed'] for r in reports), 'total': len(reports), 'cases': reports}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary['passed'] == summary['total'] else 1


if __name__ == '__main__':
    raise SystemExit(main(Path(sys.argv[1])))
