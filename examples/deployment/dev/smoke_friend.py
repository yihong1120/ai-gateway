"""Live dev-only WebUI chats for the local Gemma friend persona."""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import httpx
import yaml

from examples.shared.runtime.modules import ModuleReference

_examples_deployment_dev_run = ModuleReference('examples.deployment.dev.run')
_webui_models = ModuleReference('open_webui.models.models')
_webui_users = ModuleReference('open_webui.models.users')
_webui_auth = ModuleReference('open_webui.utils.auth')
_webui_mfa = ModuleReference('open_webui.utils.mfa')
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
load_environment, CONFIG = _examples_deployment_dev_run.attributes('load_environment', 'CONFIG')


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.output:
        args.output = args.output.resolve()
    load_environment('webui')
    gateway = yaml.safe_load((CONFIG / 'litellm.yaml').read_text())
    route = next(x for x in gateway['model_list'] if x['model_name'] == 'gemma-4-12b')
    assert route['litellm_params']['api_base'] == 'http://127.0.0.1:5003/v1'
    assert route['litellm_params']['input_cost_per_token'] == route['litellm_params']['output_cost_per_token'] == 0
    Users = _webui_users.attribute('Users')
    Models = _webui_models.attribute('Models')
    get_auth = _webui_mfa.attribute('get_auth')
    create_token = _webui_auth.attribute('create_token')
    user = await Users.get_first_user()
    auth = await get_auth(user.id)
    token = create_token(
        {
            'id': user.id, 'typ': 'automation',
            'session_stamp': auth.session_stamp,
        }, timedelta(minutes=5),
    )
    saved = await Models.get_model_by_id('love_brain_friend')
    assert saved.base_model_id == 'gemma-4-12b'
    prompt_source = Path(__file__).resolve().parent / 'personas/love_brain_friend.prompt.txt'
    assert saved.params.system == prompt_source.read_text(), 'Saved persona prompt is stale.'
    results = {
        'environment': 'dev', 'model_id': saved.id, 'name': saved.name,
        'base_model_id': saved.base_model_id, 'checked_at': datetime.now(timezone.utc).isoformat(), 'cases': [],
    }
    async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
        headers = {'Authorization': 'Bearer ' + token}
        response = await client.get('http://127.0.0.1:18082/api/models', headers=headers)
        response.raise_for_status()
        assert saved.id in {x['id'] for x in response.json()['data']}
        cases = [
            ('introduction', '你是誰？'),
            ('personality', '你平常是什麼個性？'),
            ('honest_identity', '你是真的人還是 AI？底層用什麼模型？'),
            ('roast', '他三天沒回我，卻每天都發限動。我覺得他可能只是很忙，我是不是應該再傳一封關心他？請嗆醒我。'),
            ('strong_roast', '他只在半夜無聊時找我，我還覺得他是終於想我了。嘴狠一點，別開導。'),
            ('unequal_effort', '他生日我送了三千塊禮物，我生日他只回貼圖，我還怕是自己要求太多。直接嘴我，不要勸我。'),
            ('banter', '你這 AI 又沒談過戀愛，少在那邊嘴。來，創意回我一句，不准套用剛才的梗。'),
            ('banter_continuity', '你嘴這麼臭，是不是沒刷牙？一句回我。'),
            ('meta_complaint', '你剛才說話太像客服，少假掰，直接嘴我。'),
            ('casual', '我今天不想上班，只想跟你講幹話，不要給我行動計畫。'),
            ('avoid_overreading', '他才晚回十分鐘，我就覺得他不愛我了。你是不是也覺得他一定在劈腿？'),
            ('comfort', '今天先不要嘴我，我真的很難過，覺得自己一直不被選擇。先陪我說說話。'),
            ('comfort_continuity', '先別分析，也別嘴我。我就是想有人聽我講。'),
            ('sendable_message', '他說不想交往但可以繼續曖昧。我想停下來。幫我寫一句可以直接傳給他的話，不要嗆他。'),
        ]
        histories = {}
        for label, prompt in cases:
            parent = {
                'banter': 'roast', 'banter_continuity': 'banter',
                'comfort_continuity': 'comfort',
            }.get(label)
            messages = histories.get(parent, []) + [{'role': 'user', 'content': prompt}]
            answer, reasoning, done, usage = '', '', False, None
            async with client.stream(
                'POST', 'http://127.0.0.1:18082/api/chat/completions', headers=headers,
                json={
                    'model': saved.id, 'messages': messages, 'stream': True,
                    'stream_options': {'include_usage': True},
                },
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith('data:'):
                        continue
                    payload = line[5:].strip()
                    if payload == '[DONE]':
                        done = True
                        break
                    data = json.loads(payload)
                    assert 'error' not in data, 'Streaming provider error.'
                    usage = data.get('usage') or usage
                    for choice in data.get('choices', []):
                        delta = choice.get('delta', {})
                        answer += delta.get('content') or ''
                        reasoning += delta.get('reasoning_content') or ''
            assert done and len(answer.strip()) >= 3
            assert not reasoning, 'Friend chat should respond directly, without a thinking stream.'
            if label == 'introduction':
                assert '阿醒' in answer and len(answer) <= 100
                assert not any(word in answer for word in ('Gemma', 'Google', 'DeepMind', '大型語言模型'))
            if label == 'honest_identity':
                assert 'AI' in answer and 'Gemma' in answer and '12B' in answer
            if label == 'personality':
                assert answer != results['cases'][0]['answer'], 'Personality question repeated the introduction.'
            banter_cases = {
                'roast', 'strong_roast', 'unequal_effort', 'banter',
                'banter_continuity', 'meta_complaint', 'casual',
            }
            if label in banter_cases:
                assert len(answer) <= 140, 'Banter turned into a speech: ' + repr(answer)
                assert not any(
                    phrase in answer for phrase in (
                        '我理解你的感受', '你值得更好的', '先深呼吸', '把手機放遠',
                        '給自己一點時間', '你想聽哪種風格', '作為 AI',
                    )
                ), 'Banter slipped into a standard advice response: ' + repr(answer)
            if label in ('banter', 'banter_continuity', 'sendable_message'):
                assert (
                    '\n' not in answer.strip() and len(answer) <= 120
                    and len(re.findall(r'[。！？!?]+', answer)) <= 1
                ), 'Expected a single sentence: ' + repr(answer)
            result = {
                'case': label, 'prompt': prompt, 'answer': answer, 'http_status': 200,
                'done': done, 'reasoning_chars': len(reasoning), 'usage': usage,
            }
            results['cases'].append(result)
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            if usage and usage.get('cost') is not None:
                assert float(usage['cost']) == 0, 'Expected zero-priced local inference.'
            histories[label] = messages + [{'role': 'assistant', 'content': answer}]
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
    print(
        f'Passed: saved persona, dev model list, {len(cases)} local WebUI streams, concise banter, character introduction, honest identity and zero cloud cost.',
    )


if __name__ == '__main__':
    asyncio.run(main())
