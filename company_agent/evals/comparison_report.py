"""Offline tables from comparison reports; never call a model or invent human scores."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import statistics
from pathlib import Path

from company_agent.evals.comparison import aggregate
from company_agent.evals.comparison import ARMS
from company_agent.evals.comparison import VERSION
from company_agent.evals.comparison_cases import BY_ID
from company_agent.evals.comparison_cases import check_case
from company_agent.evals.comparison_cases import fingerprint
from examples.shared.runtime.modules import ModuleReference
# Determine task modality from the versioned manifest, not output presence.

_openpyxl = ModuleReference('openpyxl')

LABELS = {
    'company_current': '現行Lite多代理',
    'company_argon': 'Argon多代理',
    'argon_tools': '單一Argon＋工具',
    'argon_chat': '純Argon對答',
}


def regrade(path):
    """Keep originals intact; recheck saved outputs without replaying a model/tool."""

    report = json.loads(path.read_text())
    report['original_version'] = report['version']
    report['version'] = VERSION
    report['regrading_notice'] = (
        'Offline regrading of identical saved outputs. Corrects a missing_source pattern-tuple bug '
        'and adds XML/tool evidence checks for narrow edits. Separates numeric correctness from sheet naming; '
        'accepts 結果/結果表 for the original ambiguously worded data_document prompt. These checks are post-hoc; '
        'original pass flags/checks are retained and no model/tool is replayed.'
    )
    report['regrading_notice'] += ' LaTeX escaped percent signs are normalized before numeric text checks.'
    original_manifest = json.loads((path.parent / 'manifest.json').read_text())
    original_prompts = {case['id']: case['prompt'] for case in original_manifest['cases']}
    for row in report['runs']:
        if 'result' not in row:
            continue
        folder = path.parent / row['folder']
        files = {name: (folder / 'downloads' / name).read_bytes() for name in row.get('artifact_sha256', {})}
        for name, data in files.items():
            if hashlib.sha256(data).hexdigest() != row['artifact_sha256'][name]:
                raise ValueError('Saved output hash mismatch')
        row['original_passed'], row['original_checks'] = row['passed'], row['checks']
        source = folder / 'sources'
        checks = check_case(
            BY_ID[row['case']],
            row['result'],
            files,
            source,
            fingerprint(source),
            [name for call in row['calls'] for name in call.get('tool_names', [])],
        )
        # Original before/after evidence must survive regrading; never assume
        # an unchanged source just because the current file hashes itself.
        checks['sources_unchanged'] = row['original_checks']['sources_unchanged']
        checks['unique_output_names'] = row['original_checks']['unique_output_names']
        checks['no_truncated_calls'] = row['original_checks']['no_truncated_calls']
        if row['case'] == 'data_document' and '（結果表A1' in original_prompts[row['case']] and 'checked.xlsx' in files:
            load_workbook = _openpyxl.attribute('load_workbook')

            book = load_workbook(io.BytesIO(files['checked.xlsx']), data_only=False)
            if '結果表' in book.sheetnames and checks['exact_numbers']:
                checks['sheet_name'] = True
                row['adjudication'] = (
                    'Original prompt said 結果表A1 without an explicit quoted sheet name; 結果表 is acceptable. Values 15/3 are correct.'
                )
        row['checks'], row['passed'] = checks, all(checks.values())
    report['summary'] = aggregate(report['runs'], report['planned'])
    return report


def build(reports):
    if len({report.get('version') for report in reports}) > 1:
        raise ValueError('Different evaluator versions must be reported separately or regraded under a shared rubric')
    rows, planned = [], []
    # Keep identical case/repeat numbers from different reports distinct.
    for index, report in enumerate(reports):
        for row in report['runs']:
            rows.append({**row, 'repeat': f"{index}:{row['repeat']}"})
        planned.extend((c, a, f"{index}:{r}") for c, a, r in report['planned'])
    return rows, planned, aggregate(rows, planned)


def tables(reports):
    rows, planned, summary = build(reports)
    lines = [
        '# company_agent／單一Argon：自動驗收數據',
        '',
        '這是最低自動驗收，尚未加入人工盲評與文件視覺品質分數。失敗與未執行分開計數。',
        '',
    ]
    if any(report.get('regrading_notice') for report in reports):
        lines += [
            '本表為對保存輸出的離線復核：修正缺來源題的評分器錯誤，並事後補強局部修訂XML／工具檢查。',
            '原始分數及檢查保留於JSON，不重新呼叫模型；新增檢查不是初輪預先註冊的指標。',
            '',
        ]

    for title, is_file in (('對話題（四組可配對）', False), ('檔案工作題（三組可配對）', True)):
        group = [r for r in rows if bool(BY_ID[r['case']].fixture) == is_file]
        if not group:
            continue
        lines += [
            f"## {title}",
            '',
            '| 組別 | 已跑/計畫 | 通過 | 中位秒 | 模型呼叫 | 費用USD | 每通過件USD |',
            '| --- | ---: | ---: | ---: | ---: | ---: | ---: |',
        ]
        for arm in ARMS:
            subset = [r for r in group if r['arm'] == arm]
            total = sum(a == arm and bool(BY_ID[c].fixture) == is_file for c, a, _ in planned)
            if not subset:
                continue
            n, passed = len(subset), sum(r.get('passed', False) for r in subset)
            cost = sum(r['accounted_usd'] for r in subset)
            success_cost = f"{cost / passed:.5f}" if passed else '—'
            lines.append(
                f"| {LABELS[arm]} | {n}/{total} | {passed}/{n} ({passed / n:.1%}) | "
                f"{statistics.median(r['seconds'] for r in subset):.2f} | "
                f"{sum(len(r.get('calls', [])) for r in subset)} | {cost:.5f} | {success_cost} |",
            )
        lines.append('')
    lines += [
        '## 同題配對',
        '',
        '| 左組／右組 | 配對數 | 左過右敗 | 右過左敗 | 都過 | 都敗 | 左減右中位秒 |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: |',
    ]
    for pair in summary['paired']:
        lines.append(
            f"| {LABELS[pair['left']]}／{LABELS[pair['right']]} | {pair['pairs']} | "
            f"{pair['left_only_pass']} | {pair['right_only_pass']} | {pair['both_pass']} | "
            f"{pair['both_fail']} | {pair['median_left_minus_right_seconds']:.2f} |",
        )
    lines += [
        '',
        '## 每題最低驗收',
        '',
        '| 題目 | ' + ' | '.join(LABELS.values()) + ' |',
        '| --- | ' + ' | '.join('---' for _ in ARMS) + ' |',
    ]
    for case in BY_ID:
        values = []
        for arm in ARMS:
            subset = [r for r in rows if r['case'] == case and r['arm'] == arm]
            values.append(f"{sum(r.get('passed', False) for r in subset)}/{len(subset)}" if subset else '—')
        lines.append('| ' + case + ' | ' + ' | '.join(values) + ' |')
    lines += ['', '## 未通過項目（待人工判讀原因）', '']
    for row in rows:
        if not row.get('passed'):
            failed = [key for key, value in row.get('checks', {}).items() if not value]
            detail = ', '.join(failed) if failed else row.get('error_type', row['status'])
            lines.append(f"- {row['case']}／{LABELS[row['arm']]}／輪次{row['repeat']}：{detail}。")
    lines += [
        '',
        f"已執行{len(rows)}件，計畫{len(planned)}件；費用合計USD {sum(r['accounted_usd'] for r in rows):.5f}。",
        '費用為gateway／設定費率估算，包含失敗請求的未知預留；不是供應商發票。',
        '',
    ]
    return '\n'.join(lines), summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reports', nargs='+', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--regrade-output', type=Path, help='Write a distinct regraded JSON; supports one input report')
    args = parser.parse_args()
    if args.regrade_output:
        if len(args.reports) != 1 or args.regrade_output.resolve() == args.reports[0].resolve():
            parser.error('Regrade exactly one input and preserve its original path')
        report = regrade(args.reports[0])
        args.regrade_output.parent.mkdir(parents=True, exist_ok=True)
        args.regrade_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        reports = [report]
    else:
        reports = [json.loads(p.read_text()) for p in args.reports]
    markdown, _ = tables(reports)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(markdown, encoding='utf-8')
    print(args.output)


if __name__ == '__main__':
    main()
