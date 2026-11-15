"""Thin official Pipe bridge; source selection comes from the owned saved turn."""
from __future__ import annotations

from examples.shared.runtime.chat import current_turn, wait_for_result  # noqa: F401 - public chat entry contract
from urllib.parse import quote

from company_agent.api.progress import progress_text
from company_agent.api.progress import STATUSES
from company_agent.api.service import service
from company_agent.api.service import upload_bytes
from examples.identity.adapters.webui import verified_user




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
    from examples.official_document_workflow.application.output_receipts import DELETION_REVIEW_NOTICE
    if DELETION_REVIEW_NOTICE in result.get('notices', []):
        lines.append(DELETION_REVIEW_NOTICE)
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
    return await wait_for_result(svc, verified['id'], key, emitter)
