"""Durable upload jobs detached from the originating chat request."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types
from functools import lru_cache

from company_agent.api.pipe import current_turn
from company_agent.api.service import service as configured_service
from company_agent.runtime.context import verified_user
from official_document_workflow.infrastructure.storage.processing_cache import upload_outcome


class DocumentInputError(ValueError):
    """A source/configuration change that is safe to show to its owner."""


def source_identity(file):
    return {
        'id': file.id, 'name': file.filename,
        'hash': getattr(file, 'hash', None) or (getattr(file, 'meta', None) or {}).get('file_hash'),
    }


async def submit(pipe, user, metadata):
    from open_webui.models.chats import Chats
    from open_webui.models.functions import Functions

    owner = (await verified_user((user or {}).get('id')))['id']
    chat_id, message_id = metadata.get('chat_id'), metadata.get('message_id')
    if not chat_id or not message_id:
        raise DocumentInputError('請在已儲存的聊天室提交公文工作。')
    chat = await Chats.get_chat_by_id_and_user_id(chat_id, owner)
    if not chat:
        raise DocumentInputError('聊天室不存在或沒有權限。')
    _, ids, _ = current_turn(
        chat.chat, metadata.get('user_message_id'), message_id,
        require_instruction=False,
    )
    if any(not isinstance(key, str) for key in ids):
        raise DocumentInputError('請先上傳圖片檔，再提交公文工作。')
    _, files = await pipe._get_owned_source_files([{'id': key} for key in ids], owner)
    if {file.id for file in files} != set(ids):
        raise DocumentInputError('本次附件不存在、非本人所有或格式不支援，未開始處理。')
    function = await Functions.get_function_by_id('official_document_processor')
    if not function or not function.is_active:
        raise DocumentInputError('公文處理服務尚未啟用。')
    svc = configured_service()
    key = await svc._io(
        svc.queue.submit_document, owner, chat_id, message_id,
        [source_identity(file) for file in files], pipe.valves.model_dump(mode='json'),
        hashlib.sha256(function.content.encode()).hexdigest(),
    )
    return (
        '公文工作已接收，會在背景執行；關閉聊天或頁面後仍會繼續。\n\n'
        f"工作編號：`{key}`\n\n[查看進度、下載成果或取消工作](/agents?job={key})"
    )


@lru_cache(maxsize=8)
def pipe_module(content):
    # A content-specific module preserves native dataclass identities even if
    # another version is loaded while a previous job is still finishing.
    name = 'official_document_background_' + hashlib.sha256(content.encode()).hexdigest()
    module = types.ModuleType(name)
    module.__file__ = __file__
    sys.modules[name] = module
    try:
        exec(compile(content, name, 'exec'), module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


async def execute(svc, row):
    from open_webui.models.functions import Functions

    owner, key, payload = row['owner'], row['id'], row['payload']
    user = await svc.identity(owner)
    if user['id'] != owner:
        raise DocumentInputError('登入身分已變更，未處理公文。')
    function = await Functions.get_function_by_id('official_document_processor')
    if not function or not function.is_active:
        raise DocumentInputError('公文處理服務已停用。')
    if hashlib.sha256(function.content.encode()).hexdigest() != payload['content_hash']:
        raise DocumentInputError('公文處理程式已更新；未重播舊工作，請另送新訊息。')
    pipe = pipe_module(function.content).Pipe()
    pipe.valves = pipe.Valves(**payload['valves'])
    ids = [source['id'] for source in payload['sources']]
    _, files = await pipe._get_owned_source_files([{'id': source} for source in ids], owner)
    if [source_identity(file) for file in files] != payload['sources']:
        raise DocumentInputError('本次附件已變更、刪除或不再屬於本人，未開始處理。')
    result = {'files': [], 'answer': ''}
    original_store, original_delete = pipe._store_derived_bytes, pipe._delete_derived_file

    async def persist():
        await svc._io(svc.queue.update, key, document_result=json.dumps(result, ensure_ascii=False))

    async def store(**kwargs):
        if kwargs['user_id'] != owner:
            raise DocumentInputError('產出檔案身分不符。')

        async def committed():
            item = await original_store(**kwargs)
            result['files'].append({
                'id': item.id, 'name': item.filename,
                'url': f"/api/v1/files/{item.id}/content?attachment=true",
                'kind': kwargs['kind'],
            })
            await persist()
            return item
        pending = asyncio.create_task(committed())
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            # A submitted storage write cannot be abandoned halfway through.
            await asyncio.gather(pending, return_exceptions=True)
            raise

    async def delete(item):
        await original_delete(item)
        result['files'] = [file for file in result['files'] if file['id'] != item.id]
        await persist()

    async def emit(event):
        if event.get('type') == 'status':
            await svc._io(
                svc.queue.update, key,
                progress=event.get('data', {}).get('description', '公文處理中'),
            )

    pipe._store_derived_bytes, pipe._delete_derived_file = store, delete
    report = {'status': 'error'}
    token = upload_outcome.set(report)
    try:
        result['answer'] = await pipe.pipe(
            {'messages': []}, __user__=user,
            __files__=[{'id': source} for source in ids],
            __event_emitter__=emit,
        )
        status = report['status']
        await svc._io(
            svc.queue.update, key, status=status,
            progress={
                'done': '已完成，請查看結果與下載', 'partial': '部分完成，請查看成果與未完成原因',
                'error': '公文處理未完成，請查看原因與已保留成果',
            }[status],
            document_result=json.dumps(result, ensure_ascii=False),
        )
    finally:
        upload_outcome.reset(token)
