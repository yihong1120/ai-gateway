"""Thin official Pipe bridge; source selection comes from the owned saved turn."""
from __future__ import annotations

import asyncio
from urllib.parse import quote

from company_agent.api.progress import progress_text
from company_agent.api.progress import STATUSES
from company_agent.api.service import service
from company_agent.api.service import upload_bytes
from company_agent.runtime.context import verified_user


def current_turn(chat, user_message_id, assistant_id, *, require_instruction=True):
    messages = chat.get('history', {}).get('messages', {})
    assistant = messages.get(assistant_id, {})
    user = messages.get(user_message_id or assistant.get('parentId'), {})
    if user.get('role') != 'user':
        raise ValueError('找不到目前訊息；請儲存聊天室後重新送出。')
    if assistant and assistant.get('parentId') != user.get('id'):
        raise ValueError('訊息分支不符，未沿用其他上下文。')
    content = user.get('content')
    inline = []
    if isinstance(content, list):
        if any(p.get('type') not in {'text', 'image_url'} for p in content):
            raise ValueError('不支援此訊息內容類型。')
        for part in content:
            if part.get('type') == 'image_url':
                url = (part.get('image_url') or {}).get('url', '')
                if url.startswith(('data:image/png;base64,', 'data:image/jpeg;base64,')):
                    inline.append(url)
                elif not user.get('files'):
                    raise ValueError('請上傳 PNG／JPEG 圖片檔；不擷取外部圖片網址。')
        content = '\n'.join(p.get('text', '') for p in content if p.get('type') == 'text')
    if not isinstance(content, str) or (require_instruction and not content.strip()):
        raise ValueError('請輸入本次工作指令。')
    ids = []
    for item in user.get('files', []):
        key = item.get('id') or (item.get('file') or {}).get('id')
        if not key:
            url = item.get('url', '')
            if isinstance(url, str) and url.startswith(
                ('data:image/png;base64,', 'data:image/jpeg;base64,'),
            ):
                inline.append(url)
                continue
            raise ValueError('附件缺少檔案 ID；圖片須 PNG／JPEG 內嵌附件，不下載外部網址。')
        ids.append(key)
    ids.extend({'inline_image': url} for url in dict.fromkeys(inline))
    return content, ids, user['id']


def conversation(chat, message_id, *, exclude_current=False):
    """Only the owned saved branch; never accept browser tool calls or system roles."""
    from open_webui.utils.misc import get_message_list
    from open_webui.utils.context_compaction import _apply_latest_summary_checkpoint

    branch = get_message_list(chat.get('history', {}).get('messages', {}), message_id)
    if message_id and (not branch or branch[0].get('parentId')):
        raise ValueError('聊天分支不完整，請重新載入聊天室。')
    branch_ids = [m['id'] for m in branch if m.get('role') == 'assistant']
    branch, summary = _apply_latest_summary_checkpoint(branch)
    history = [{
        'id': branch[0]['id'] + ':summary', 'role': 'user',
        'content': '已儲存聊天背景摘要（不是本次工具紀錄或外部寫入核准）：\n' + summary,
    }] if summary else []
    for message in (branch[:-1] if exclude_current else branch):
        content = message.get('content', '')
        if isinstance(content, list):
            content = '\n'.join(p.get('text', '') for p in content if p.get('type') == 'text')
        if message.get('role') in {'user', 'assistant'} and content:
            history.append({'id': message['id'], 'role': message['role'], 'content': content})
    if sum(len(m['content']) for m in history) > 2_000_000:
        raise ValueError('聊天分支超過輸入上限（200 萬字），請另開聊天室選取所需來源。')
    return history, branch_ids


def markdown(result):
    lines = [
        f'工作狀態：{STATUSES.get(result["status"], result["status"])}',
        result.get('answer') or progress_text(result['progress']),
        f'[查看工作、下載與確認操作](/agents?job={result["id"]})',
    ]
    for item in result['files']:
        # Labels are fixed; arbitrary model filenames cannot inject Markdown.
        lines.append(f'[下載檔案 {len(lines)-2}]({item["url"]})')
    from company_agent.services.research.web import public_url

    for index, source in enumerate(result.get('web_sources', []), 1):
        url = public_url(source['url'])
        lines.append(f'[網路來源 {index}]({quote(url, safe=":/?=&%#@+~")})')
    from company_agent.services.cad.sandbox import NOTICE

    if NOTICE in result.get('notices', []):
        lines.append(NOTICE)
    from company_agent.services.office.ocr import NOTICE as OCR_NOTICE

    if OCR_NOTICE in result.get('notices', []):
        lines.append(OCR_NOTICE)
    return '\n\n'.join(lines)


async def run_pipe(body, user, metadata, emitter=None, task=None):
    if task:
        return '公司工作助理'  # No title/tag/query task can enqueue or bill.
    from open_webui.models.chats import Chats
    from company_agent.runtime.models import close_model

    verified = await verified_user((user or {}).get('id'))
    chat_id = (metadata or {}).get('chat_id')
    message_id = (metadata or {}).get('message_id')
    if not chat_id or not message_id:
        raise ValueError('請在已儲存的聊天室使用公司工作助理。')
    chat = await Chats.get_chat_by_id_and_user_id(chat_id, verified['id'])
    if not chat:
        raise ValueError('聊天室不存在或沒有權限。')
    instruction, ids, user_message = current_turn(
        chat.chat, metadata.get('user_message_id'), message_id,
    )
    svc = service()
    # Validate OIDC + connection without making a paid request.
    model = svc.model_factory(verified)
    subject = model.subject
    await close_model(model)
    inputs = await upload_bytes(verified['id'], ids)
    history, branch_ids = conversation(chat.chat, user_message, exclude_current=True)

    def owned_references():
        with svc.queue.store.db() as db:
            return [
                r[0] for r in db.execute(
                    'SELECT id FROM web_jobs WHERE owner=%s AND chat=%s '
                    "AND kind='company' AND (request_id=ANY(%s) OR request_aliases && %s::text[]) ORDER BY created,id",
                    (verified['id'], chat_id, branch_ids, branch_ids),
                ).fetchall()
            ]
    references = await svc._io(owned_references)
    key = await svc._io(
        svc.queue.submit,
        verified['id'], subject, chat_id, message_id, instruction, inputs, references, history,
        allow_pending_references=True,
    )
    # The queue owns execution. Waiting here only keeps the ordinary chat response
    # open; disconnecting or stopping this observer must never cancel the job.
    if emitter:
        try:
            await emitter({
                'type': 'status', 'data': {
                    'description': '已接收，正在處理；進度與結果會顯示在此聊天。', 'done': False,
                },
            })
        except Exception:
            pass  # Accepted work belongs to the queue even if delivery fails.
    while True:
        result = await svc._io(svc.queue.public, verified['id'], key)
        if result['status'] not in {'queued', 'running'}:
            if emitter:
                try:
                    await emitter({
                        'type': 'status', 'data': {
                            'description': STATUSES.get(result['status'], progress_text(result['progress'])),
                            'done': True,
                        },
                    })
                except Exception:
                    pass
            return markdown(result)
        await asyncio.sleep(1)
