"""Opt-in, budgeted live gateway benchmark on synthetic company-agent tasks.

Does not change default models, create API keys, or touch EIP/NAS. Reports use
gateway cost headers when present; these are estimates, not provider invoices.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import math
import os
import re
import time
import zipfile
from pathlib import Path

import yaml
from langchain_core.messages import messages_to_dict

import company_agent.runtime.models as _models_imports
import examples.identity.adapters.webui as _webui_imports
from company_agent.agents.registry import ROLE_TOOLS
from company_agent.config import Root
from company_agent.config import Settings
from company_agent.evals.runner import Budget
from company_agent.evals.runner import evaluation
from company_agent.evals.runner import own_model
from company_agent.evals.runner import release_model
from company_agent.evals.runner import track_report
from company_agent.evals.runner import write_json
from company_agent.evals.sandbox import make_state_directory
from company_agent.runtime.execution import DocumentAgent
from company_agent.services.office.documents import create_slides
from company_agent.services.office.documents import create_spreadsheet
from company_agent.services.office.documents import create_word
from company_agent.services.office.documents import extract
from company_agent.services.office.documents import read_spreadsheet
from company_agent.state import GRAPH_VERSION
from examples.shared.config import config_path
from examples.shared.config import read_service
from examples.shared.runtime.modules import ModuleReference

_agent_scenario_suite = ModuleReference('company_agent.evals.scenario_suite')


class AnalysisInvocation:
    def __init__(self, model, operation='analyze', **options):
        self.model, self.operation, self.options = model, operation, options

    async def ainvoke(self, messages):
        return await getattr(self.model, self.operation)(messages, **self.options)


class MeteredInvocation:
    def __init__(self, outer, bound, role, schemas):
        self.outer, self.bound, self.role, self.schemas = outer, bound, role, schemas

    async def ainvoke(self, messages):
        outer, bound, role, schemas = self.outer, self.bound, self.role, self.schemas
        # Bytes plus framing is a deliberately conservative token
        # allowance. Failed requests retain their reserve as unknown.
        size = len(json.dumps([messages_to_dict(messages), schemas], ensure_ascii=False).encode())
        output_limit = getattr(getattr(outer.model, 'client', None), 'max_tokens', None) or 4096
        output_limit = getattr(bound, 'options', {}).get('max_output_tokens') or output_limit
        reserve = (size + 256 * len(messages)) * outer.rates['input'] + output_limit * outer.rates['output']
        outer.budget.reserve(reserve)
        start = time.monotonic()
        row = {'role': role, 'requested_model': outer.name, 'input_serialized_bytes': size}
        try:
            message = await bound.ainvoke(messages)
        except (Exception, asyncio.CancelledError) as exc:
            diagnostic = str(exc).lower()
            row.update(
                status='failed',
                error_type=type(exc).__name__,
                seconds=round(time.monotonic() - start, 3),
                reserved_unknown_usd=reserve,
                provider_status=getattr(exc, 'status_code', None),
                error_flags=[
                    flag for flag in ('resource_exhausted', 'quota', 'max_parallel', 'budget', 'cooldown')
                    if flag in diagnostic
                ],
            )
            outer.calls.append(row)
            # Preserve SDK type so execution can save its normal safe failure
            # projection. Reports record type only, never provider bodies.
            raise
        metadata = dict(message.response_metadata)
        headers = {str(k).lower(): v for k, v in metadata.pop('headers', {}).items()}
        usage = message.usage_metadata or {}
        input_tokens = usage.get('input_tokens', 0)
        output_tokens = usage.get('output_tokens', 0)
        cached = usage.get('input_token_details', {}).get('cache_read', 0)
        estimate = (
            max(0, input_tokens - cached) * outer.rates['input']
            + cached * outer.rates['cache']
            + output_tokens * outer.rates['output']
        )
        header = headers.get('x-litellm-response-cost')
        try:
            cost = float(header) if header is not None else estimate
        except (ValueError, TypeError):
            cost = estimate
        if not math.isfinite(cost) or cost < 0:
            cost = estimate
            header = None
        if not usage:
            cost = reserve
        outer.budget.settle(reserve, cost)
        row.update(
            status='returned',
            seconds=round(time.monotonic() - start, 3),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached,
            cost_usd=cost,
            configured_rate_estimate_usd=estimate,
            cost_source=(
                'gateway_header'
                if header is not None
                else 'configured_rates' if usage else 'unknown_reserved'
            ),
            response_model=metadata.get('model_name'),
            retries=headers.get('x-litellm-attempted-retries'),
            fallbacks=headers.get('x-litellm-attempted-fallbacks'),
            finish_reason=metadata.get('finish_reason'),
        )
        row['tool_call_count'] = len(message.tool_calls)
        known_tools = set().union(*ROLE_TOOLS.values()) | {'complete_job', 'complete_assignment'}
        row['tool_names'] = [
            call['name'] if call['name'] in known_tools else 'unknown_tool'
            for call in message.tool_calls
        ]
        row['invalid_tool_call_count'] = len(message.invalid_tool_calls)
        outer.calls.append(row)
        print(
            json.dumps(
                {
                    'model': outer.name,
                    'role': role,
                    'tokens': input_tokens + output_tokens,
                    'usd': round(cost, 6),
                    'seconds': row['seconds'],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if str(row['fallbacks'] or '0') not in ('0', '0.0'):
            raise ValueError('閘道實際執行 fallback；此測試不能視為指定模型結果。')
        # Never persist arbitrary provider headers in checkpoints.
        return message.model_copy(update={'response_metadata': metadata})


class MeteredModel:
    def __init__(self, model, name, rates, budget):
        self.model, self.name, self.rates, self.budget = model, name, rates, budget
        self.calls = []

    def bind_tools(self, tools, **kwargs):
        bound = self.model.bind_tools(tools, **kwargs)
        names = {t.name for t in tools}
        role = 'coordinator' if names == {'complete_job'} or any(name.startswith('delegate_') for name in names) else next(
            role
            for role, allowed in ROLE_TOOLS.items()
            if allowed == names - {'complete_job', 'complete_assignment'}
        )
        schemas = [t.tool_call_schema.model_json_schema() for t in tools]
        return self.measured(bound, role, schemas)

    async def analyze(self, messages, **options):
        return await self.measured(
            AnalysisInvocation(self.model, **options), 'auxiliary_analysis', [],
        ).ainvoke(messages)

    async def summarize(self, messages):
        return await self.measured(
            AnalysisInvocation(self.model, 'summarize'), 'summary', [],
        ).ainvoke(messages)

    def measured(self, bound, role, schemas):
        return MeteredInvocation(self, bound, role, schemas)


TASKS = {
    'documents': '讀取 demo/source.txt，依內容建立 report.docx 工程週報及恰好兩頁的 briefing.pptx。'
    '兩份均須寫明本週完成基礎施工及下週鋼筋檢查；保留沒有確切期限的限制。只製作草稿，不存回資料夾、不建立行事曆。',
    'spreadsheet': '使用 demo/source.xlsx 的 工程 工作表第1欄第1至3列做數值統計。'
    '請實際使用統計工具，將常數數值合計與筆數寫入 result.xlsx 的 結果 工作表，'
    'A1=合計、B1=工具計算的合計；A2=筆數、B2=工具計算的常數數值筆數。'
    '另請審核 Agent 實際開啟產出核對數值。只製作草稿，不存回NAS或EIP。',
    'word_edit': '讀取 demo/source.docx，僅將原有「進度：待確認」文字 run 改為「進度：已確認」，'
    '用局部修改工具另存 revised.docx，保留其他內容與格式，不能重新製作整份。請獨立審核實際開啟核對。不要存回 NAS。',
    'slides_edit': '讀取 demo/source.pptx，僅將原有「進度：待確認」文字 run 改為「進度：已確認」，'
    '用局部修改工具另存 revised.pptx，保留其他內容與格式，不能重新製作整份。請獨立審核實際開啟核對。不要存回 NAS。',
}


def verify(agent, result, task, owner='benchmark'):
    artifacts = {}
    for item in result['files']:
        name, _, data = agent.store.read_artifact(owner, item['id'], result['job_id'])
        artifacts[name] = data
    checks = {
        'done': result['status'] == 'done',
        'no_publish': not result.get('proposals'),
        'source_read': bool(result.get('sources')),
    }
    if agent.settings.require_artifact_review:
        checks['all_artifacts_reviewed'] = (
            bool(artifacts) and not result['artifact_review']['unreviewed_ids']
        )
    if task == 'documents':

        for name, suffix in [('report.docx', '.docx'), ('briefing.pptx', '.pptx')]:
            text = (
                ''.join(b['text'] for b in extract(artifacts[name], suffix))
                if name in artifacts
                else ''
            )
            checks[name] = bool(text) and '基礎施工' in text and '鋼筋檢查' in text
        if 'briefing.pptx' in artifacts:
            with zipfile.ZipFile(io.BytesIO(artifacts['briefing.pptx'])) as z:
                checks['two_slides'] = (
                    sum(bool(re.fullmatch(r'ppt/slides/slide\d+\.xml', n)) for n in z.namelist())
                    == 2
                )
        checks['no_calendar'] = not any(n.endswith('.csv') for n in artifacts)
    elif task == 'spreadsheet':
        checks['values'] = False
        if 'result.xlsx' in artifacts:
            page = read_spreadsheet(artifacts['result.xlsx'], sheet='結果')
            values = {
                cell['coordinate']: cell.get('value')
                for row in page['rows']
                for cell in row['cells']
            }
            # Accept formula output only when this exact artifact came from
            # the sandboxed, recalculating report tool, never arbitrary caches.
            with agent.store.db() as db:
                receipts = db.execute(
                    "SELECT result FROM tool_receipts WHERE owner=%s AND job=%s AND name='create_spreadsheet_report' AND result IS NOT NULL",
                    (owner, result['job_id']),
                ).fetchall()
            verified_ids = {json.loads(r[0])['id'] for r in receipts}
            if any(
                f['id'] in verified_ids and f.get('name') == 'result.xlsx' for f in result['files']
            ):
                values = {
                    cell['coordinate']: (
                        cell.get('cached_value') if cell['type'] == 'f' else cell.get('value')
                    )
                    for row in page['rows']
                    for cell in row['cells']
                }
            checks['values'] = (
                values.get('A1') == '合計'
                and values.get('B1') == 30
                and values.get('A2') == '筆數'
                and values.get('B2') == 2
            )
        checks['review_returned'] = result.get('review_status') == 'advisory_returned'
    else:

        suffix = '.docx' if task == 'word_edit' else '.pptx'
        name = 'revised' + suffix
        source = Path(agent.settings.roots[0].path) / ('source' + suffix)
        checks['replacement'] = False
        checks['other_parts_unchanged'] = False
        if name in artifacts:
            text = ''.join(b['text'] for b in extract(artifacts[name], suffix))
            checks['replacement'] = (
                '進度：已確認' in text and '進度：待確認' not in text and '保留此段內容' in text
            )
            changed = 'word/document.xml' if suffix == '.docx' else 'ppt/slides/slide1.xml'
            with (
                zipfile.ZipFile(source) as old,
                zipfile.ZipFile(io.BytesIO(artifacts[name])) as new,
            ):
                checks['other_parts_unchanged'] = set(old.namelist()) == set(
                    new.namelist(),
                ) and all(old.read(n) == new.read(n) for n in old.namelist() if n != changed)
        with agent.store.db() as db:
            checks['used_edit_tool'] = bool(
                db.execute(
                    'SELECT 1 FROM tool_receipts WHERE job=%s AND name=%s AND result IS NOT NULL',
                    (result['job_id'], 'edit_text_run'),
                ).fetchone(),
            )
        checks['review_returned'] = result.get('review_status') == 'advisory_returned'
    with agent.store.db() as db:
        checks['no_external_write_proposal'] = (
            db.execute(
                'SELECT count(*) FROM proposals WHERE job=%s', (result['job_id'],),
            ).fetchone()[0]
            == 0
        )
    return checks


@evaluation
async def run(args):
    if not args.live or not args.ack_use_webui_key:
        raise ValueError(
            '實測需要 --live 與 --ack-use-webui-key；將使用既有一般 WebUI 閘道 key，不使用公文免扣額度 key。',
        )
    if not getattr(args, 'owner', None) or not os.getenv('COMPANY_AGENT_DB_SCHEMA', '').startswith(
        'company_agent_test_',
    ):
        raise ValueError(
            '舊版比較工具須指定 --owner 及隔離測試 schema；一般情境請用 company_agent.evals.scenario_suite。',
        )
    BilledMeter = _agent_scenario_suite.attribute('BilledMeter')

    user = await _webui_imports.verified_user(args.owner)
    env = read_service('webui', config_path(args.config))
    bases = env.get('OPENAI_API_BASE_URLS', '').split(';')
    keys = env.get('OPENAI_API_KEYS', '').split(';')
    if (
        len(bases) != 1
        or len(keys) != 1
        or not bases[0].startswith(('http://127.0.0.1:', 'http://localhost:'))
    ):
        raise ValueError('測試只允許單一本機 LiteLLM 端點，未自動選擇其他端點。')
    configured = yaml.safe_load(Path('examples/litellm/config.yaml').read_text())
    rates = {}
    for item in configured['model_list']:
        p = item['litellm_params']
        if p.get('input_cost_per_token') is not None:
            rates[item['model_name']] = {
                'input': p['input_cost_per_token'],
                'output': p['output_cost_per_token'],
                'cache': p.get('cache_read_input_token_cost', p['input_cost_per_token']),
            }
    if any(name not in rates for name in args.models):
        raise ValueError('模型缺少明確設定費率，未呼叫。')
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    private = output / 'state'
    make_state_directory(private)
    budget = Budget(args.budget_usd)
    report = {
        'synthetic_only': True,
        'models': args.models,
        'budget_usd': args.budget_usd,
        'compact_prompts': args.compact_prompts,
        'graph_version': GRAPH_VERSION,
        'max_steps': args.max_steps,
        'require_artifact_review': args.require_review,
        'structured_completion': args.structured_completion,
        'tasks': args.tasks,
        'configured_rates_per_token': {m: rates[m] for m in args.models},
        'runs': [],
        'pricing_notice': '閘道費率估算，不是 Azure 最終帳單；未知失敗請求保留預算。',
    }
    track_report(output / 'report.json', report)
    for name in args.models:
        for task in args.tasks:
            instruction = TASKS[task]
            folder = output / (name + '_' + task)
            folder.mkdir(mode=0o700)
            docs = folder / 'sources'
            docs.mkdir()
            (docs / 'source.txt').write_text(
                '合成測試工程：本週完成基礎施工，下週安排鋼筋檢查。\n公文沒有確切期限，不要建立行事曆。\n',
                encoding='utf-8',
            )
            (docs / 'source.xlsx').write_bytes(
                create_spreadsheet([{'name': '工程', 'rows': [[10], [20], ['未提供']]}]),
            )
            if task == 'word_edit':
                (docs / 'source.docx').write_bytes(
                    create_word(
                        '合成測試文件',
                        [
                            {'text': '進度：待確認', 'heading': False},
                            {'text': '保留此段內容', 'heading': False},
                        ],
                    ),
                )
            if task == 'slides_edit':
                (docs / 'source.pptx').write_bytes(
                    create_slides(
                        [
                            {'title': '合成測試簡報', 'bullets': ['進度：待確認', '保留此段內容']},
                            {'title': '保留第二頁', 'bullets': ['保留此段內容']},
                        ],
                    ),
                )
            settings = Settings(
                state_dir=str(private.resolve()),
                max_steps=args.max_steps,
                compact_prompts=args.compact_prompts,
                require_artifact_review=args.require_review,
                require_user_billing=True,
                structured_completion=args.structured_completion,
                roots=[
                    Root(
                        name='demo',
                        path=str(docs.resolve()),
                        readers=[args.owner],
                        model_access=True,
                    ),
                ],
            )
            gateway = own_model(_models_imports.gateway_for_verified_user(user, bases[0], keys[0], name))
            gateway.client.include_response_headers = True
            meter = BilledMeter(gateway, name, rates[name], budget)
            agent = DocumentAgent(settings, meter)
            start = time.monotonic()
            try:
                result = await agent.run(args.owner, instruction)
                checks = verify(agent, result, task, args.owner)
                downloads = folder / 'downloads'
                downloads.mkdir(mode=0o700)
                for item in result['files']:
                    filename, _, data = agent.store.read_artifact(
                        args.owner, item['id'], result['job_id'],
                    )
                    target = downloads / (item['id'] + '_' + filename)
                    if target.parent != downloads:
                        raise ValueError('不合法的測試產出檔名。')
                    with target.open('xb') as output_file:
                        output_file.write(data)
                    item['download_path'] = str(target)
            finally:
                await release_model(gateway)
            row = {
                'model': name,
                'task': task,
                'seconds': round(time.monotonic() - start, 3),
                'status': result['status'],
                'checks': checks,
                'passed': all(checks.values()),
                'calls': meter.meter.calls,
                'input_tokens': sum(c.get('input_tokens', 0) for c in meter.meter.calls),
                'output_tokens': sum(c.get('output_tokens', 0) for c in meter.meter.calls),
                'cost_usd': sum(c.get('cost_usd', 0) for c in meter.meter.calls),
                'unknown_reserved_usd': sum(
                    c.get('reserved_unknown_usd', 0) for c in meter.meter.calls
                ),
                'model_steps': result['model_steps'],
                'agent_visits': result['agent_visits'],
                'answer': result['answer'],
                'files': result['files'],
            }
            report['runs'].append(row)
            report['budget_accounted_usd'] = budget.charged
            write_json(output / 'report.json', report)
            print(
                json.dumps(
                    {
                        'completed': name + '/' + task,
                        'passed': row['passed'],
                        'usd': row['cost_usd'],
                        'checks': checks,
                        'seconds': row['seconds'],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    print(
        json.dumps({'report': str(output / 'report.json'), 'budget_accounted_usd': budget.charged}),
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument(
        '--owner', required=True, help='Server-verified WebUI user UUID for personal billing',
    )
    parser.add_argument('--config', type=Path)
    parser.add_argument(
        '--models', nargs='+', default=['DeepSeek-V4-Flash', 'Kimi-K2.5', 'grok-4.6'],
    )
    parser.add_argument(
        '--tasks', nargs='+', choices=list(TASKS), default=['documents', 'spreadsheet'],
    )
    parser.add_argument('--budget-usd', type=float, default=0.5)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--ack-use-webui-key', action='store_true')
    parser.add_argument('--compact-prompts', action='store_true')
    parser.add_argument('--require-review', action='store_true')
    parser.add_argument('--structured-completion', action='store_true')
    parser.add_argument('--max-steps', type=int, default=12, choices=range(1, 31))
    args = parser.parse_args()
    if not 0 < args.budget_usd <= 1:
        parser.error('budget-usd 必須介於 0 至 1 美元')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
