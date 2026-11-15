"""Reuse the deployed Pipe and the current user's Open WebUI file ACLs."""
from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import ExitStack
from pathlib import Path
from typing import BinaryIO

from open_webui.models.files import Files
from open_webui.models.functions import Functions
from open_webui.storage.provider import Storage

from .multi_agent import process_with_team
from examples.official_document_workflow.domain import documents as module
from examples.official_document_workflow.domain.calendar.csv_schema import (
    CALENDAR_CSV_FORMAT_KEY,
)
from examples.official_document_workflow.domain.calendar.match_review import (
    model_prompt as match_review_prompt,
)
from examples.official_document_workflow.domain.calendar.match_review import (
    transport_schema as match_review_schema,
)
from examples.official_document_workflow.domain.calendar.relative_dates import (
    ReceiptContext,
)
from examples.official_document_workflow.domain.limits import MAX_EIP_ATTACHMENTS
from examples.official_document_workflow.domain.validation import ContentValidationError
from examples.official_document_workflow.infrastructure.eip import calendar_csv
from examples.official_document_workflow.infrastructure.eip.calendar_attachments import (
    validate_upload,
)
from examples.official_document_workflow.infrastructure.office import office_input
from examples.official_document_workflow.infrastructure.storage.automation_store import (
    store,
)
from examples.official_document_workflow.infrastructure.storage.processing_cache import (
    ProcessingCache,
)
from examples.official_document_workflow.infrastructure.storage.webui_files import (
    prior_csv_ids,
)
from examples.official_document_workflow.runtime import current_resources
from examples.shared.runtime.io import run_sync
from examples.shared.runtime.modules import ModuleReference

_workflow_processor = ModuleReference('examples.official_document_workflow.services.processor')


class DocumentValidationError(ValueError):
    """A model response did not pass required document-content validation."""

    def __init__(self, message, *, issue=None):
        super().__init__(message)
        self.issue = issue


async def configured_pipe():
    function = await Functions.get_function_by_id('official_document_processor')
    if not function or not function.is_active:
        raise ValueError('公文處理 Function 尚未啟用。')
    DocumentProcessor = _workflow_processor.attribute('DocumentProcessor')

    pipe = DocumentProcessor()
    valves = await Functions.get_function_valves_by_id('official_document_processor')
    pipe.valves = pipe.Valves(**(valves or {}))
    pipe.valves.eip_large_sources = True
    return pipe


async def review_calendar_matches(request, *, owner=None):
    pipe = await configured_pipe()
    pipe._usage_owner = owner
    pipe.valves = pipe.valves.model_copy(update={'cloud_ocr_text_model': pipe.valves.calendar_reconciliation_model})
    return await pipe.analyze_text(
        module.CloudSemanticSource(text=json.dumps(request, ensure_ascii=False), kind='paddle_layout_ocr'),
        prompt=match_review_prompt(), schema=match_review_schema(), max_output_tokens=4_000,
    )


async def source_file(owner: str, payload: BinaryIO, name: str):
    original = payload
    with ExitStack() as cleanup:
        cleanup.callback(original.close)
        if Path(name).suffix.lower() in office_input.OFFICE_SUFFIXES:
            content_cache = await run_sync(lambda: current_resources().shared_content)
            payload, name = await office_input.to_pdf(payload, name, content_cache=content_cache)
        else:
            payload, name = await office_input.to_pdf(payload, name)
        if payload is not original:
            cleanup.callback(payload.close)
        pipe = await configured_pipe()
        return await pipe.store_output(
            user_id=owner,
            payload=payload,
            display_name=Path(name).name,
            content_type='application/pdf',
            source_file_id='',
            kind='eip_source',
        )


async def process(owner: str, ids: list[str], instruction: str, emitter, *, groups: list[list[str]], date_context=None, calendar_only=False, tracking_reminders=False):
    pipe = await configured_pipe()
    pipe._usage_owner = owner
    if date_context is not None and not isinstance(date_context, ReceiptContext):
        raise ValueError('EIP 收文日期來源格式不符。')
    pipe._eip_receipt_context = date_context
    pipe._eip_calendar_only = calendar_only
    pipe._eip_tracking_reminders = tracking_reminders is True
    if pipe.valves.document_analysis_cache_enabled:
        pipe._eip_processing_cache = ProcessingCache(await run_sync(store), owner)
        await run_sync(current_resources().configure_document_cache, pipe)
    try:
        return await process_with_team(pipe,  owner, ids, groups, instruction, emitter)
    except ContentValidationError as exc:
        raise DocumentValidationError(exc.describe(), issue=exc.as_issue()) from exc
    except module.CloudExtractionError as exc:
        # Provider/network/configuration failures are handled by the model
        # transport before this boundary. This exception is the document
        # parser rejecting required fields or their literal source evidence.
        raise DocumentValidationError('公文欄位或原文依據未通過驗證，請人工確認。') from exc


async def retained_document_exports(owner, exports, source_ids, expected_hashes):
    """Reuse only owned, unchanged PDF/Word exports from this exact source set."""
    if not exports or len(exports) > 20 or len({r['id'] for r in exports}) != len(exports):
        raise ValueError('既有公文輸出清單不完整，未進行行事曆補處理。')
    for entry in exports:
        if entry.get('kind') not in {'document_pdf', 'document_docx'}:
            raise ValueError('既有檔案不是公文 PDF／Word，未沿用。')
        item = await Files.get_file_by_id_and_user_id(entry['id'], owner)
        if item is None or set((item.meta or {}).get('eip_group_source_ids') or ()) != set(source_ids):
            raise ValueError('既有公文輸出與本人來源附件不符，未沿用。')
        path = Path(await asyncio.to_thread(Storage.get_file, item.path))

        def digest():
            with path.open('rb') as stream:
                return hashlib.file_digest(stream, 'sha256').hexdigest()

        if await asyncio.to_thread(digest) != expected_hashes.get(entry['id']):
            raise ValueError('既有公文輸出內容已變更，未沿用。')
    return list(exports)


async def calendar_file(owner: str, source_ids: list[str], payload: bytes):
    pipe = await configured_pipe()
    file = await pipe.store_output(
        user_id=owner,
        payload=payload,
        display_name='行事曆.csv',
        content_type='text/csv; charset=utf-8',
        source_file_id=source_ids[0],
        kind='calendar_csv',
        extra_metadata={'eip_group_source_ids': source_ids},
    )
    return {'id': file.id, 'name': file.filename, 'kind': 'calendar_csv'}


async def read_csv(owner: str, file_id: str, *, default_duration_minutes=None, include_history=True):
    item = await Files.get_file_by_id_and_user_id(file_id, owner)
    if not item or not item.filename.lower().endswith('.csv'):
        raise ValueError('請選擇自己擁有的行事曆 CSV。')
    path = await asyncio.to_thread(Storage.get_file, item.path)
    payload = await asyncio.to_thread(Path(path).read_bytes)
    rows, transport = calendar_csv.parse_csv(
        payload, default_duration_minutes=default_duration_minutes,
        csv_format=(item.meta or {}).get(CALENDAR_CSV_FORMAT_KEY, 'plain'),
    )
    if include_history:
        await annotate_csv_sources(owner, item, rows, default_duration_minutes)
    return rows, transport


async def annotate_csv_sources(owner, item, rows, default_duration_minutes):
    """Bind only server-owned source metadata; never parse identity from CSV text."""
    source_ids = (item.meta or {}).get('eip_group_source_ids')
    if source_ids:
        if not all(source_ids):
            raise ValueError('CSV 來源群組資料無效，未匯入。')
        source_ids = sorted(set(source_ids))
        fingerprints = []
        for source_id in source_ids:
            source = await Files.get_file_by_id_and_user_id(source_id, owner)
            if source is None:
                raise ValueError('CSV 原始來源已不存在或未授權，無法安全核對重跑紀錄。')
            fingerprints.append((source.meta or {}).get('file_hash') or 'file:' + source.id)
        scope = hashlib.sha256(json.dumps(sorted(fingerprints)).encode()).hexdigest()
        prior = await prior_csv_ids(owner, item.id, source_ids)
        origin = {
            'scope': scope,
            'owner': owner,
            'prior_csv_ids': prior,
            'duration': default_duration_minutes,
        }
        for row in rows:
            row['_eip_origin'] = origin


async def read_calendar_attachments(owner: str, files: list[dict]):
    """Return owner-authorized renamed original PDFs for EIP calendar attach.

    CSV and searchable-OCR derivatives are intentionally excluded by callers;
    this function additionally revalidates every requested File ACL, path and
    byte count before anything is uploaded to EIP.
    """
    if not 1 <= len(files) <= MAX_EIP_ATTACHMENTS:
        raise ValueError(f'每筆行程可夾帶 1 至 {MAX_EIP_ATTACHMENTS} 份改名後原始 PDF。')
    seen = set()
    result = []
    for entry in files:
        file_id, expected_name = entry.get('id'), entry.get('name')
        if file_id in seen:
            raise ValueError('行事曆附件資料不完整，未匯入行程。')
        seen.add(file_id)
        item = await Files.get_file_by_id_and_user_id(file_id, owner)
        if not item or item.filename != expected_name or not item.filename.lower().endswith('.pdf'):
            raise ValueError('改名後原始 PDF 不存在或不屬於目前使用者，未匯入行程。')
        path = await asyncio.to_thread(Storage.get_file, item.path)
        payload = await asyncio.to_thread(Path(path).read_bytes)
        validate_upload(item.filename, payload)
        result.append((item.filename, payload))
    return result
