"""Public job lifecycle, ownership, budget and checkpoint recovery."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.types import Command
from langsmith import tracing_context

from company_agent.config import Settings
from company_agent.graph import build_graph
from company_agent.runtime.context import Context
from company_agent.runtime.models import model_failure_message
from company_agent.runtime.models import model_policy
from company_agent.runtime.models import routed_model
from company_agent.runtime.models import UserBilledModel
from company_agent.runtime.results import finish_run
from company_agent.runtime.storage import Store
from company_agent.state import GRAPH_VERSION


class DocumentAgent:
    def __init__(self, settings: Settings, model, *, store=None):
        self.settings, self.model = settings, routed_model(model, settings)
        state = Path(settings.state_dir).resolve()
        for root in settings.roots:
            if root.name.startswith('nas_'):
                raise ValueError('nas_ 為遠端私人資料根目錄的保留前綴。')
            location = Path(root.path).resolve()
            if state == location or location in state.parents or state in location.parents:
                raise ValueError('私人狀態目錄不可與可搜尋的檔案根目錄重疊。')
        if store is not None and store.directory.resolve() != state:
            raise ValueError('工作設定與儲存目錄不符。')
        self.store = store if store is not None else Store(settings.state_dir)

    def billing_identity(self, owner):
        if isinstance(self.model, UserBilledModel):
            if self.model.owner != owner:
                raise ValueError('工作擁有人與計費使用者不符，未呼叫模型或執行工作。')
            return {'owner': owner, 'subject': self.model.subject}
        if self.settings.require_user_billing:
            raise ValueError('此部署必須使用已驗證的個人計費身分，禁止匿名或服務帳號代付。')
        return None

    async def run(self, owner, instruction, progress=None, conversation=()):
        if (
            not owner
            or not isinstance(instruction, str)
            or not instruction.strip()
            or len(instruction) > 10_000
        ):
            raise ValueError('請提供有效身分及 10000 字以內的工作指令。')
        initial = {
            'messages': [
                HumanMessage(content=m['content'], id=m.get('id')) if m['role'] == 'user'
                else AIMessage(content=m['content'], id=m.get('id'), name='coordinator')
                for m in conversation
            ] + [HumanMessage(content=instruction)],
        }
        return await self._drive(owner, None, initial, progress)

    async def resume(self, owner, job, decision=None, progress=None):
        # A thread ID is not permission; verify ownership before loading it.
        self.store.result(owner, job)
        return await self._drive(owner, job, None, progress, decision=decision)

    async def _drive(self, owner, job, initial, progress, *, decision=None):
        billing = self.billing_identity(owner)
        if job is None:
            job = self.store.job(owner)
        policy = {
            'graph_version': (GRAPH_VERSION, '此為舊版或不相容的 Agent checkpoint，請另開工作；既有成果仍可匯出。'),
            'nas_binding_revision': (self.settings.nas_binding_revision, 'NAS 綁定已變更，請另開工作。'),
            'billing_identity': (billing, '工作計費身分與建立時不同，禁止改扣另一使用者。'),
            'model_policy': (model_policy(self.model), '模型分流設定已變更，請以原設定續跑或另開工作。'),
            'completion_protocol': (
                'typed_tool' if self.settings.structured_completion else 'text',
                '工作完成協定已變更，請以原設定恢復或另開新工作。',
            ),
        }
        config = {'configurable': {'thread_id': job}}
        # Do not inherit host-wide tracing that could send company source text
        # to an additional external service. The configured model is explicit.
        with self.store.mutex('job:' + job), tracing_context(enabled=False):
            # Keep the original job limit across approval resumes.
            limit = self.store.step_budget(owner, job, self.settings.max_steps)
            ctx = Context(
                owner,
                job,
                self.settings.model_copy(update={'max_steps': limit}),
                self.store,
                self.model,
            )
            config['recursion_limit'] = 5 * limit + 10
            serde = EncryptedSerializer.from_pycryptodome_aes(
                serde=JsonPlusSerializer(pickle_fallback=False, allowed_json_modules=[]),
                key=(self.store.directory / 'checkpoint.key').read_bytes(),
            )
            async with AsyncPostgresSaver.from_conn_string(self.store.conninfo, serde=serde) as saver:
                with self.store.mutex('checkpoint_setup'):
                    await saver.setup()
                graph = build_graph(self.model, saver, self.settings.structured_completion)
                if initial is None:
                    snapshot = await graph.aget_state(config)
                    for key, (expected, notice) in policy.items():
                        if snapshot.values.get(key) != expected:
                            raise ValueError(notice)
                    if not snapshot.next:
                        raise ValueError('此工作沒有可恢復的 LangGraph 節點，或屬於舊版工作。')
                    interrupts = [i for task in snapshot.tasks if not task.result for i in task.interrupts]
                    if interrupts:
                        if (
                            not isinstance(decision, dict)
                            or set(decision) != {'proposal_id', 'decision'}
                            or decision.get('decision') not in ('approve', 'reject')
                        ):
                            raise ValueError('請明確核准或拒絕本次存檔申請；未消耗等待中的確認。')
                        selected = [i for i in interrupts if i.value['proposal_id'] == decision['proposal_id']]
                        if len(selected) != 1:
                            raise ValueError('請明確核准或拒絕本次存檔申請；未消耗等待中的確認。')
                        initial = Command(resume={selected[0].id: decision})
                    elif decision is not None:
                        raise ValueError('目前不是等待確認的節點。')
                else:
                    initial.update({key: value for key, (value, _) in policy.items()})
                try:
                    async with asyncio.timeout(self.settings.execution_timeout_seconds):
                        async for kind, data in graph.astream(
                            initial, config=config, context=ctx, stream_mode=['updates', 'custom'],
                        ):
                            # Expose progress only, never checkpoint/source payloads.
                            if progress:
                                if kind == 'custom':
                                    event = data
                                elif '__interrupt__' in data:
                                    event = {'stage': 'awaiting_approval'}
                                else:
                                    event = {'stage': 'node_completed', 'nodes': list(data)}
                                await progress({'job_id': job, **event})
                except Exception as failure:
                    notice = model_failure_message(failure)
                    if notice:
                        try:
                            snapshot = await graph.aget_state(config)
                            finish_run(self.settings, self.store, owner, job, snapshot, limit, failure=notice)
                        except Exception as saving_failure:
                            logging.getLogger(__name__).warning(
                                'Unable to save Company Agent model failure (%s)', type(saving_failure).__name__,
                            )
                    raise  # Caller keeps the native error; no retry or replay.
                snapshot = await graph.aget_state(config)
                return finish_run(
                    self.settings, self.store, owner, job, snapshot, limit,
                )
