"""Durable upload jobs detached from the originating chat request."""
from __future__ import annotations

import asyncio
import json

from .output_receipts import DELETION_REVIEW_NOTICE
from examples.identity.adapters.webui import verified_user
from examples.official_document_workflow.infrastructure.storage.processing_cache import (
    upload_outcome,
)
from examples.official_document_workflow.runtime import current_resources
from examples.official_document_workflow.services.output_delivery import output_job
from examples.official_document_workflow.services.version import workflow_revision
from examples.shared.runtime.chat import current_turn
from examples.shared.runtime.chat import wait_for_result
from examples.shared.runtime.io import run_sync
from examples.shared.runtime.jobs import service as configured_service
from examples.shared.runtime.module_registry import ModuleRegistry
from examples.shared.runtime.modules import ModuleReference

_webui_chats = ModuleReference('open_webui.models.chats')
_webui_functions = ModuleReference('open_webui.models.functions')


class DocumentInputError(ValueError):
    """A source/configuration change that is safe to show to its owner."""


def source_identity(file):
    return {
        'id': file.id, 'name': file.filename,
        'hash': getattr(file, 'hash', None) or (getattr(file, 'meta', None) or {}).get('file_hash'),
    }


async def submit(pipe, user, metadata, emitter=None):
    Chats = _webui_chats.attribute('Chats')
    Functions = _webui_functions.attribute('Functions')

    owner = (await verified_user((user or {}).get('id')))['id']
    chat_id, message_id = metadata.get('chat_id'), metadata.get('message_id')
    if not chat_id or not message_id:
        raise DocumentInputError('請在已儲存的聊天室提交公文工作。')
    chat = await Chats.get_chat_by_id_and_user_id(chat_id, owner)
    if not chat:
        raise DocumentInputError('聊天室不存在或沒有權限。')
    message, ids, _ = current_turn(
        chat.chat, metadata.get('user_message_id'), message_id,
        require_instruction=False,
    )
    if not ids:
        function = await Functions.get_function_by_id('official_document_processor')
        if not function or not function.is_active:
            raise DocumentInputError('公文處理服務尚未啟用。')
        continuation = await pipe.clarifications.complete(
            body={'messages': [{'role': 'user', 'content': message}]},
            user_id=owner, scope=pipe.clarifications.scope(owner, chat_id), request=None,
        )
        if continuation is None:
            raise DocumentInputError('目前沒有可續接的期限選擇，請重新附加公文。')
        return continuation
    if any(not isinstance(key, str) for key in ids):
        raise DocumentInputError('請先上傳圖片檔，再提交公文工作。')
    _, files = await pipe.owned_sources([{'id': key} for key in ids], owner)
    if {file.id for file in files} != set(ids):
        raise DocumentInputError('本次附件不存在、非本人所有或格式不支援，未開始處理。')
    function = await Functions.get_function_by_id('official_document_processor')
    if not function or not function.is_active:
        raise DocumentInputError('公文處理服務尚未啟用。')
    svc = configured_service()
    key = await svc._io(
        svc.queue.submit_document, owner, chat_id, message_id,
        [source_identity(file) for file in files], pipe.valves.model_dump(mode='json'),
        workflow_revision(function.content),
    )
    return await wait_for_result(svc, owner, key, emitter)


_modules = ModuleRegistry('official_document_background_', __file__)
pipe_module = _modules.acquire


async def execute(svc, row):
    Functions = _webui_functions.attribute('Functions')

    owner, key, payload = row['owner'], row['id'], row['payload']
    user = await svc.identity(owner)
    if user['id'] != owner:
        raise DocumentInputError('登入身分已變更，未處理公文。')
    function = await Functions.get_function_by_id('official_document_processor')
    if not function or not function.is_active:
        raise DocumentInputError('公文處理服務已停用。')
    if workflow_revision(function.content) != payload['content_hash']:
        raise DocumentInputError('公文處理程式已更新；未重播舊工作，請另送新訊息。')
    with pipe_module(function.content) as module, output_job(owner, key):
        pipe = module.Pipe()
        pipe.valves = pipe.Valves(**payload['valves'])
        ids = [source['id'] for source in payload['sources']]
        _, files = await pipe.owned_sources([{'id': source} for source in ids], owner)
        if [source_identity(file) for file in files] != payload['sources']:
            raise DocumentInputError('本次附件已變更、刪除或不再屬於本人，未開始處理。')
        await run_sync(current_resources().configure_document_cache, pipe)
        result = {'files': [], 'answer': ''}
        original_store, original_delete = pipe.store_output, pipe.delete_output

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
            deletion = await original_delete(item)
            if getattr(deletion, 'state', None) == 'deleted':
                result['files'] = [file for file in result['files'] if file['id'] != item.id]
            else:
                result.setdefault('notices', []).append(DELETION_REVIEW_NOTICE)
            await persist()
            return deletion

        async def emit(event):
            if event.get('type') == 'status':
                await svc._io(
                    svc.queue.update, key,
                    progress=event.get('data', {}).get('description', '公文處理中'),
                )

        pipe.store_output, pipe.delete_output = store, delete
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
