"""Score local OCR output without publishing private document text."""
from __future__ import annotations

import argparse
import html
import json
import re
import statistics
import unicodedata
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

from rapidfuzz.distance import Levenshtein


def plain(text):
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text)
    text = re.sub(r'<[^>]*>', ' ', text)
    text = re.sub(r'(?m)^\s*\|?[\s:|-]{3,}\|?\s*$', '', text)
    return html.unescape(text).replace('|', ' ').replace('**', '').replace('#', '')


def normalized(text, compact=True):
    text = unicodedata.normalize('NFKC', plain(text))
    text = re.sub(r'(?<=\d),(?=\d{3}(?:\D|$))', '', text)
    return re.sub(r'\s+', '' if compact else ' ', text)


def contains(text, field):
    target = normalized(field)
    if re.fullmatch(r'[\d.]+', target):
        return bool(re.search(r'(?<!\d)' + re.escape(target) + r'(?!\d)', normalized(text, compact=False)))
    return target in normalized(text)


class Table(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.row = []
        elif tag in ('td', 'th'):
            self.cell = ''

    def handle_data(self, data):
        if self.cell is not None:
            self.cell += data

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.cell is not None:
            if self.row is not None:
                self.row.append(normalized(self.cell))
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def summarize(root, destination):
    manifest = json.loads((root / 'corpus/manifest.json').read_text())
    cases = {c['id']: c for c in manifest['cases']}
    reports = []
    for file in sorted((root / 'results').glob('*/report.json')):
        source = json.loads(file.read_text())
        if 'smoke' in source['label']:
            continue
        results = []
        edits = chars = field_hits = fields = cell_hits = cells = 0
        for row in source['results']:
            case = cases[row['case']]
            output = row['text']
            got = sum(contains(output, f) for f in case['critical_fields'])
            total = len(case['critical_fields'])
            cell_got = sum(contains(output, f) for f in case['table_cells'])
            cell_total = len(case['table_cells'])
            field_hits += got
            fields += total
            cell_hits += cell_got
            cells += cell_total
            result = {
                k: row.get(k) for k in (
                    'case', 'kind', 'repeat', 'success',
                    'seconds', 'gpu_peak_MiB', 'gpu_min_free_MiB', 'error',
                )
            }
            result.update(
                critical_fields_matched=got, critical_fields_total=total,
                table_cells_matched=cell_got, table_cells_total=cell_total,
            )
            if row['case'].startswith('synthetic'):
                truth, predicted = normalized(case['ground_truth']), normalized(output)
                distance = Levenshtein.distance(truth, predicted)
                edits += distance
                chars += len(truth)
                result['cer_percent'] = round(distance / len(truth) * 100, 3)
                # A separate bag-of-characters diagnostic distinguishes reading
                # order changes from missing/substituted characters.
                missing = sum((Counter(truth) - Counter(predicted)).values())
                result['character_recall_percent'] = round((1 - missing / len(truth)) * 100, 3)
                result['missing_synthetic_fields'] = [f for f in case['critical_fields'] if not contains(output, f)]
            if case['table_cells']:
                table = Table()
                table.feed(row.get('markdown', '') or output)
                result['structured_table_present'] = bool(table.rows)
                expected = [case['table_cells'][i:i + 4] for i in range(0, len(case['table_cells']), 4)]
                result['table_rows_exact'] = sum(
                    any([normalized(c) for c in expected_row] == observed[-4:] for observed in table.rows)
                    for expected_row in expected
                )
                result['table_rows_total'] = len(expected)
            results.append(result)
        times = [r['seconds'] for r in results if r['success']]
        structured_rows = sum(r.get('table_rows_exact', 0) for r in results)
        expected_rows = sum(r.get('table_rows_total', 0) for r in results)
        reports.append({
            'label': source['label'], 'backend': source['backend'],
            'requests': len(results), 'successes': sum(r['success'] for r in results),
            'failures': sum(not r['success'] for r in results),
            'median_page_seconds': round(statistics.median(times), 3) if times else None,
            'slowest_page_seconds': max(times, default=None),
            'synthetic_cer_percent': round(edits / chars * 100, 3) if chars else None,
            'critical_field_recall_percent': round(field_hits / fields * 100, 3) if fields else None,
            'critical_fields_matched': field_hits, 'critical_fields_total': fields,
            'table_cell_recall_percent': round(cell_hits / cells * 100, 3) if cells else None,
            'structured_table_rows_exact': structured_rows,
            'structured_table_rows_total': expected_rows,
            'gpu_peak_MiB': source.get('gpu_peak_MiB'),
            'gpu_min_free_MiB': source.get('gpu_min_free_MiB'),
            'startup_seconds': source['startup_seconds'], 'warmup_seconds': source['warmup_seconds'],
            'round_seconds': source.get('round_seconds'), 'cases': results,
        })
    gemma = []
    for file in sorted(root.glob('*-coexist-gemma.json')):
        data = json.loads(file.read_text())
        gemma.append({k: v for k, v in data.items() if k != 'requests'})
    report = {
        'date': '2026-10-14', 'timezone': 'Asia/Taipei',
        'corpus_pages': len(cases), 'synthetic_pages': sum(k.startswith('synthetic') for k in cases),
        'real_pages': sum(k.startswith('real') for k in cases),
        'metrics': {
            'cer': 'Levenshtein distance on normalized synthetic full text only; lower is better.',
            'critical_fields': 'Exact normalized match; numeric fields use digit boundaries.',
            'real_documents': 'Embedded PDF text provides field references; no full-page CER because image-only stamps are not fully transcribed.',
            'normalization': 'NFKC, whitespace/markup removal, thousands-separator normalization; traditional/simplified characters are NOT merged.',
        },
        'profiles': reports, 'gemma_coexist': gemma,
    }
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    for r in reports:
        print(
            r['label'], 'success', r['successes'], '/', r['requests'],
            'median', r['median_page_seconds'], 'CER%', r['synthetic_cer_percent'],
            'fields%', r['critical_field_recall_percent'], 'cells%', r['table_cell_recall_percent'],
        )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/home/changdar/opt/ocr-comparison-20261014'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    summarize(args.root, args.output)
