"""Paired Flash/Argon data-role replacement evaluation; offline by default.

Uses only synthetic files, verified dev-user billing and disposable PostgreSQL
schemas. Role tests isolate the actual data agent; workflow tests run the real
company graph. It never changes production routing or promotes a candidate.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import fcntl
import json
import math
import os
import random
import statistics
import time
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from company_agent.evals.runner import JobBudget as JobBudget
from company_agent.evals.runner import evaluation, own_sandbox, own_model, release_model, track_report, write_json
from company_agent.evals.data_replacement_cases import BY_ID, digest, fingerprint, grade, prepare, role_plan


ROOT = Path(__file__).resolve().parents[2]
VERSION = 'data-replacement-v1.3'
ARMS = {'flash': 'gemini-3.8-flash', 'argon': 'gemini-4-argon'}
LITE = 'gemini-3.5-flash-lite'
ROLES = ('coordinator', 'knowledge', 'data', 'document', 'reviewer', 'research', 'engineering', 'eip')


def schedule(cases, repeats, seed):
    """Each pair stays together; AB/BA order is balanced within each repeat."""
    rng = random.Random(seed)
    planned = []
    for repeat in range(1, repeats + 1):
        shuffled = list(cases)
        rng.shuffle(shuffled)
        for index, case in enumerate(shuffled):
            arms = ('flash', 'argon') if (index + repeat) % 2 else ('argon', 'flash')
            planned.append({'case': case.id, 'repeat': repeat, 'seed': seed + repeat * 101,
                            'lane': case.lane, 'arms': list(arms)})
    return planned


def source_version():
    paths = [*sorted((ROOT / 'company_agent').rglob('*.py')),
             *sorted((ROOT / 'company_agent/agents').rglob('*.md')), ROOT / 'examples/litellm/config.yaml']
    return {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in paths}


def manifest(cases, repeats, seed, concurrency):
    pairs = schedule(cases, repeats, seed)
    return {'version': VERSION, 'live': False, 'paid_calls': 0,
            'cases': [c.public() for c in cases], 'models': ARMS, 'other_role_model': LITE,
            'orchestration_mode': 'hybrid', 'office_execution_mode': 'specialist',
            'role_concurrency': concurrency, 'workflow_concurrency': 1,
            'provider_failure_policy': {'request_retries': 0, 'replay_failed_jobs': False,
                                        'consecutive_failed_jobs_per_model': 3,
                                        'finish_started_pair_unless_hard_failure': True},
            'repeats': repeats, 'seed': seed, 'planned_jobs': len(pairs) * 2, 'pairs': pairs,
            'decision_rules': {'minimum_observed_positive_acceptance': 0.95,
                               'maximum_observed_acceptance_loss': 0.05,
                               'control_acceptance': 1.0, 'critical_failures': 0,
                               'meaningful_cost_or_median_latency_ratio': 0.8,
                               'scope': 'provisional within tested cases; not statistical proof of universal equivalence'},
            'source_sha256': source_version()}


def fixture_inputs(report_path, case, seed):
    """Reuse the baseline's exact source bytes and private oracle, never transcripts."""
    report_path = report_path.resolve()
    report = json.loads(report_path.read_text())
    if not report.get('completed_at') or report.get('unrun'):
        raise ValueError('Fixture baseline must be complete.')
    definition = next((item for item in report['cases'] if item['id'] == case.id), None)
    if definition != json.loads(json.dumps(case.public())):
        raise ValueError('Baseline fixture case definition differs from the current test.')
    rows = [row for row in report['runs'] if row['case'] == case.id and row['seed'] == seed]
    if not rows or any(row['source_sha256'] != rows[0]['source_sha256'] for row in rows):
        raise ValueError('Baseline fixture pair is missing or has inconsistent source hashes.')
    row = rows[0]
    folder = (report_path.parent / row['folder']).resolve()
    if not folder.is_relative_to(report_path.parent):
        raise ValueError('Fixture folder escapes its report directory.')
    sources = folder / 'sources'
    if not sources.resolve().is_relative_to(folder) or not (folder / 'expected.private.json').resolve().is_relative_to(folder):
        raise ValueError('Fixture paths escape their job directory.')
    if fingerprint(sources) != row['source_sha256']:
        raise ValueError('Baseline fixture source hashes changed.')
    values = {}
    for name in row['source_sha256']:
        path = sources / name
        if Path(name).name != name or not path.resolve().is_relative_to(folder):
            raise ValueError('Unsafe fixture source path.')
        values[name] = path.read_bytes()
    return values, json.loads((folder / 'expected.private.json').read_text())


def export_pack(output, cases, repeats, seed, concurrency):
    output.mkdir(parents=True, exist_ok=False)
    plan = manifest(cases, repeats, seed, concurrency)
    write_json(output / 'manifest.json', plan)
    headers = ('id', 'title', 'lane', 'category', 'control', 'rationale', 'prompt', 'outputs')
    with (output / 'cases.csv').open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=headers)
        writer.writeheader()
        for case in cases:
            row = case.public()
            writer.writerow({key: ', '.join(case.outputs) if key == 'outputs' else row[key] for key in headers})
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    book = Workbook()
    sheet = book.active
    sheet.title = '案例'
    sheet.append(list(headers))
    for case in cases:
        row = case.public()
        sheet.append([', '.join(case.outputs) if key == 'outputs' else row[key] for key in headers])
    sheet.freeze_panes = 'A2'
    sheet.auto_filter.ref = sheet.dimensions
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical='top', wrap_text=True)
    for column, width in {'A': 10, 'B': 30, 'C': 15, 'D': 22, 'E': 12, 'F': 35, 'G': 100, 'H': 36}.items():
        sheet.column_dimensions[column].width = width
    book.save(output / 'cases.xlsx')
    with zipfile.ZipFile(output / 'synthetic_inputs.zip', 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for case in cases:
            files, gold = prepare(case, seed + 101)
            for name, data in files.items():
                archive.writestr(case.id + '/sources/' + name, data)
            archive.writestr(case.id + '/prompt.txt', case.prompt)
            # This is a human/evaluator bundle. The live runner places this
            # private oracle outside every agent's allowed source root.
            archive.writestr(case.id + '/evaluator_only/gold.json', json.dumps(gold, ensure_ascii=False, indent=2))
    return plan


class MissingSheetOnce:
    """One recorded, identical parser fault per recovery job; no global patch."""
    def __init__(self, original):
        self.original, self.applied = original, False

    async def invoke(self, runtime, **kwargs):
        if not self.applied:
            self.applied = True
            kwargs = {**kwargs, 'sheet': '__injected_missing_sheet__'}
        return await self.original(runtime=runtime, **kwargs)


async def run_role(settings, meter, owner, case, store, setup_lock, diagnostics=None):
    from langchain.agents import create_agent
    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langsmith import tracing_context
    from company_agent.agents.registry import ROLE_TOOLS
    from company_agent.graph import specialist_tools
    from company_agent.runtime.completion import completion_tool
    from company_agent.runtime.context import Context
    from company_agent.runtime.middleware import CompanyPolicy
    from company_agent.runtime.models import model_failure_message, routed_model, select_model
    from company_agent.runtime.results import finish_run
    from company_agent.state import GRAPH_VERSION

    job = store.job(owner)
    store.step_budget(owner, job, settings.max_steps)
    store.delivery_plan(owner, job, role_plan(case, {}))
    call_id = 'controlled_data_' + job
    store.claim_assignment(owner, job, call_id, case.id.lower(),
                           {'role': 'data', 'task': case.prompt, 'context': '', 'outputs': list(case.outputs)})
    model = routed_model(meter, settings)
    ctx = Context(owner, job, settings, store, model, delegation_id=call_id,
                  role='data', assignment_outputs=case.outputs)
    tools = {t.name: t for t in specialist_tools()}
    selected = [tools[name] for name in sorted(ROLE_TOOLS['data'])]
    fault = None
    if case.fault == 'missing_sheet':
        original = next(t for t in selected if t.name == 'analyze_spreadsheet')
        fault = MissingSheetOnce(original.coroutine)
        selected = [t.model_copy(update={'coroutine': fault.invoke}) if t.name == original.name else t for t in selected]
    serde = EncryptedSerializer.from_pycryptodome_aes(
        serde=JsonPlusSerializer(pickle_fallback=False, allowed_json_modules=[]),
        key=(store.directory / 'checkpoint.key').read_bytes())
    async with AsyncPostgresSaver.from_conn_string(store.conninfo, serde=serde) as saver:
        async with setup_lock:
            await saver.setup()
        graph = create_agent(select_model(model, 'data'), [*selected, completion_tool('data')],
                             middleware=[CompanyPolicy('data', True)], context_schema=Context,
                             checkpointer=saver, name='data')
        config = {'configurable': {'thread_id': job}, 'recursion_limit': settings.max_steps * 5 + 10}
        try:
            with tracing_context(enabled=False):
                await graph.ainvoke({'messages': [HumanMessage(content=json.dumps({
                    'user_request': case.prompt, 'task': case.prompt,
                    'selected_context': '交付檔名規格已由伺服器登記，無須重複呼叫 plan_delivery；依 task 的全部要求讀取、計算與產檔，不能改寫既有規格。',
                    'outputs': list(case.outputs), 'work_key': case.id.lower(),
                }, ensure_ascii=False))], 'graph_version': GRAPH_VERSION}, config, context=ctx)
        except (Exception, asyncio.CancelledError) as error:
            try:
                snapshot = await graph.aget_state(config)
                finish_run(settings, store, owner, job, snapshot, settings.max_steps,
                           failure=model_failure_message(error) or '本次測試中止；部分成果與來源回條已保留。')
            except Exception as saving_error:
                print(json.dumps({'diagnostic_save_error_type': type(saving_error).__name__}), flush=True)
            raise
        finally:
            if diagnostics is not None:
                diagnostics['fault_applied'] = bool(fault and fault.applied)
        snapshot = await graph.aget_state(config)
        result = finish_run(settings, store, owner, job, snapshot, settings.max_steps)
        result['architecture'] = 'isolated_data_role'
        result['context_mode'] = 'controlled_assignment'
        return result, bool(fault and fault.applied)


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def summarize(rows):
    summary = {}
    for arm in ARMS:
        subset = [r for r in rows if r['arm'] == arm]
        by_lane = {}
        for lane in ('role', 'workflow'):
            for control in (False, True):
                group = [r for r in subset if r['lane'] == lane and r['control'] is control]
                if not group:
                    continue
                accepted = sum(r['passed'] for r in group)
                charged = sum(r['accounted_usd'] for r in group)
                by_lane[f'{lane}_{"control" if control else "positive"}'] = {
                    'n': len(group), 'passed': accepted, 'acceptance': accepted / len(group),
                    'median_seconds': statistics.median(r['seconds'] for r in group),
                    'p95_seconds': percentile([r['seconds'] for r in group], 0.95),
                    'accounted_usd': charged, 'usd_per_accepted': charged / accepted if accepted else None,
                    'returned_cost_usd': sum(c.get('cost_usd', 0) for r in group for c in r['calls']),
                    'unknown_reserved_usd': sum(c.get('reserved_unknown_usd', 0) for r in group for c in r['calls']),
                }
        summary[arm] = {'groups': by_lane, 'n': len(subset), 'passed': sum(r['passed'] for r in subset),
                        'critical_failures': sum(bool(r.get('critical_failures')) for r in subset),
                        'errors': sum(r['status'] == 'error' for r in subset),
                        'accounted_usd': sum(r['accounted_usd'] for r in subset)}
    indexed = {(r['case'], r['repeat'], r['arm']): r for r in rows}
    pairs = []
    for case_id, repeat in sorted({(r['case'], r['repeat']) for r in rows}):
        if (case_id, repeat, 'flash') not in indexed or (case_id, repeat, 'argon') not in indexed:
            continue
        flash, argon = indexed[(case_id, repeat, 'flash')], indexed[(case_id, repeat, 'argon')]
        pairs.append({'case': case_id, 'repeat': repeat, 'both_pass': flash['passed'] and argon['passed'],
                      'flash_only_pass': flash['passed'] and not argon['passed'],
                      'argon_only_pass': argon['passed'] and not flash['passed'],
                      'both_fail': not flash['passed'] and not argon['passed'],
                      'same_sources': flash['source_sha256'] == argon['source_sha256']})
    return {'arms': summary, 'pairs': pairs,
            'paired_counts': {key: sum(p[key] for p in pairs) for key in ('both_pass', 'flash_only_pass', 'argon_only_pass', 'both_fail')},
            'all_pairs_have_identical_sources': all(p['same_sources'] for p in pairs)}


def classify(case, row):
    calls = row['calls']
    if row['status'] == 'error':
        if row.get('error_type') in {'TimeoutError', 'APITimeoutError'}:
            return 'provider_or_timeout'
        if any(c.get('error_type') in {'BadRequestError', 'NotFoundError', 'UnprocessableEntityError'} for c in calls):
            return 'gateway_configuration'
        if any(c.get('error_type') in {'APITimeoutError', 'TimeoutError', 'RateLimitError', 'APIConnectionError',
                                     'AuthenticationError', 'PermissionDeniedError'} for c in calls):
            return 'provider_or_timeout'
        return row.get('failure_stage', 'execution')
    if row['passed']:
        return 'accepted_control' if case.control else 'accepted'
    if not any(c.get('role') == 'data' for c in calls):
        return 'coordinator_before_data'
    result = row.get('result', {})
    if any(d['agent'] == 'data' and d['status'] != 'completed' for d in result.get('delegations', [])):
        return 'data_assignment_incomplete'
    if case.lane == 'role':
        return 'data_output_or_contract'
    if any(d['agent'] in {'document', 'reviewer'} and d['status'] != 'completed' for d in result.get('delegations', [])):
        return 'downstream_assignment'
    return 'workflow_output_or_completion'


@asynccontextmanager
async def model_slot(directory, arm):
    """Cross-process per-model job gate; queue time is outside job latency."""
    if directory is None:
        yield
        return
    folder = Path(directory)
    if not folder.is_dir() or folder.is_symlink() or folder.stat().st_mode & 0o077:
        raise ValueError('Model gate directory must be private (0700).')
    descriptor = os.open(folder / (arm + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def provider_stop(row, streaks, limit=3):
    """New independent trials are allowed; failed requests are never retried."""
    errors = {c.get('error_type') for c in row['calls'] if c.get('status') == 'failed'}
    if errors & {'APIConnectionError', 'AuthenticationError', 'PermissionDeniedError',
                 'UnprocessableEntityError', 'BadRequestError', 'NotFoundError'}:
        return 'hard'
    if errors & {'RateLimitError', 'APITimeoutError', 'CancelledError'} or row.get('error_type') in {'TimeoutError', 'APITimeoutError'}:
        streaks[row['arm']] += 1
        if limit and streaks[row['arm']] >= limit:
            return 'new_pairs'
    else:
        streaks[row['arm']] = 0
    return None


@evaluation
async def live(args, cases):
    from examples.deployment.dev.run import load_environment
    environment = load_environment('webui')
    if args.billing_key_file:
        key_file = args.billing_key_file.resolve()
        if key_file.stat().st_mode & 0o077:
            raise ValueError('Billing key binding must be private (0600).')
        binding = json.loads(key_file.read_text())
        if binding.get('environment') != 'isolated_dev' or binding.get('gateway') != environment['OPENAI_API_BASE_URLS']:
            raise ValueError('Evaluation billing must belong to the isolated dev gateway.')
        os.environ['OPENAI_API_KEYS'] = binding['key']
    from open_webui.models.users import Users
    from company_agent.api.service import ServiceConfiguration
    from company_agent.config import ModelRoute, Root, Settings
    from company_agent.evals.benchmark import Budget
    from company_agent.evals.scenario_suite import BilledMeter, model_rates
    from examples.identity.adapters.webui import verified_user
    from company_agent.runtime.execution import DocumentAgent
    from company_agent.runtime.storage import Store
    from company_agent.evals.sandbox import make_state_directory
    import psycopg
    import yaml

    user = await Users.get_user_by_id(args.owner) or await Users.get_user_by_email(args.owner)
    if user is None:
        with psycopg.connect(environment['DATABASE_URL']) as db:
            names = db.execute('SELECT id FROM "user" WHERE lower(name)=lower(%s)', (args.owner,)).fetchall()
        if len(names) != 1:
            raise ValueError('Dev owner must resolve unambiguously.')
        user = await Users.get_user_by_id(names[0][0])
    verified = await verified_user(user.id)
    gateway = own_model(ServiceConfiguration.model(verified))
    catalog = model_rates(yaml.safe_load((ROOT / 'examples/litellm/config.yaml').read_text())['model_list'])
    if any(name not in catalog for name in (*ARMS.values(), LITE)):
        await release_model(gateway)
        raise ValueError('Every requested model must have configured rates.')
    budget = Budget(args.budget_usd)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    definition = manifest(cases, args.repeats, args.seed, args.concurrency)
    report = {**definition, 'live': True, 'paid_calls': 0, 'owner_name': verified['name'],
              'started_at': datetime.now(timezone.utc).isoformat(), 'runs': [], 'catalog': catalog,
              'limits': {'request_timeout': args.request_timeout, 'job_timeout': args.timeout,
                         'max_steps': args.max_steps, 'reasoning': args.reasoning, 'max_tokens': args.max_tokens},
              'billing_key_alias': binding['alias'] if args.billing_key_file else 'configured_dev_chat_key'}
    if args.source_report:
        report['fixture_reference'] = {'report_sha256': digest(args.source_report.read_bytes()),
                                       'scope': 'exact baseline source bytes and evaluator-only oracle; no model history reused'}
    report['provider_failure_policy']['consecutive_failed_jobs_per_model'] = args.provider_failure_limit or None
    report['provider_failure_policy']['continue_independent_cases_after_transient_failures'] = args.provider_failure_limit == 0
    report['per_model_job_concurrency'] = 1 if args.model_lock_dir else args.concurrency
    track_report(args.output / 'report.json', report)
    write_json(args.output / 'manifest.json', {k: v for k, v in report.items() if k != 'runs'})
    own_sandbox(os.environ['COMPANY_AGENT_DATABASE_URL'])
    setup_lock = asyncio.Lock()
    stopped = asyncio.Event()
    hard_stopped = asyncio.Event()
    provider_streaks = {arm: 0 for arm in ARMS}
    case_map = {c.id: c for c in cases}
    pairs = definition['pairs']
    runtime_hashes = definition['source_sha256']
    pairs_by_lane = {lane: [p for p in pairs if p['lane'] == lane] for lane in ('role', 'workflow')}

    async def run_pair(pair):
        case = case_map[pair['case']]
        source_bytes, gold = (fixture_inputs(args.source_report, case, pair['seed']) if args.source_report
                              else prepare(case, pair['seed']))
        for arm in pair['arms']:
            if hard_stopped.is_set():
                return
            async with model_slot(args.model_lock_dir, arm):
                folder = args.output / f'{case.id}_{arm}_r{pair["repeat"]}'
                folder.mkdir(mode=0o700)
                sources = folder / 'sources'
                sources.mkdir(mode=0o700)
                for name, data in source_bytes.items():
                    (sources / name).write_bytes(data)
                before = fingerprint(sources)
                write_json(folder / 'expected.private.json', gold)
                state = folder / 'state'
                make_state_directory(state)
                local_budget = JobBudget(budget)
                meter = BilledMeter(gateway, gateway.client.model_name, catalog[gateway.client.model_name], local_budget)
                meter.meter.rate_catalog = catalog
                route = ModelRoute(model=ARMS[arm], reasoning_effort=args.reasoning, max_tokens=args.max_tokens,
                                   request_timeout_seconds=args.request_timeout)
                settings = Settings(state_dir=str(state), model_name=LITE,
                                    role_models={role: route if role == 'data' else ModelRoute(
                                        model=LITE, reasoning_effort='medium', max_tokens=args.max_tokens,
                                        request_timeout_seconds=args.request_timeout) for role in ROLES},
                                    model_timeout_seconds=args.request_timeout, max_steps=args.max_steps,
                                    execution_timeout_seconds=args.timeout, compact_prompts=True,
                                    structured_completion=True, require_artifact_review=case.lane == 'workflow',
                                    require_user_billing=True, require_delivery_plan=True,
                                    orchestration_mode='hybrid', office_execution_mode='specialist',
                                    enable_spreadsheet_reports=True,
                                    roots=[Root(name='demo', path=str(sources), readers=[user.id], model_access=True)])
                store = Store(state)
                row = {'case': case.id, 'title': case.title, 'lane': case.lane, 'control': case.control,
                       'category': case.category, 'arm': arm, 'repeat': pair['repeat'], 'seed': pair['seed'],
                       'source_sha256': before, 'data_route': route.model_dump(), 'folder': folder.name,
                       'other_role_model': LITE, 'fault': case.fault}
                started = time.monotonic()
                stage, result, files, fault_applied = 'execution', {}, {}, False
                diagnostics = {}
                try:
                    async with asyncio.timeout(args.timeout):
                        if case.lane == 'role':
                            result, fault_applied = await run_role(settings, meter, user.id, case, store, setup_lock, diagnostics)
                        else:
                            result = await DocumentAgent(settings, meter, store=store).run(user.id, case.prompt)
                    stage = 'grading'
                    files = {f['name']: store.read_artifact(user.id, f['id'], result['job_id'])[2] for f in result['files']}
                    evidence = store.evidence(user.id, result['job_id'])
                    with store.db() as db:
                        receipts = [{'call_id': call_id, 'name': name, 'result': json.loads(value) if value else None}
                                    for call_id, name, value in db.execute(
                            'SELECT call_id,name,result FROM tool_receipts WHERE owner=%s AND job=%s', (user.id, result['job_id'])).fetchall()]
                    checks = grade(case, result, files, sources, before, gold, evidence, receipts, meter.meter.calls, fault_applied)
                    checks['data_route_correct'] = all(c['requested_model'] == ARMS[arm] for c in meter.meter.calls if c['role'] == 'data')
                    checks['other_role_routes_fixed'] = all(c['requested_model'] == LITE for c in meter.meter.calls if c['role'] != 'data')
                    row.update(status='returned', result=result, checks=checks, passed=all(checks.values()),
                               statistics_evidence=evidence, tool_receipts=receipts, fault_applied=fault_applied)
                except Exception as error:
                    row.update(status='error', error_type=type(error).__name__, failure_stage=stage, passed=False)
                    # Preserve partial artifacts and real receipts without exception/provider bodies.
                    with store.db() as db:
                        jobs = db.execute('SELECT id FROM jobs WHERE owner=%s ORDER BY created DESC LIMIT 1', (user.id,)).fetchone()
                    if jobs:
                        result = store.result(user.id, jobs[0])
                        files = {f['name']: store.read_artifact(user.id, f['id'], jobs[0])[2] for f in store.deliverable_files(user.id, jobs[0])}
                        row['partial_result'] = result
                        row['statistics_evidence'] = store.evidence(user.id, jobs[0])
                        with store.db() as db:
                            receipts = [{'call_id': key, 'name': name, 'result': json.loads(value) if value else None}
                                        for key, name, value in db.execute(
                                            'SELECT call_id,name,result FROM tool_receipts WHERE owner=%s AND job=%s',
                                            (user.id, jobs[0])).fetchall()]
                        row['tool_receipts'] = receipts
                        row['fault_applied'] = diagnostics.get('fault_applied', False)
                        try:
                            row['checks'] = grade(case, result, files, sources, before, gold,
                                                  row['statistics_evidence'], receipts, meter.meter.calls, row['fault_applied'])
                        except Exception as grading_error:
                            row['partial_grading_error_type'] = type(grading_error).__name__
                row.update(seconds=round(time.monotonic() - started, 3), accounted_usd=local_budget.charged, calls=meter.meter.calls)
                row['critical_failures'] = []
                checks = row.get('checks', {})
                if fingerprint(sources) != before:
                    row['critical_failures'].append('source_changed')
                if checks.get('no_forbidden_tools') is False:
                    row['critical_failures'].append('forbidden_tool_attempt')
                if result.get('status') == 'done' and any(not value for key, value in checks.items() if key.startswith(('cells_', 'verified_formula_values_'))):
                    row['critical_failures'].append('accepted_wrong_data')
                row['attribution'] = classify(case, row)
                downloads = folder / 'downloads'
                downloads.mkdir(mode=0o700)
                for name, data in files.items():
                    if Path(name).name != name:
                        raise ValueError('Unsafe artifact name.')
                    (downloads / name).write_bytes(data)
                if result.get('job_id'):
                    history = folder / 'artifact_history'
                    history.mkdir(mode=0o700)
                    for artifact in store.files(user.id, result['job_id']):
                        name, _, data = store.read_artifact(user.id, artifact['id'], result['job_id'])
                        if Path(name).name != name:
                            raise ValueError('Unsafe artifact history name.')
                        (history / (artifact['id'] + '__' + name)).write_bytes(data)
                row['artifact_sha256'] = {name: digest(data) for name, data in files.items()}
                write_json(folder / 'result.json', row)
                report['runs'].append(row)
                report['summary'] = summarize(report['runs'])
                report['accounted_usd'] = budget.charged
                report['paid_calls'] = sum(len(r['calls']) for r in report['runs'])
                write_json(args.output / 'report.json', report)
                print(json.dumps({'completed': len(report['runs']), 'planned': definition['planned_jobs'],
                                  'case': case.id, 'arm': arm, 'passed': row['passed'], 'attribution': row['attribution'],
                                  'seconds': row['seconds'], 'accounted_usd': round(local_budget.charged, 6)}, ensure_ascii=False), flush=True)
                action = provider_stop(row, provider_streaks, args.provider_failure_limit)
                if budget.exhausted or action == 'hard':
                    report['stopped_reason'] = 'budget_or_gateway_unavailable'
                    stopped.set()
                    hard_stopped.set()
                elif action == 'new_pairs':
                    report['stopped_reason'] = 'three_consecutive_provider_failures_for_one_model'
                    stopped.set()

    async def worker(queue):
        while not stopped.is_set():
            try:
                pair = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await run_pair(pair)

    try:
        queue = asyncio.Queue()
        for pair in pairs_by_lane['role']:
            queue.put_nowait(pair)
        await asyncio.gather(*(worker(queue) for _ in range(args.concurrency)))
        # The production graph serializes its own checkpoint setup lease. Keep
        # workflow jobs sequential; role-only setup above uses an async lock.
        for pair in pairs_by_lane['workflow']:
            if stopped.is_set():
                break
            await run_pair(pair)
    finally:
        report['completed_at'] = datetime.now(timezone.utc).isoformat()
        report['completed_jobs'] = len(report['runs'])
        complete = {(r['case'], r['repeat'], r['arm']) for r in report['runs']}
        report['unrun'] = [{'case': p['case'], 'repeat': p['repeat'], 'arm': arm}
                           for p in pairs for arm in p['arms'] if (p['case'], p['repeat'], arm) not in complete]
        report['source_version_unchanged'] = runtime_hashes == source_version()
        report['accounted_usd'] = budget.charged
        report['summary'] = summarize(report['runs'])
        write_json(args.output / 'report.json', report)
        await release_model(gateway)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cases', nargs='+', choices=list(BY_ID), default=list(BY_ID))
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--seed', type=int, default=20261017)
    parser.add_argument('--concurrency', type=int, choices=(1, 2), default=2)
    parser.add_argument('--provider-failure-limit', type=int, default=3, help='0 continues independent cases after transient failures.')
    parser.add_argument('--model-lock-dir', type=Path, help='Private shared directory limiting each model to one active job.')
    parser.add_argument('--source-report', type=Path, help='Reuse exact source fixtures from a completed paired baseline report.')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--owner')
    parser.add_argument('--billing-key-file', type=Path)
    parser.add_argument('--budget-usd', type=float)
    parser.add_argument('--request-timeout', type=int, default=150)
    parser.add_argument('--timeout', type=int, default=300)
    parser.add_argument('--max-steps', type=int, default=40)
    parser.add_argument('--max-tokens', type=int, default=4096)
    parser.add_argument('--reasoning', choices=('low', 'medium', 'high'), default='medium')
    args = parser.parse_args()
    if args.provider_failure_limit < 0 or args.repeats < 1 or not 1 <= args.max_steps <= 40 or not 256 <= args.max_tokens <= 16384 or not 10 <= args.request_timeout <= 300 or args.timeout <= 0:
        parser.error('Invalid repeat, step, token or timeout limit.')
    args.output = args.output.resolve()
    cases = [BY_ID[key] for key in dict.fromkeys(args.cases)]
    if args.live:
        if not args.owner or args.budget_usd is None or not math.isfinite(args.budget_usd) or args.budget_usd <= 0:
            parser.error('--live requires a verified --owner and finite positive --budget-usd.')
        report = asyncio.run(live(args, cases))
        print(json.dumps(report['summary'], ensure_ascii=False, indent=2))
    else:
        plan = export_pack(args.output, cases, args.repeats, args.seed, args.concurrency)
        print(json.dumps({'output': str(args.output), 'cases': len(cases), 'planned_jobs': plan['planned_jobs'], 'paid_calls': 0}))


if __name__ == '__main__':
    main()
