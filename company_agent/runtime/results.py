"""Project a saved execution into the stable, owner-scoped public job result."""
from __future__ import annotations

import json
from functools import reduce
from itertools import chain

from langchain_core.messages import AIMessage
from langchain_core.messages import filter_messages
from langchain_core.messages import ToolMessage
from langchain_core.messages.ai import add_usage

from company_agent.agents.registry import capability_catalog
from company_agent.agents.registry import ROLE_TOOLS
from company_agent.config import MAX_STEPS
from company_agent.runtime.completion import review_evidence
from company_agent.services.cad.sandbox import NOTICE as CAD_NOTICE
from company_agent.services.cad.sandbox import read_results as cad_read_results
from company_agent.services.eip.reading import read_results as eip_read_results
from company_agent.services.external_tools.evidence import read_results as external_read_results
from company_agent.services.research.web import merge_web_sources
from company_agent.state import GRAPH_VERSION
from company_agent.state import merge_sources


def execution_summary(state):
    """Project business evidence and usage; never return the agent's raw transcript."""
    messages = state.get('messages', [])
    reports = list(state.get('reports', {}).values())
    notices = []
    tools = filter_messages(messages, include_types=[ToolMessage])
    model_messages = filter_messages(messages, include_types=[AIMessage])
    receipts = {name: [] for name in ('read_cad', 'eip_read_attachment', 'propose_publish')}
    for message in tools:
        if message.status == 'success' and message.name in receipts:
            receipts[message.name].append(json.loads(message.content))
    notices.extend(
        receipt['notice'] for receipt in receipts['propose_publish']
        if receipt['status'] == 'rejected' and 'notice' in receipt
    )
    external_reads = external_read_results(
        json.loads(message.content) for message in tools
        if message.status == 'success' and (message.name or '').startswith('external_')
    )
    names = {message.name for message in tools}
    if names & {'read_cad', 'create_cad', 'edit_cad_text'}:
        notices.append(CAD_NOTICE)
    if 'read_ocr' in names:
        from company_agent.services.office.ocr import NOTICE
        notices.append(NOTICE)
    if 'understand_image' in names:
        from company_agent.services.office.vision import VISION_NOTICE
        notices.append(VISION_NOTICE)
    if any(message.status == 'success' and message.name in ROLE_TOOLS['eip'] for message in tools):
        notices.append('EIP 查詢／下載可能更新已讀；沒有簽核。行事曆需至公司助理工作台預覽並由本人確認，模型不會自動匯入。')
    result = {
        'sources': state.get('sources', []),
        'web_sources': state.get('web_sources', []),
        'notices': notices,
        'read_results': [*cad_read_results(receipts['read_cad']), *eip_read_results(receipts['eip_read_attachment']), *external_reads],
        'tool_feedback': [{'tool': m.name, 'message': m.content} for m in tools if m.status == 'error'],
        'model_calls': [
            {
                'step': m.response_metadata['model_step'], 'agent': m.name, 'usage': m.usage_metadata or {},
                'latency_seconds': m.response_metadata.get('latency_seconds'),
                'model': m.response_metadata.get('model_name'),
            }
            for m in model_messages if 'model_step' in m.response_metadata
        ],
    }
    for report in reports:
        for key in ('notices', 'read_results', 'tool_feedback', 'model_calls'):
            result[key].extend(report.get(key, []))
        result['sources'] = merge_sources(result['sources'], report.get('sources', []))
        result['web_sources'] = merge_web_sources(result['web_sources'], report.get('web_sources', []))
    return result


def finish_run(settings, store, owner, job, snapshot, limit, *, failure=None):
    state = snapshot.values
    completion = state.get('completion')
    interrupts = [i.value for task in snapshot.tasks if not task.result for i in task.interrupts]
    # An interrupted child has not returned a tool result yet. Its approval
    # carries the public evidence projection, persisted by native interrupt.
    reports = {**state.get('reports', {}), **{i['delegation_id']: i['report'] for i in interrupts}}
    summary = execution_summary({**state, 'reports': reports})
    files = store.deliverable_files(owner, job)
    reviews = review_evidence(state)
    reviewed = {(item['id'], item['sha256']) for item in reviews}
    unreviewed = [f['id'] for f in files if (f['id'], f['sha256']) not in reviewed]
    incomplete = completion and completion['outcome'] != 'completed'
    if settings.require_artifact_review and unreviewed and not interrupts and failure is None and not incomplete:
        raise ValueError('產出尚未取得獨立審核的實際讀取回條。')
    answer = state['messages'][-1].text
    if completion:
        answer = completion['summary']
    if interrupts:
        answer = '檔案草稿已保留；請確認目的地後核准或拒絕存檔。'
    status = 'awaiting_approval' if interrupts else 'done'
    if not interrupts and completion and completion['outcome'] != 'completed':
        status = completion['outcome']
    if failure is not None:
        status, answer = 'error', failure
    calls = sorted(summary.pop('model_calls'), key=lambda call: call['step'])
    with store.db() as db:
        aux = [
            json.loads(row[0]).get('usage', {}) for row in db.execute(
                "SELECT result FROM tool_receipts WHERE owner=%s AND job=%s AND name IN ('billed_analysis','compact_conversation') AND result IS NOT NULL",
                (owner, job),
            ).fetchall()
        ]
    usage = reduce(add_usage, chain((call['usage'] for call in calls), aux), add_usage(None, None))
    used = store.attempts(job)
    result = {
        **summary,
        'answer': answer,
        'budget': {'used': used, 'limit': limit, 'maximum': MAX_STEPS},
        'proposals': [{k: v for k, v in i.items() if k not in {'report', 'delegation_id'}} for i in interrupts],
        'usage': {key: usage[key] for key in ('input_tokens', 'output_tokens')},
        'model_steps': used,
        'resumable': bool(snapshot.next),
        'framework': 'langgraph', 'architecture': 'single_agent' if settings.orchestration_mode == 'chat' else 'multi_agent', 'graph_version': GRAPH_VERSION,
        'orchestration_mode': settings.orchestration_mode,
        'office_execution_mode': settings.office_execution_mode,
        'model_calls': calls,
        'delivery_validation': store.validations(owner, job),
        'context_mode': 'per_delegation',
        'completion_protocol': 'typed_tool' if settings.structured_completion else 'text',
        'capabilities': capability_catalog(),
        'review_status': 'advisory_returned' if reviews else 'not_completed',
        'artifact_review': {
            'required': settings.require_artifact_review,
            'read_by_reviewer': reviews, 'unreviewed_ids': unreviewed,
            'scope': '僅證明審核讀取過文字／數值區塊，不等同全文覆蓋、視覺合格或批准存檔。',
        },
        'agent_visits': [call['agent'] for call in calls],
        'delegations': [
            {
                'id': call['id'], 'agent': reports[call['id']]['agent'], 'status': reports[call['id']]['status'],
                'resolves_assignments': reports[call['id']].get('resolves_assignments', []),
                'revises_assignments': reports[call['id']].get('revises_assignments', []),
                'work_key': reports[call['id']].get('work_key'), 'outputs': reports[call['id']].get('outputs', []),
            }
            for message in state['messages'] if isinstance(message, AIMessage)
            for call in message.tool_calls if call['id'] in reports
        ],
    }
    store.finish(owner, job, status, result)
    return store.result(owner, job)
