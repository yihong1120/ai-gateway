"""One real dev company-agent chat, including durable progress and saved result."""
import asyncio
import json
import time
import uuid
from datetime import timedelta

import httpx
import sys
from pathlib import Path
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from examples.deployment.dev.run import load_environment


async def main():
    env = load_environment('webui')
    from open_webui.models.users import Users
    from open_webui.utils.auth import create_token
    from company_agent.api.service import ServiceConfiguration
    from company_agent.runtime.models import close_model
    user = await Users.get_first_user()
    model = ServiceConfiguration.model(user.model_dump())
    try:
        assert str(model.client.openai_api_base) == env['OPENAI_API_BASE_URLS']
        assert model.client.openai_api_key.get_secret_value() == env['OPENAI_API_KEYS']
    finally:
        await close_model(model)
    headers = {'Authorization': 'Bearer ' + create_token({'id': user.id}, timedelta(minutes=5))}
    user_id, assistant_id = str(uuid.uuid4()), str(uuid.uuid4())
    prompt = '用繁體中文簡短介紹你的用途。這是功能驗證，請勿存取 EIP、NAS 或網路，不要建立檔案。'
    user_message = {'id': user_id, 'role': 'user', 'content': prompt, 'parentId': None,
                    'childrenIds': [assistant_id], 'timestamp': int(time.time())}
    assistant = {'id': assistant_id, 'role': 'assistant', 'content': '', 'parentId': user_id,
                 'childrenIds': [], 'model': 'company_work_assistant', 'done': False}
    async with httpx.AsyncClient(base_url=env['WEBUI_URL'], headers=headers, timeout=120, trust_env=False) as client:
        r = await client.post('/api/v1/chats/new', json={'chat': {'title': 'DEV company_agent 功能驗證',
            'models': ['company_work_assistant'], 'history': {'messages': {user_id: user_message,
                assistant_id: assistant}, 'currentId': assistant_id}, 'messages': [user_message, assistant]}})
        r.raise_for_status()
        chat_id = r.json()['id']
        print('Created isolated dev test chat:', chat_id, flush=True)
        r = await client.post('/api/chat/completions', json={'model': 'company_work_assistant',
            'chat_id': chat_id, 'id': assistant_id, 'user_message': user_message,
            'messages': [{'role': 'user', 'content': prompt}], 'stream': True})
        r.raise_for_status()
        deadline = time.monotonic() + 120
        saw_progress = False
        while time.monotonic() < deadline:
            saved = await client.get('/api/v1/chats/' + chat_id)
            saved.raise_for_status()
            message = saved.json()['chat']['history']['messages'][assistant_id]
            saw_progress |= bool(message.get('statusHistory')) or bool(message.get('company_agent_job_id'))
            if message.get('done') and message.get('company_agent_job_id'):
                result = await client.get('/agents/jobs/' + message['company_agent_job_id'])
                result.raise_for_status()
                data = result.json()
                assert data['status'] == 'done', 'Company dev job status: ' + data['status']
                assert data.get('answer') and data['answer'] in message['content']
                assert saw_progress
                print('Passed: dev model/key, company job completed, progress and answer saved in chat.', flush=True)
                print(json.dumps({'job': data['id'], 'status': data['status'], 'answer': data['answer']}, ensure_ascii=False))
                return
            await asyncio.sleep(1)
        raise RuntimeError('Dev company result was not delivered to its chat within 120 seconds.')


if __name__ == '__main__':
    asyncio.run(main())
