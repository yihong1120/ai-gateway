"""Secondary whole-number parsing check for clearly labelled Office metrics."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import unicodedata
from pathlib import Path


from company_agent.services.office.documents import extract


METRICS = {'合計': 'sum', '筆數': 'count'}
PATTERN = re.compile(r'(合計|筆數)\s*[:：]\s*([+-]?\d[\d,]*(?:\.\d+)?(?:[eE][+-]?\d+)?)')


def check_text(text, statistics):
    text = unicodedata.normalize('NFKC', text)
    observed, review = [], []
    for match in PATTERN.finditer(text):
        prefix = text[max(text.rfind('\n', 0, match.start()) + 1, match.start() - 24):match.start()]
        if '舊版' in prefix or '旧版' in prefix or 'old.xlsx' in prefix:
            continue
        # Expressions after the first number need semantic review, not a scalar verdict.
        if text[match.end():].lstrip().startswith(('/', '*', '+', '-')):
            continue
        value = float(match[2].replace(',', ''))
        expected = statistics[METRICS[match[1]]]
        item = {'label': match[1], 'actual': value, 'expected': expected,
                'matches': math.isclose(value, expected, rel_tol=1e-10, abs_tol=1e-8)}
        observed.append(item)
        if not item['matches']:
            review.append({**item, 'context': text[max(0, match.start() - 60):match.end() + 80]})
    return {'observed_metrics': observed, 'needs_semantic_review': review}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert not check_text('合計：356.0；筆數：10', {'sum': 356, 'count': 10})['needs_semantic_review']
    assert len(check_text('合計：356.1；筆數：10.5', {'sum': 356, 'count': 10})['needs_semantic_review']) == 2
    report = json.loads(args.report.read_text())
    assert report.get('completed_at') and len(report['runs']) == report['planned_jobs'] and not report['unrun']
    results = []
    for row in report['runs']:
        if row['lane'] != 'workflow' or row['case'] == 'W03':
            continue
        folder = args.report.parent / row['folder']
        expected = json.loads((folder / 'expected.private.json').read_text())['stats']
        for name, sha in row['artifact_sha256'].items():
            path = folder / 'downloads' / name
            if path.suffix not in {'.docx', '.pptx'}:
                continue
            data = path.read_bytes()
            assert hashlib.sha256(data).hexdigest() == sha
            text = '\n'.join(block['text'] for block in extract(data, path.suffix))
            results.append({'case': row['case'], 'arm': row['arm'], 'repeat': row['repeat'],
                            'file': name, 'primary_passed': row['passed'], 'artifact_sha256': sha,
                            **check_text(text, expected)})
            assert path.read_bytes() == data
    value = {'version': 'company-data-text-audit-v1',
             'scope': 'labelled sum/count scalar checks; flags require semantic review; primary scores unchanged',
             'self_test': 'correct decimal representation accepted; decimal-extension errors detected',
             'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             'files_inspected': len(results), 'runs': results,
             'candidate_mismatch_count': sum(len(item['needs_semantic_review']) for item in results)}
    args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: value[key] for key in ('files_inspected', 'candidate_mismatch_count', 'self_test')}))


if __name__ == '__main__':
    main()
