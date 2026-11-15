"""Owned chat-turn selection and durable work observation."""
from __future__ import annotations

import asyncio

from .progress import progress_text
from .progress import STATUSES


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


async def wait_for_result(svc, owner, key, emitter=None):
    """Observe accepted work without owning or cancelling its execution."""
    # The queue owns execution. Waiting here only keeps the ordinary chat response
    # open; disconnecting or stopping this observer must never cancel the job.
    if emitter:
        try:
            await emitter(
                {
                    'type': 'status',
                    'data': {
                        'description': '已接收，正在處理；進度與結果會顯示在此聊天。',
                        'done': False,
                    },
                },
            )
        except Exception:
            pass  # Accepted work belongs to the queue even if delivery fails.
    previous = None
    while True:
        result = await svc._io(svc.queue.public, owner, key)
        done = result['status'] not in {'queued', 'running'}
        description = (
            STATUSES.get(result['status'], progress_text(result['progress']))
            if done
            else progress_text(result['progress'])
        )
        current = (description, done)
        if emitter and current != previous:
            try:
                await emitter(
                    {
                        'type': 'status',
                        'data': {'description': description, 'done': done},
                    },
                )
            except Exception:
                pass
        previous = current
        if done:
            return svc.render(result)
        await asyncio.sleep(1)
