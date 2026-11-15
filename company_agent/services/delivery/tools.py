"""Job-scoped planning, version selection and deterministic acceptance tools."""
from __future__ import annotations

import asyncio

from langchain_core.messages import HumanMessage

from langchain_core.tools import ToolException
from langgraph.types import Command

from company_agent.runtime.tools import injected_tool
from company_agent.runtime.tools import tool_receipt
from company_agent.schemas import Strict
from company_agent.services.delivery.schemas import DeliveryPlan
from company_agent.services.delivery.schemas import SupersedeArtifact
from company_agent.services.delivery.explicit import check_explicit_cells
from company_agent.services.delivery.preservation import bind_preservation, verify_preservation_sources
from company_agent.services.files.errors import RecoverableToolError
from company_agent.services.delivery.validation import validate_delivery
from company_agent.services.delivery.validation import statistics_for_plan
from company_agent.services.delivery.validation import verify_evidence_sources


async def plan(runtime, **kwargs):
    ctx = runtime.context
    if ctx.role != 'coordinator' or ctx.delegation_id is not None:
        raise RecoverableToolError('coordinator_plan_required', '全域交付規格由主協調登記；子角色應回報缺少或不完整的計畫，不自行鎖定或改寫規格。')
    specification = DeliveryPlan.model_validate(kwargs)
    instruction = next(m.text for m in reversed(runtime.state['messages']) if isinstance(m, HumanMessage))
    check_explicit_cells(instruction, specification)
    specification = await asyncio.to_thread(bind_preservation, ctx, instruction, specification, runtime.state.get('sources', []))
    value = specification.model_dump()
    try:
        result = await asyncio.to_thread(ctx.store.delivery_plan, ctx.owner, ctx.job, value)
    except ValueError as error:
        raise ToolException(str(error)) from error
    return Command(update={'messages': [tool_receipt(runtime.tool_call_id, 'plan_delivery', result)]})


async def supersede(runtime, **kwargs):
    ctx = runtime.context
    args = SupersedeArtifact.model_validate(kwargs)
    # Ownership/integrity failures propagate; they are not repairable permission grants.
    try:
        result = await asyncio.to_thread(
            ctx.store.supersede_artifact, ctx.owner, ctx.job,
            args.old_id, args.old_sha256, args.new_id, args.new_sha256,
        )
    except ValueError as error:
        # Store has already checked ownership and both byte hashes. Preserve
        # the topology; only this known lifecycle argument error is repairable.
        if str(error) != '只能取代尚未交付的有效草稿，不可分支取代或形成循環。':
            raise
        files = await asyncio.to_thread(ctx.store.files, ctx.owner, ctx.job)
        raise RecoverableToolError('invalid_version_transition', str(error),
            active_artifacts=[{k: f[k] for k in ('id', 'name', 'sha256', 'lifecycle')}
                              for f in files if f['lifecycle'] != 'superseded'],
            next_action='不要分支、逆向或重複取代歷史鏈。依有效清單審核／交付；只有另一個有效錯稿仍存在時才明確取代它。') from error
    return Command(update={'messages': [tool_receipt(runtime.tool_call_id, 'supersede_artifact', result)]})


async def validate(runtime):
    ctx = runtime.context
    specification, evidence = await asyncio.to_thread(statistics_for_plan, ctx.store, ctx.owner, ctx.job)
    await asyncio.to_thread(verify_preservation_sources, ctx, specification)
    await asyncio.to_thread(verify_evidence_sources, ctx, evidence.values())
    result = await asyncio.to_thread(validate_delivery, ctx.store, ctx.owner, ctx.job)
    return Command(update={'messages': [tool_receipt(runtime.tool_call_id, 'validate_delivery', {'validations': result})]})


def delivery_tools():
    return [
        injected_tool('plan_delivery', DeliveryPlan,
                      '產檔前依使用者明確要求固定檔名、工作表／儲存格、必要文字與統計來源範圍。統計 key 供驗收引用；只登記已知要求，不自訂或猜測需求。已登記後不可改寫。',
                      plan, handle_tool_error=True),
        injected_tool('supersede_artifact', SupersedeArtifact,
                      '明確指定本工作舊草稿與替代新版的 ID／SHA256。舊版保留稽核，但不再交付；新版須重新審核。使用者要求保留兩版時不要取代。', supersede, handle_tool_error=True),
        injected_tool('validate_delivery', Strict,
                      '重新讀取本工作有效成果，以已固定規格及真實統計憑據驗收；失敗時修訂新版，不放寬規格。不代替 reviewer 或本人核准。',
                      validate, handle_tool_error=True),
    ]
