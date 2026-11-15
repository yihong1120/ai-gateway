"""Publish owned durable job progress/results into the originating WebUI messages."""

from __future__ import annotations

import asyncio
import logging

from company_agent.api.progress import STATUSES, progress_text

logger = logging.getLogger(__name__)


async def publish_job(queue, row, io, result_reader=None):
    from open_webui.models.chats import Chats
    from open_webui.socket.main import get_event_emitter

    from company_agent.api.pipe import markdown

    chat = await Chats.get_chat_by_id_and_user_id(row['chat'], row['owner'])
    if chat is None:
        return  # Deleted chats and jobs created outside chat have no destination.
    result = await io(result_reader or queue.public, row['owner'], row['id'])
    done = result['status'] not in {'queued', 'running'}
    description = (
        STATUSES.get(result['status'], progress_text(result['progress'])) if done else progress_text(result['progress'])
    )
    content = markdown(result) if done else (description + '\n\n' + f'[查看工作或取消](/agents?job={row["id"]})')
    messages = chat.chat.get('history', {}).get('messages', {})
    for message_id in dict.fromkeys([row['request_id'], *row['request_aliases']]):
        message = messages.get(message_id)
        if not message or message.get('role') != 'assistant':
            continue
        marker = message.get('company_agent_job_id')
        existing = message.get('content') or ''
        # Only replace an empty accepted turn, our own updates, or an old receipt
        # for this exact job. Never replace an unrelated answer or recreate a turn.
        if (
            marker != row['id']
            and existing
            and not (
                isinstance(existing, str)
                and existing.startswith(('工作已接收，', '公文工作已接收，'))
                and f'/agents?job={row["id"]}' in existing
            )
        ):
            continue
        output = [
            {
                'id': 'company_agent_' + message_id,
                'type': 'message',
                'role': 'assistant',
                'status': 'completed' if done else 'in_progress',
                'content': [{'type': 'output_text', 'text': content}],
            }
        ]
        saved = await Chats.upsert_message_to_chat_by_id_and_message_id(
            row['chat'],
            message_id,
            {
                'content': content,
                'output': output,
                'done': done,
                'company_agent_job_id': row['id'],
            },
        )
        if saved is None:
            raise RuntimeError('Company Agent chat result could not be saved')
        status = {'description': description, 'done': done}
        await Chats.add_message_status_to_chat_by_id_and_message_id(row['chat'], message_id, status)
        emitter = await get_event_emitter(
            {
                'user_id': row['owner'],
                'chat_id': row['chat'],
                'message_id': message_id,
            },
            update_db=False,
        )
        if emitter is not None:
            await emitter({'type': 'status', 'data': status})
            await emitter(
                {
                    'type': 'chat:completion',
                    'data': {
                        'content': content,
                        'output': output,
                        'done': done,
                    },
                }
            )


async def publish_pending(queue, io, *, result_reader=None):
    slots = asyncio.Semaphore(4)

    async def deliver(row):
        async with slots:
            await attempt(row)

    async def attempt(row):
        try:
            async with asyncio.timeout(30):
                await publish_job(queue, row, io, result_reader)
            await io(queue.chat_delivered, row['id'], row['revision'])
        except Exception as failure:
            # Delivery failure does not replay execution or consume more tokens.
            # Leave the durable revision pending so the next pass retries delivery.
            logger.warning('Company Agent chat delivery failed (%s)', type(failure).__name__)
            await io(queue.chat_failed, row['id'], row['revision'])

    results = await asyncio.gather(*(deliver(row) for row in await io(queue.chat_pending)), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
