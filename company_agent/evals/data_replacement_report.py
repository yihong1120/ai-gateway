"""Publish a bounded replacement conclusion from one frozen paired cohort."""
from __future__ import annotations

import argparse
import csv
import json
import uuid
import zipfile
from pathlib import Path

from company_agent.evals.comparison import write_json
from company_agent.evals.data_replacement import ARMS, summarize
from company_agent.evals.data_replacement_cases import BY_ID


def decision(report):
    summary = summarize(report['runs'])
    if report.get('unrun') or not report.get('source_version_unchanged') or not summary['all_pairs_have_identical_sources']:
        return '測試不完整或版本／來源不一致，不能判定是否替換。'
    flash, argon = summary['arms']['flash'], summary['arms']['argon']
    if flash['critical_failures']:
        return 'Flash 出現重要正確性或操作邊界問題，不建議全面替換。'
    frole, arole = flash['groups'].get('role_positive'), argon['groups'].get('role_positive')
    if not frole or not arole:
        return '缺少完整的 data 角色正向案例，不能判定是否替換。'
    controls = flash['groups'].get('role_control')
    if frole['acceptance'] < 0.95 or frole['acceptance'] < arole['acceptance'] - 0.05 or controls and controls['acceptance'] < 1:
        return 'Flash 尚未通過預先設定的角色品質門檻，不建議全面替換；可再分析哪些類型適合。'
    workflow = flash['groups'].get('workflow_positive')
    if not workflow or workflow['acceptance'] < 0.95:
        return 'Flash 的 data 角色表現達到門檻，但完整交付流程尚未達標，現階段只適合作限定場景候選。'
    cost_benefit = frole['usd_per_accepted'] <= arole['usd_per_accepted'] * 0.8 if arole['usd_per_accepted'] else False
    time_benefit = frole['median_seconds'] <= arole['median_seconds'] * 0.8
    if cost_benefit or time_benefit:
        return '在已測場景中，Flash 達到品質門檻並有時間或費用優勢，可作限定場景替換候選。'
    return '在已測場景中，Flash 達到品質門檻，但尚未顯示足夠的時間或費用優勢。'


def build(report, output, development=(), cleanup=None, source_folder=None):
    output.parent.mkdir(parents=True, exist_ok=True)
    summary = summarize(report['runs'])
    conclusion = decision(report)
    public_rows = []
    for row in report['runs']:
        fields = ('case', 'title', 'lane', 'control', 'category', 'arm', 'repeat', 'seed', 'source_sha256',
                  'data_route', 'other_role_model', 'status', 'error_type', 'failure_stage', 'passed',
                  'checks', 'calls', 'seconds', 'accounted_usd', 'critical_failures', 'attribution',
                  'artifact_sha256', 'fault', 'fault_applied')
        public_rows.append({**{key: row[key] for key in fields if key in row},
                            'answer': row.get('result', row.get('partial_result', {})).get('answer', ''),
                            'tool_feedback': row.get('result', row.get('partial_result', {})).get('tool_feedback', [])})
    value = {'version': report['version'], 'models': ARMS, 'conclusion': conclusion,
             'scope': 'minimum programmatic acceptance on synthetic fixtures; no human blind/visual scoring',
             'summary': summary, 'runs': public_rows, 'limits': report.get('limits'),
             'source_version_unchanged': report.get('source_version_unchanged'),
             'source_sha256': report['source_sha256'], 'planned_jobs': report['planned_jobs'],
             'completed_jobs': len(report['runs']), 'unrun': report.get('unrun', []),
             'started_at': report.get('started_at'), 'completed_at': report.get('completed_at'),
             'development_cohorts': [{'version': d['version'], 'summary': summarize(d['runs']),
                                      'runs': d['runs'], 'unrun': d.get('unrun', [])} for d in development],
             'cleanup': cleanup}
    write_json(output.with_suffix('.json'), value)
    with output.with_suffix('.csv').open('w', newline='', encoding='utf-8-sig') as stream:
        headers = ['case', 'title', 'lane', 'category', 'arm', 'repeat', 'passed', 'seconds',
                   'accounted_usd', 'attribution', 'failed_checks', 'error_type']
        writer = csv.DictWriter(stream, fieldnames=headers)
        writer.writeheader()
        for row in public_rows:
            writer.writerow({**{key: row.get(key, '') for key in headers},
                             'failed_checks': ', '.join(k for k, v in row.get('checks', {}).items() if not v)})
    lines = ['# Flash 取代 Argon 資料角色測試結果', '', conclusion, '',
             '這項比較只更換 data 角色模型，其餘七個角色固定使用 Gemini 3.5 Flash Lite。'
             '直接角色測試與完整公司代理流程分開統計；缺資料及工具限制對照題不混入正向產檔成功率。', '',
             '## 測試範圍', '',
             f'主批次規劃 {report["planned_jobs"]} 件，完成 {len(report["runs"])} 件，未執行 {len(report.get("unrun", []))} 件。'
             f'配對來源完全相同：{summary["all_pairs_have_identical_sources"]}；程式版本固定：{report.get("source_version_unchanged")}。', '',
             '推理等級、輸出上限、單次請求及整體工作時限相同；沒有模型 fallback 或自動升級。'
             '角色測試預先提供交付檔名，不提供隱藏數字答案；流程測試由真正主協調安排計算、文件與獨立審核。', '',
             '## 成功率與工作成本', '',
             '| 範圍 | 模型 | 通過 | 中位秒數 | P95 秒數 | 費用估算 USD | 每成功件 USD |',
             '| --- | --- | --- | --- | --- | --- | --- |']
    labels = {'role_positive': 'data 正向任務', 'role_control': '缺資料與工具限制', 'workflow_positive': '完整交付流程'}
    for key, label in labels.items():
        for arm in ARMS:
            group = summary['arms'][arm]['groups'].get(key)
            if not group:
                continue
            unit = f'{group["usd_per_accepted"]:.5f}' if group['usd_per_accepted'] is not None else '—'
            lines.append(f'| {label} | {arm} | {group["passed"]}/{group["n"]} | {group["median_seconds"]:.2f} | '
                         f'{group["p95_seconds"]:.2f} | {group["accounted_usd"]:.5f} | {unit} |')
    counts = summary['paired_counts']
    lines += ['', f'同題同輪配對：兩者通過 {counts["both_pass"]}、只有 Flash 通過 {counts["flash_only_pass"]}、'
              f'只有 Argon 通過 {counts["argon_only_pass"]}、兩者未通過 {counts["both_fail"]}。', '',
              '費用欄採 gateway 回應或設定費率估算；逾時未取得用量的請求保留保守預留，不能當作供應商實扣。'
              '回應費用與未知預留分別保存在 JSON；若有 gateway 持久記帳，另列於清理與帳務。', '',
              '## 逐題結果', '', '| 題號 | 場景 | Flash | Argon |', '| --- | --- | --- | --- |']
    for key in sorted({r['case'] for r in report['runs']}):
        records = [r for r in report['runs'] if r['case'] == key]
        cells = []
        for arm in ARMS:
            subset = [r for r in records if r['arm'] == arm]
            cells.append(f'{sum(r["passed"] for r in subset)}/{len(subset)}')
        lines.append(f'| {key} | {BY_ID[key].title} | {cells[0]} | {cells[1]} |')
    lines += ['', '## 未通過案例與原因', '']
    failures = [r for r in public_rows if not r['passed']]
    if not failures:
        lines += ['本批次所有已執行案例均通過自動驗收。', '']
    for row in failures:
        failed = ', '.join(k for k, v in row.get('checks', {}).items() if not v)
        lines.append(f'- {row["case"]}／{row["arm"]}／第 {row["repeat"]} 輪：{row["attribution"]}；'
                     + (f'{row.get("error_type")}。' if row.get('error_type') else f'未通過 {failed}。'))
    lines += ['', '主協調尚未呼叫 data 的失敗，分類為 coordinator_before_data；文件或 reviewer 的失敗另列，'
              '不直接推論為 data 模型能力不足。逾時仍計入操作上的失敗，但與數值錯誤分開解讀。', '',
              '## 替換判準與限制', '',
              '預先設定的操作門檻為：Flash 正向成功率至少95%，比 Argon 少不超過5個百分點，限制對照全數正確處理，'
              '重要錯數字或操作邊界問題為零；完整交付也須達95%。每成功件費用或中位時間至少改善20%，才視為有明顯實用收益。', '',
              '這是有限合成案例上的候選門檻；每題僅少量重複，沒有證明普遍等效，也沒有人工盲評或完整版面認證。'
              '真實 NAS／EIP、超長工作、巨集及任意 Excel 功能不在本批次範圍。正式模型設定沒有自動切換。', '']
    if development:
        lines += ['## 開發試跑', '',
                  '開發試跑與主批次分開保留，原始失敗沒有以補測覆蓋；不同程式版本或修正過的評分器不混算。', '']
        for index, cohort in enumerate(development, 1):
            group = summarize(cohort['runs'])['arms']
            lines.append(f'- 試跑 {index}：Flash {group["flash"]["passed"]}/{group["flash"]["n"]}，'
                         f'Argon {group["argon"]["passed"]}/{group["argon"]["n"]}。')
    if cleanup:
        lines += ['', '## 清理與帳務', '',
                  '```json', json.dumps(cleanup, ensure_ascii=False, indent=2), '```', '']
    if source_folder:
        mapping, human = [], []
        blind_path = output.with_name(output.stem + '-blind-review.zip')
        with zipfile.ZipFile(blind_path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for row in report['runs']:
                blind_id = 'B' + uuid.uuid4().hex[:12]
                archive.writestr(blind_id + '/prompt.txt', BY_ID[row['case']].prompt)
                archive.writestr(blind_id + '/answer.txt', row.get('result', row.get('partial_result', {})).get('answer', ''))
                downloads = source_folder / row['folder'] / 'downloads'
                if downloads.is_dir():
                    for path in downloads.iterdir():
                        if path.is_file():
                            archive.write(path, blind_id + '/' + path.name)
                mapping.append({'blind_id': blind_id, 'case': row['case'], 'arm': row['arm'], 'repeat': row['repeat']})
                human.append({'blind_id': blind_id, 'accuracy_0_5': '', 'completeness_0_5': '',
                              'clarity_0_5': '', 'practicality_0_5': '', 'notes': ''})
        write_json(source_folder / 'blind-mapping.private.json', mapping)
        with output.with_name(output.stem + '-human-review.csv').open('w', newline='', encoding='utf-8-sig') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(human[0]) if human else ['blind_id'])
            writer.writeheader()
            writer.writerows(human)
        lines += ['', f'人工盲評資料包：[{blind_path.name}]({blind_path.name})；評分欄位尚未填寫，不當作已完成的人類評分。', '']
    output.write_text('\n'.join(lines), encoding='utf-8')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--primary', type=Path, required=True)
    parser.add_argument('--development', type=Path, nargs='*', default=[])
    parser.add_argument('--cleanup', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.primary.read_text())
    build(report, args.output, [json.loads(p.read_text()) for p in args.development],
          json.loads(args.cleanup.read_text()) if args.cleanup else None, args.primary.parent)


if __name__ == '__main__':
    main()
