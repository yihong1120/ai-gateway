"""Score frozen local OCR experiments; export counts without private document text."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
from collections import Counter
from pathlib import Path

from rapidfuzz.distance import Levenshtein

from examples.deployment.diagnostics.ocr_score import contains
from examples.deployment.diagnostics.ocr_score import normalized
from examples.deployment.diagnostics.ocr_score import Table


def repeated_ngram(text, size=12):
    text = normalized(text)
    counts = Counter(text[i: i + size] for i in range(max(0, len(text) - size + 1)))
    return max(counts.values(), default=0)


def structured_rows(markdown):
    table = Table()
    table.feed(markdown)
    rows = table.rows
    for line in markdown.splitlines():
        if line.strip().startswith('|') and not re.fullmatch(r'[\s|:\-]+', line):
            rows.append([normalized(c) for c in line.strip().strip('|').split('|')])
    return rows


def score(root, output):
    path = root / 'corpus/manifest.json'
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == path.with_suffix('.sha256').read_text().strip()
    cases = {c['id']: c for c in json.loads(path.read_text())['cases']}
    reports = []
    for path in sorted((root / 'results').glob('*/report.json')):
        source = json.loads(path.read_text())
        assert source['manifest_sha256'] == digest
        rows = []
        edits = chars = 0
        for result in source['results']:
            case = cases[result['case']]
            text = result['text']
            row = {k: result.get(k) for k in ('case', 'repeat', 'success', 'seconds', 'orientation_angle')}
            row.update(
                fields_matched=sum(contains(text, f) for f in case['critical_fields']),
                fields_total=len(case['critical_fields']),
                output_characters=len(normalized(text)),
                most_repeated_12gram_count=repeated_ngram(text),
            )
            if case.get('expected_blank'):
                row['blank_hallucination'] = bool(normalized(text))
            truth = case.get('ground_truth')
            if truth:
                truth = normalized(truth)
                distance = Levenshtein.distance(truth, normalized(text))
                edits += distance
                chars += len(truth)
                row['cer_percent'] = round(distance / len(truth) * 100, 3)
            if case['table_cells']:
                observed = structured_rows(result.get('markdown') or text)
                expected = [case['table_cells'][i: i + 4] for i in range(0, len(case['table_cells']), 4)]
                row['exact_table_rows'] = sum(
                    any([normalized(c) for c in e] == r[-4:] for r in observed) for e in expected
                )
                row['expected_table_rows'] = len(expected)
            rows.append(row)
        field_hits = sum(r['fields_matched'] for r in rows)
        fields = sum(r['fields_total'] for r in rows)
        real = [r for r in rows if r['case'].startswith('real-')]
        real_hits = sum(r['fields_matched'] for r in real)
        real_fields = sum(r['fields_total'] for r in real)
        times = [r['seconds'] for r in rows if r['success']]
        reports.append(
            {
                'label': source['label'],
                'backend': source['backend'],
                'orientation': source['orientation'],
                'requests': len(rows),
                'responses_completed': sum(r['success'] for r in rows),
                'median_page_seconds_including_orientation': round(statistics.median(times), 3) if times else None,
                'slowest_page_seconds': max(times, default=None),
                'synthetic_cer_percent': round(edits / chars * 100, 3) if chars else None,
                'critical_fields_matched': field_hits,
                'critical_fields_total': fields,
                'critical_field_recall_percent': round(field_hits / fields * 100, 3) if fields else None,
                'real_critical_fields_matched': real_hits,
                'real_critical_fields_total': real_fields,
                'real_critical_field_recall_percent': round(real_hits / real_fields * 100, 3) if real_fields else None,
                'exact_table_rows': sum(r.get('exact_table_rows', 0) for r in rows),
                'expected_table_rows': sum(r.get('expected_table_rows', 0) for r in rows),
                'blank_pages_with_invented_text': sum(r.get('blank_hallucination', False) for r in rows),
                'whole_gpu_peak_MiB': source.get('gpu_peak_MiB'),
                'whole_gpu_min_free_MiB': source.get('gpu_min_free_MiB'),
                'cases': rows,
            },
        )
    report = {
        'date': '2026-10-17',
        'timezone': 'Asia/Taipei',
        'local_inference_only': True,
        'corpus_pages': len(cases),
        'benchmark_requests': sum(r['requests'] for r in reports),
        'benchmark_responses_completed': sum(r['responses_completed'] for r in reports),
        'manifest_sha256': digest,
        'reference': 'Six synthetic pages have full known text; six nonblank real pages have manually reviewed printed key fields; one near-blank scan is a hallucination control. References frozen before inference.',
        'metrics': {
            'fields': 'Exact NFKC/whitespace/markup normalized matching; thousands separators removed; Traditional and Simplified characters remain distinct. Numeric fields have digit boundaries.',
            'cer': 'Normalized synthetic full-text Levenshtein distance only; includes reading-order and markup effects.',
            'table': 'All four expected cells must match in the same HTML/Markdown table row.',
            'completion': 'A completed response is not an accuracy pass. Blank pages may return empty text successfully.',
            'latency': 'Single requests, no warm-up omitted; first page can include lazy model initialization. Orientation runtime included. Preliminary raw Paddle/GLM requests overlapped briefly and must not be used for isolated latency comparison.',
            'gpu': 'Whole card measurements include the resident production Paddle service and any experimental server; not per-model memory.',
            'limitations': 'Small fixed corpus and sampled printed fields; no real-page full-text CER, handwritten accuracy, chart geometry understanding, downstream scheduling, or EIP import assertions.',
        },
        'profiles': reports,
    }
    for filename, key in (
        ('gemma-restored.json', 'gemma_restoration'),
        ('services-validation.json', 'post_experiment_verification'),
        ('model-identities.json', 'mineru_model_identities'),
    ):
        metadata = root / filename
        if metadata.exists():
            report[key] = json.loads(metadata.read_text())
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    for r in reports:
        print(
            r['label'],
            f"{r['responses_completed']}/{r['requests']}",
            'fields',
            r['critical_field_recall_percent'],
            'real',
            r['real_critical_field_recall_percent'],
            'CER',
            r['synthetic_cer_percent'],
            'table',
            r['exact_table_rows'],
            '/',
            r['expected_table_rows'],
            'blank hallucinations',
            r['blank_pages_with_invented_text'],
            'median',
            r['median_page_seconds_including_orientation'],
        )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    score(args.root, args.output)
