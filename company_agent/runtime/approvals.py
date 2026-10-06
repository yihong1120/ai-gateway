"""Native publication interrupt; writes require an authenticated resume."""
from __future__ import annotations

from langgraph.runtime import Runtime
from langgraph.types import interrupt

from company_agent.runtime.context import Context
from company_agent.runtime.results import execution_summary
from company_agent.runtime.tools import tool_receipt
from company_agent.services.files.nas import file_access
from company_agent.state import State


def publish_approval(state: State, runtime: Runtime[Context], role):
    ctx, pending = runtime.context, state['pending']
    proposal = ctx.store.proposal(ctx.owner, ctx.job, pending['proposal_id'])
    # DocumentAgent.resume validates the decision before consuming this interrupt.
    decision = interrupt({
        'type': 'publish_approval', **proposal, 'delegation_id': ctx.delegation_id,
        'report': {**execution_summary(state), 'agent': role, 'status': 'awaiting_approval'},
    })
    if decision['decision'] == 'reject':
        ctx.store.reject(ctx.owner, ctx.job, pending['proposal_id'])
        value = {
            'status': 'rejected',
            'notice': '使用者拒絕存檔；草稿保留，不要重新提出相同申請。',
        }
    else:
        access = file_access(ctx.settings, ctx.store, ctx.owner)
        value = ctx.store.confirm(ctx.owner, pending['proposal_id'], access, replay_ok=True)
    # The official reducer replaces the proposal receipt after a successful decision.
    return {
        'messages': [tool_receipt(pending['tool_call_id'], 'propose_publish', value)],
        'pending': None,
    }
