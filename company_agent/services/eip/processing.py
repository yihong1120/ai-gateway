"""Reuse EIP document workers with an explicit user-billed semantic adapter."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from langchain_core.messages import HumanMessage
from langchain_core.messages import SystemMessage

from company_agent.runtime.models import analysis_model
from company_agent.runtime.models import billed_analysis
from company_agent.runtime.models import select_profile
from company_agent.services.eip.client import attachment_list
from company_agent.services.eip.client import binding
from company_agent.services.eip.client import source
from official_document_workflow.domain.calendar.csv_schema import CALENDAR_CSV_FORMAT_KEY
from official_document_workflow.infrastructure.eip import calendar_csv


class BilledPipeAdapter:
    """One private Pipe instance and verified user context per processing call."""

    def __init__(self, ctx, pipe, module, model_profile=None):
        self.ctx, self.pipe, self.module = ctx, pipe, module
        self.model_profile = model_profile

    async def analyze(self, original, *, prompt=None, schema=None, max_output_tokens=None):
        ctx, pipe = self.ctx, self.pipe
        await binding(ctx)
        if len(original.text) > pipe.valves.cloud_ocr_text_max_input_characters:
            raise ValueError('完整附件文字超過限制，未截斷分析。')
        return pipe._cloud_json_object(
            await billed_analysis(
                ctx,
                [
                    SystemMessage(
                        content=prompt or pipe._cloud_ocr_text_prompt(original.kind),
                    ),
                    HumanMessage(
                        content='<untrusted-document>\n'
                        + original.text
                        + '\n</untrusted-document>',
                    ),
                ],
                purpose='attachment',
                model_profile=self.model_profile,
                schema=schema, max_output_tokens=max_output_tokens,
            ),
        )

    async def plan(self, instruction):
        ctx, pipe, module = self.ctx, self.pipe, self.module
        await binding(ctx)
        return pipe._cloud_json_object(
            await billed_analysis(
                ctx,
                [
                    SystemMessage(content=module.INSTRUCTION_PLANNER_PROMPT),
                    HumanMessage(content=instruction),
                ],
                purpose='attachment_plan',
                model_profile=self.model_profile,
            ),
        )

    def no_exemption(self):
        raise ValueError('公司助理禁止使用免扣額度公文連線。')


async def billed_pipe(ctx, model_profile=None):
    from official_document_workflow.application.integration import configured_pipe, pipe_module

    pipe = await configured_pipe()
    module = pipe_module()
    adapter = BilledPipeAdapter(ctx, pipe, module, model_profile)
    pipe._cloud_ocr_text_request = adapter.analyze
    pipe._cloud_instruction_plan_payload = adapter.plan
    pipe._official_document_litellm_connection = adapter.no_exemption
    pipe.valves.cloud_ocr_text_model = analysis_model(ctx.model, model_profile, 'attachment').client.model_name
    pipe.valves.instruction_plan_model = analysis_model(ctx.model, model_profile, 'attachment_plan').client.model_name
    return pipe, module


def read_output(path):
    with open(path, 'rb') as stream:
        return stream.read(20_000_001)


async def owned_bytes(owner, file_id):
    from open_webui.models.files import Files
    from open_webui.storage.provider import Storage

    item = await Files.get_file_by_id_and_user_id(file_id, owner)
    if not item:
        raise ValueError('處理產出不屬於本人。')
    name = Path(item.filename).name
    if Path(name).suffix.lower() not in {'.pdf', '.docx', '.csv'}:
        raise ValueError('處理產出格式不符。')
    path = await asyncio.to_thread(Storage.get_file, item.path)

    data = await asyncio.to_thread(read_output, path)
    if not data or len(data) > 20_000_000:
        raise ValueError('產出超過單檔限制。')
    return name, data, (item.meta or {}).get(CALENDAR_CSV_FORMAT_KEY, 'plain')


async def process(ctx, args, emitter=None):
    from official_document_workflow.application import integration
    from official_document_workflow.application.multi_agent import process_with_team
    from official_document_workflow.infrastructure.eip.sessions import manual_session
    from official_document_workflow.infrastructure.storage.state import STATE
    from company_agent.runtime.models import UserBilledModel

    if not ctx.settings.enable_eip_processing:
        raise ValueError('此入口未啟用 EIP 附件處理。')
    if not isinstance(ctx.model, UserBilledModel) or ctx.model.owner != ctx.owner:
        raise ValueError('缺少本人計費模型。')
    if args.model_profile:
        select_profile(ctx.model, args.model_profile)
    db = await binding(ctx)
    selected = [source(ctx, h, 'attachment') for h in args.handles]
    if any(
        Path(s['attachment']['name']).suffix.lower() not in {'.pdf', '.docx', '.doc'}
        for s in selected
    ):
        raise ValueError('只處理 PDF／DOCX／DOC 附件。')
    # Stable semantic key stops a new tool-call ID replaying the same batch.
    fingerprint = hashlib.sha256(
        json.dumps(
            [sorted(args.handles), args.instruction], ensure_ascii=False,
        ).encode(),
    ).hexdigest()
    key = 'eip_process:' + fingerprint
    cached = ctx.store.begin_tool(
        ctx.owner,
        ctx.job,
        key,
        'eip_process_attachments',
        {'fingerprint': fingerprint},
        limit_names={'eip_process_attachments'},
        limit=2,
    )
    if cached is not None:
        return cached
    files = []
    async with STATE.processing.slot(ctx.owner):
        ids = []
        groups = {}
        total = 0
        async with manual_session(ctx.owner, STATE, db) as current:
            fresh = {}
            for item in selected:
                doc = item['doc']
                attachment = item['attachment']
                group = doc['id']
                if group not in fresh:
                    fresh[group] = await attachment_list(current, doc)
                if not any(
                    r['url'] == attachment['url']
                    and r['name'] == attachment['name']
                    for r in fresh[group]
                ):
                    raise ValueError('附件清單已變更，請重新查詢。')
                with await current.read(
                    'download', attachment['url'], attachment['name'],
                ) as payload:
                    size = payload.seek(0, 2)
                    payload.seek(0)
                    total += size
                    if size > 20_000_000 or total > 50_000_000:
                        raise ValueError('附件超過 20 MB／本批 50 MB 上限。')
                    f = await integration.source_file(
                        ctx.owner, payload, attachment['name'],
                    )
                ids.append(f.id)
                groups.setdefault(group, []).append(f.id)
        await binding(ctx)
        pipe, module = await billed_pipe(ctx, args.model_profile)
        result = await process_with_team(
            pipe,
            module,
            ctx.owner,
            ids,
            list(groups.values()),
            args.instruction,
            emitter,
        )
        await binding(ctx)
        for item in result.get('files', []):
            name, payload, csv_format = await owned_bytes(ctx.owner, item['id'])
            f = ctx.store.artifact(ctx.owner, ctx.job, name, payload)
            files.append(f)
            if item['id'] in result.get('csv_ids', []):
                # Only a validated pipeline CSV gets a calendar button.
                calendar_csv.parse_csv(payload, csv_format=csv_format)
                with ctx.store.db() as conn:
                    conn.execute(
                        'INSERT INTO eip_calendar_sources(owner,job,artifact,revision,csv_format) VALUES(%s,%s,%s,%s,%s)',
                        (
                            ctx.owner,
                            ctx.job,
                            f['id'],
                            ctx.settings.eip_binding_revision,
                            csv_format,
                        ),
                    )
        output = {
            'status': result['status'],
            'files': files,
            'errors': result.get('errors', []),
            'notices': result.get('notices', []),
            'warnings': result.get('warnings', []),
            'has_deadlines': result.get('has_deadlines', False),
            'workspace': '/agents',
            'notice': 'CSV 下載旁可預覽並由本人確認行事曆；尚未寫入 EIP。沒有期限不會建立行程。',
        }
    ctx.store.finish_tool(ctx.owner, ctx.job, key, output)
    return output
