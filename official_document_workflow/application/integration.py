"""Reuse the deployed Pipe and the current user's Open WebUI file ACLs."""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import BinaryIO

from open_webui.models.files import Files
from open_webui.models.functions import Functions
from open_webui.storage.provider import Storage

from .multi_agent import process_with_team
from official_document_workflow.infrastructure.eip import calendar_csv
from official_document_workflow.infrastructure.eip.calendar_attachments import (
    validate_upload,
)
from official_document_workflow.infrastructure.office import office_input
from official_document_workflow.infrastructure.storage.automation_store import store
from official_document_workflow.infrastructure.storage.processing_cache import (
    ProcessingCache,
)
from official_document_workflow.infrastructure.storage.webui_files import prior_csv_ids


def pipe_module():
    name = 'official_document_eip_pipe'
    if name not in sys.modules:
        path = Path(__file__).resolve().parents[2] / 'openwebui/functions/official_document_processor_pipe.py'
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


async def configured_pipe():
    function = await Functions.get_function_by_id('official_document_processor')
    if not function or not function.is_active:
        raise ValueError('公文處理 Function 尚未啟用。')
    pipe = pipe_module().Pipe()
    valves = await Functions.get_function_valves_by_id('official_document_processor')
    pipe.valves = pipe.Valves(**(valves or {}))
    pipe.valves.eip_large_sources = True
    return pipe


async def source_file(owner: str, payload: BinaryIO, name: str):
    original = payload
    with ExitStack() as cleanup:
        cleanup.callback(original.close)
        payload, name = await office_input.to_pdf(payload, name)
        if payload is not original:
            cleanup.callback(payload.close)
        pipe = await configured_pipe()
        return await pipe._store_derived_bytes(
            user_id=owner,
            payload=payload,
            display_name=Path(name).name,
            content_type='application/pdf',
            source_file_id='',
            kind='eip_source',
        )


async def process(owner: str, ids: list[str], instruction: str, emitter, *, groups: list[list[str]]):
    pipe = await configured_pipe()
    module = pipe_module()
    pipe._eip_processing_cache = ProcessingCache(store(), owner)
    return await process_with_team(pipe, module, owner, ids, groups, instruction, emitter)


async def calendar_file(owner: str, source_ids: list[str], payload: bytes):
    pipe = await configured_pipe()
    file = await pipe._store_derived_bytes(
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
    rows, transport = calendar_csv.parse_csv(payload, default_duration_minutes=default_duration_minutes)
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
    if not 1 <= len(files) <= 5:
        raise ValueError('每筆行程可夾帶 1 至 5 份改名後原始 PDF。')
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
