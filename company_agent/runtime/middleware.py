"""Company-specific policies around the official agent loop.

LangChain owns model/tool dispatch and argument-error feedback. This middleware
owns job limits, company prompts and publication approval.
"""
from __future__ import annotations

import asyncio

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware import hook_config
from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware import ModelResponse
from langchain_core.messages import SystemMessage
from langgraph.runtime import Runtime

from company_agent.runtime.approvals import publish_approval
from company_agent.runtime.compaction import ChatSummarization
from company_agent.runtime.context import Context
from company_agent.runtime.prompts import model_messages
from company_agent.services.files.access import FolderAccess
from company_agent.services.files.nas import file_access
from company_agent.services.files.nas import NASUnavailable
from company_agent.state import State


def visible_sources(ctx, state, role, parameters):
    if role == 'coordinator':
        return FolderAccess(
            ctx.settings.roots, ctx.owner, ctx.settings.max_file_bytes,
        ).visible_roots()
    if 'root' not in parameters:
        return []
    access = file_access(ctx.settings, ctx.store, ctx.owner)
    nas_roots = []
    for root in state.get('accessed_roots', []):
        if root.startswith('nas_'):
            nas_roots.append(root)
        else:
            access.allowed(root)  # Local revocation must stop before NAS I/O.
    # Discovery populates this operation's NAS mapping. Validate previously
    # accessed roots against that same fresh listing instead of logging in twice.
    visible = access.visible_roots()
    for root in nas_roots:
        access.allowed(root)
    return visible


class CompanyPolicy(AgentMiddleware):
    state_schema = State

    def __init__(self, role, structured_completion):
        self.role = role
        self.structured = structured_completion

    async def abefore_agent(self, state: State, runtime: Runtime[Context]):
        # Runs before this job's tools exist. A completed node is checkpointed,
        # so approval/model resumes reuse its context without another summary.
        if self.role == 'coordinator':
            return await ChatSummarization(runtime.context, runtime.stream_writer).abefore_model(state, runtime)

    @hook_config(can_jump_to=['end'])
    async def abefore_model(self, state: State, runtime: Runtime[Context]):
        if state.get('pending'):
            return publish_approval(state, runtime, self.role)
        if state.get('completion'):
            return {'jump_to': 'end'}

    async def awrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:
        state, ctx = request.state, request.runtime.context
        role = self.role
        # These fields only select prompt guidance. The model-visible schema
        # remains LangChain's tool_call_schema, with runtime injection hidden.
        parameters = {field for tool in request.tools for field in tool.get_input_schema().model_fields}
        if 'NAS' in ctx.source_errors:
            visible = FolderAccess(ctx.settings.roots, ctx.owner, ctx.settings.max_file_bytes).visible_roots()
        else:
            try:
                visible = await asyncio.to_thread(visible_sources, ctx, state, role, parameters)
            except NASUnavailable as failure:
                # NAS discovery is optional for local uploads and other sources.
                # Keep its failure scoped to this run, shared by its delegations.
                ctx.source_errors['NAS'] = str(failure)
                visible = FolderAccess(ctx.settings.roots, ctx.owner, ctx.settings.max_file_bytes).visible_roots()
        messages = await model_messages(
            role, state, ctx, visible, self.structured, {tool.name for tool in request.tools}, parameters,
        )
        if ctx.source_errors:
            messages[0] = SystemMessage(
                content=messages[0].content + '\n來源連線限制：' + '\n'.join(ctx.source_errors.values())
                + '\n本次不要重試不可用的 NAS，也不要猜測 root/path；可完成其他可用來源的工作。'
                '若任務需要 NAS，明確交回未完成部分與修復方式，不宣稱已查詢成功或沒有檔案。',
            )
        step = await asyncio.to_thread(
            ctx.store.reserve_model_attempt, ctx.owner, ctx.job, ctx.settings.max_steps,
        )
        remaining = ctx.settings.max_steps - step + 1
        messages[0] = SystemMessage(
            content=messages[0].content
            + f"\n本工作剩餘模型呼叫 {remaining} 次（包含子分析）。"
            + '請依剩餘次數安排操作與審核；不足以完成時如實交回已有成果及待辦。',
        )
        request.runtime.stream_writer({'stage': 'model', 'agent': role, 'step': step})
        response = await handler(
            request.override(
                messages=messages[1:],
                system_message=messages[0],
                tool_choice='required' if self.structured else None,
            ),
        )
        message = response.result[0]
        if self.structured and not message.tool_calls:
            raise ValueError('模型未提交結構化操作或結果。')
        # Stable receipt IDs are independent of provider IDs (which may repeat or
        # contain Gemini thought signatures). Keep originals for outbound history.
        calls = [
            {**call, 'id': f"call_{ctx.job.replace('-', '')}_{step}_{index}"}
            for index, call in enumerate(message.tool_calls)
        ]
        response.result[0] = message.model_copy(
            update={
                'name': role,
                'id': f"model-{ctx.job}-{step}",
                'tool_calls': calls,
                'additional_kwargs': {
                    k: v for k, v in message.additional_kwargs.items() if k != 'tool_calls'
                },
                'response_metadata': {
                    **message.response_metadata,
                    'model_step': step,
                    'provider_tool_ids_by_call': {
                        c['id']: original['id'] for c, original in zip(calls, message.tool_calls)
                    },
                },
            },
        )
        return response
