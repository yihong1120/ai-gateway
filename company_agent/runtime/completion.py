"""Typed task completion using official tools, never free-form success claims."""
from __future__ import annotations

import asyncio
import json
from typing import Annotated
from typing import Literal

from langchain_core.messages import HumanMessage
from langchain_core.messages import ToolMessage
from langchain_core.tools import ToolException
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator
from pydantic import StringConstraints

import company_agent.services.delivery.explicit as _explicit_imports
import company_agent.services.delivery.preservation as _preservation_imports
from company_agent.runtime.tools import injected_tool
from company_agent.runtime.tools import tool_receipt
from company_agent.schemas import Strict
from company_agent.services.delivery.schemas import DeliveryPlan
from company_agent.services.delivery.validation import statistics_for_plan
from company_agent.services.delivery.validation import validate_delivery
from company_agent.services.delivery.validation import verify_evidence_sources


def review_evidence(state):
    return [
        item for report in state.get('reports', {}).values()
        if report['agent'] == 'reviewer' and report['status'] == 'completed'
        for item in report['reviewed_artifacts']
    ]


def unresolved_assignments(state):
    reports = state.get('reports', {})
    pending = [
        key for report in reports.values() if report['status'] == 'completed'
        for key in report.get('resolves_assignments', [])
    ]
    resolved = set()
    while pending:
        key = pending.pop()
        if key in resolved or key not in reports:
            continue
        resolved.add(key)
        pending.extend(reports[key].get('resolves_assignments', []))
    return [key for key, report in reports.items() if report['status'] != 'completed' and key not in resolved]


class EvidenceRequest(Strict):
    role: Literal['knowledge', 'research', 'eip', 'engineering', 'data', 'document', 'reviewer']
    task: str = Field(min_length=1, max_length=4000, description='為回答原問題所需的唯讀查證，不是額外操作授權。')
    context: str = Field(default='', max_length=12000, description='真實來源位置、已知版本及待核對問題；不可包含憑證。')


class Completion(Strict):
    outcome: Literal['completed', 'needs_input', 'blocked'] = Field(
        description='completed 僅限本次必要要求全部完成；仍待使用者補充必填資訊用 needs_input，必要來源／權限不可用用 blocked。僅說明缺漏不代表原操作已完成。',
    )
    missing_inputs: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]] = Field(
        default_factory=list, max_length=30, description='本次要求仍需使用者提供的必填資訊，例如會議結束時間；沒有缺漏填空陣列。',
    )
    blockers: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]] = Field(
        default_factory=list, max_length=30, description='必要來源無權限、尚未連接或不可用等阻礙；無阻礙填空陣列。不含帳密。',
    )
    summary: Annotated[str, StringConstraints(strip_whitespace=True)] = Field(
        min_length=1,
        max_length=2000,
        description='繁體中文的實際回答正文。主協調的此欄會直接顯示給使用者；子任務的此欄是交回主協調的完整發現。直接寫出具體結論、數值、日期、來源網址與限制，不得只說已查詢、已整理或已完成，也沒有後續步驟補寫正文。需要資料時明列缺少什麼。',
    )
    evidence_requests: list[EvidenceRequest] = Field(
        default_factory=list,
        description='需要其他來源補證時，交回建議角色、限定的唯讀任務與來源位置；由主協調按原需求決定是否委派，不自動執行。無需補證填空陣列。',
    )
    artifact_ids: list[str] = Field(
        max_length=100,
        description='工具實際產出的 ID；沒有產出填空陣列，不得猜測路徑或 ID。',
    )

    @field_validator('artifact_ids')
    @classmethod
    def distinct(cls, value):
        if len(value) != len(set(value)):
            raise ValueError('Artifact IDs must be distinct')
        return value

    @model_validator(mode='after')
    def consistent_outcome(self):
        if self.outcome == 'completed' and (self.missing_inputs or self.blockers):
            raise ValueError('仍有必填資料或必要阻礙，outcome 不得 completed；請用 needs_input 或 blocked，保留已有結論。')
        return self


async def complete(runtime: ToolRuntime, *, role, **kwargs):
    name = 'complete_job' if role == 'coordinator' else 'complete_assignment'
    args = Completion.model_validate(kwargs)
    if len(runtime.state['messages'][-1].tool_calls) != 1:
        raise ToolException('完成工具須單獨呼叫，先等待其他操作與子任務結果。')
    ctx = runtime.context
    history = await asyncio.to_thread(ctx.store.files, ctx.owner, ctx.job)
    files = [f for f in history if f.get('lifecycle', 'draft') != 'superseded']
    actual = {f['id'] for f in files}
    declared = set(args.artifact_ids)
    if not declared <= {f['id'] for f in history}:
        raise ToolException('完成清單含非本工作產出，未接受完成宣告。')
    # Reopen/hash verification; completion cannot bypass artifact ownership.
    await asyncio.to_thread(ctx.store.verify_artifacts, ctx.owner, ctx.job, args.artifact_ids)
    if ctx.settings.office_execution_mode == 'specialist' and role in {'data', 'document'}:
        produced = {
            json.loads(m.content).get('id') for m in runtime.state['messages']
            if isinstance(m, ToolMessage) and m.status == 'success'
            and m.name in {
                'create_spreadsheet', 'create_spreadsheet_report', 'edit_spreadsheet',
                'create_word', 'create_slides', 'create_text', 'edit_text_run', 'edit_artifact_text_run',
            }
        }
        names = {f['name'] for f in history if f['id'] in declared}
        if not declared <= produced or not names <= set(ctx.assignment_outputs):
            raise ToolException('專責子任務只能交回本次實際產製且在 outputs 內的檔案；唯讀／統計任務填 artifact_ids=[]。')
        if args.outcome == 'completed' and names != set(ctx.assignment_outputs):
            raise ToolException('本次 outputs 尚未全部產製，不得宣告 completed；補齊或明列 needs_input／blocked。')
    if role == 'coordinator' and args.outcome == 'completed':

        plan = await asyncio.to_thread(ctx.store.delivery_plan, ctx.owner, ctx.job)
        if plan is not None or ctx.settings.require_delivery_plan:
            instruction = next(m.text for m in reversed(runtime.state['messages']) if isinstance(m, HumanMessage))
            _explicit_imports.check_explicit_cells(instruction, DeliveryPlan.model_validate(plan or {}))
        await asyncio.to_thread(_preservation_imports.verify_preservation_sources, ctx, DeliveryPlan.model_validate(plan or {}))
        unresolved = unresolved_assignments(runtime.state)
        if unresolved:
            raise ToolException(
                '尚有子任務未完成：' + ', '.join(unresolved)
                + '。若已補足條件，重新委派同角色並以 resolves_assignments 明確補做；否則明列待補資料或阻礙，不得宣告全部完成。',
            )
        if declared != actual:
            raise ToolException('完成清單未列出所有本工作產出或混入已取代舊稿；須恰為全部有效成果，舊稿須明確 supersede_artifact 後才可省略。')
        _, evidence = await asyncio.to_thread(statistics_for_plan, ctx.store, ctx.owner, ctx.job)
        await asyncio.to_thread(verify_evidence_sources, ctx, evidence.values())
        await asyncio.to_thread(validate_delivery, ctx.store, ctx.owner, ctx.job)
        if ctx.settings.require_artifact_review:
            reviewed = {
                (r['id'], r['sha256'])
                for r in review_evidence(runtime.state)
            }
            if any((f['id'], f['sha256']) not in reviewed for f in files):
                raise ToolException('尚有產出未經獨立審核實際讀取，未接受完成宣告。')
        await asyncio.to_thread(ctx.store.finalize_artifacts, ctx.owner, ctx.job, args.artifact_ids)
    value = args.model_dump()
    ctx.store.audit(ctx.owner, ctx.job, name, args.outcome)
    return Command(
        update={
            'completion': value,
            'messages': [tool_receipt(runtime.tool_call_id, name, value)],
        },
    )


def completion_tool(role):
    return injected_tool(
        'complete_job' if role == 'coordinator' else 'complete_assignment', Completion,
        '提交本工作結果，必須列出所有實際產出並完成要求的審核；缺資料或無法完成須明列 outcome。'
        if role == 'coordinator' else '提交本次子任務結果並交回主協調；只列實際產出，不代替審核或批准存檔。',
        complete, role=role, handle_tool_error=True,
    )
