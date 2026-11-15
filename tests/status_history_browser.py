"""Live native WebUI regression checks on A dev; creates and removes one synthetic chat."""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
from playwright.sync_api import expect
from playwright.sync_api import sync_playwright

from examples.shared.runtime.modules import ModuleReference

_webui_users = ModuleReference('open_webui.models.users')
_webui_auth = ModuleReference('open_webui.utils.auth')
_webui_mfa = ModuleReference('open_webui.utils.mfa')

ROOT = Path(__file__).resolve().parents[1]
config = Path.home()/'.config/ai-gateway-dev/webui.json'
env = json.loads(config.read_text())
assert ':15432/' in env['DATABASE_URL'] and env['DATABASE_URL'].endswith(
    '/openwebui_dev',
), 'Only the isolated dev database is allowed'
assert env['OPENAI_API_BASE_URLS'] == 'http://127.0.0.1:14002/v1', 'Refusing a production gateway'
os.environ.update(env)
os.environ.update(FROM_INIT_PY='true', PYTHON_DOTENV_DISABLED='1', ENABLE_DB_MIGRATIONS='false', DEBUG='false')


async def identity():
    Users = _webui_users.attribute('Users')
    get_auth, get_mfa_config, is_mfa_required = _webui_mfa.attributes('get_auth', 'get_mfa_config', 'is_mfa_required')
    create_token = _webui_auth.attribute('create_token')
    user = await Users.get_first_user()
    assert not is_mfa_required('oauth', await get_mfa_config()), 'Use a real signed-in browser when MFA is required'
    auth = await get_auth(user.id)
    return create_token(
        {
            'id': user.id, 'typ': 'session', 'auth_method': 'oauth', 'auth_time': int(time.time()),
            'session_stamp': auth.session_stamp,
        }, timedelta(minutes=5),
    )

token = asyncio.run(identity())
base = 'http://127.0.0.1:18082'
browser_base = 'https://ai-dev.changdar-server.mooo.com'
headers = {'Authorization': 'Bearer '+token}
uid, aid = str(uuid.uuid4()), str(uuid.uuid4())
phases = [
    '已接收，正在處理；進度與結果會顯示在此聊天。', '等待執行（多人並行排程，同一帳號依序處理）',
    '正在執行', '等待公文處理名額；不同帳號可並行處理。',
    '正在一次完成公文分組、附件關聯與欄位判讀。', '正在一次完成公文分組、附件關聯與欄位判讀。',
    '行事曆核對 Agent：確認事件、日期角色與附件關聯', '行事曆核對 Agent：確認事件、日期角色與附件關聯',
]
link = '[查看工作或取消](/agents?job=status-ui-preview)'
user = {
    'id': uid, 'parentId': None, 'childrenIds': [aid], 'role': 'user',
    'content': '狀態呈現驗證（不執行文件或模型工作）', 'timestamp': int(time.time()),
}
answer = {
    'id': aid, 'parentId': uid, 'childrenIds': [], 'role': 'assistant', 'model': 'official_document_processor', 'modelIdx': 0,
    'content': phases[-1]+'\n\n'+link, 'timestamp': int(time.time()), 'done': False,
    'statusHistory': [{'description': phase, 'done': False} for phase in phases],
}
chat = {
    'title': '介面驗證：最新狀態與收合歷程', 'models': ['official_document_processor'],
    'history': {'currentId': aid, 'messages': {uid: user, aid: answer}}, 'messages': [user, answer],
    'timestamp': int(time.time()*1000),
}
checks = []


def passed(name):
    checks.append(name)
    print('Passed:', name, flush=True)


with httpx.Client(base_url=base, headers=headers, timeout=30, trust_env=False) as client:
    response = client.post('/api/v1/chats/new', json={'chat': chat})
    response.raise_for_status()
    chat_id = response.json()['id']
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path='/home/changdar/.cache/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell', headless=True, args=['--no-sandbox'],
            )
            context = browser.new_context(viewport={'width': 1440, 'height': 1050}, locale='zh-TW', color_scheme='dark')
            context.add_init_script(
                'if(location.origin === '+json.dumps(browser_base)+'){localStorage.setItem("token",'+json.dumps(
                    token,
                )+');localStorage.setItem("theme","dark");localStorage.setItem("locale","zh-TW");}',
            )
            page = context.new_page()

            def current_release_notice(route):
                response = route.fetch()
                data = response.json()
                data.setdefault('ui', {})['version'] = '0.12.4'
                route.fulfill(response=response, json=data)
            page.route('**/api/v1/users/user/settings', current_release_notice)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/api/chat/completions', lambda route: route.abort())
            page.goto(browser_base+'/c/'+chat_id, wait_until='domcontentloaded')
            page.keyboard.press('Escape')
            message = page.locator('#message-'+aid)
            toggle = message.locator('.company-status-toggle')
            expect(toggle).to_be_visible(timeout=60000)
            expect(toggle).to_have_attribute('aria-expanded', 'false')
            expect(toggle).to_contain_text(phases[-1])
            expect(message.locator('[data-company-redundant-status]')).to_be_hidden()
            expect(message.get_by_role('link', name='查看工作或取消')).to_be_visible()
            passed('native_chat_starts_collapsed_with_one_latest_status')
            message.screenshot(path='/tmp/status-history-collapsed.png')
            toggle.click()
            expect(toggle).to_have_attribute('aria-expanded', 'true')
            rows = message.locator('[data-company-status-history] .status-description:visible')
            expect(rows).to_have_count(6)
            passed('native_click_expands_five_distinct_past_stages')
            message.screenshot(path='/tmp/status-history-expanded.png')

            page.wait_for_timeout(1500)

            def event(kind, data):
                r = client.post(f'/api/v1/chats/{chat_id}/messages/{aid}/event', json={'type': kind, 'data': data})
                r.raise_for_status()
                assert r.json() is True
            for _ in range(2):
                event('status', {'description': '正在整理輸出與下載檔', 'done': False})
            expect(toggle).to_contain_text('正在整理輸出與下載檔', timeout=20000)
            expect(toggle).to_have_attribute('aria-expanded', 'true')
            expect(rows).to_have_count(7)
            expect(message.locator('[data-company-redundant-status]')).to_be_hidden()
            passed('live_status_updates_preserve_expansion_and_deduplicate_history')
            toggle.press('Space')
            expect(toggle).to_have_attribute('aria-expanded', 'false')
            event('status', {'description': '正在檢查下載連結', 'done': False})
            expect(toggle).to_contain_text('正在檢查下載連結')
            expect(toggle).to_have_attribute('aria-expanded', 'false')
            passed('keyboard_collapse_stays_collapsed_during_live_updates')
            toggle.click()
            expect(toggle).to_have_attribute('aria-expanded', 'true')
            completed_link = '[查看工作、下載與確認操作](/agents?job=status-ui-preview)'
            filenames = ['函文.docx', '附件.docx', '行事曆.csv', '核對結果.docx']
            named_downloads = '\n\n'.join(
                f'[{name}](#status-ui-download-{index})' for index, name in enumerate(filenames, 1)
            )
            footer_downloads = '\n\n'.join(f'[下載檔案 {index}](#status-ui-download-{index})' for index in range(1, 5))
            completed_content = '工作狀態：部分完成\n\n已產生兩份結果；另一份需要補充資料。\n\n'+named_downloads+'\n\n'+completed_link+'\n\n'+footer_downloads

            def expect_completed_links():
                expect(message.get_by_role('link', name='查看工作、下載與確認操作', include_hidden=True)).to_be_hidden()
                for index, name in enumerate(filenames, 1):
                    expect(
                        message.get_by_role(
                            'link', name=f'下載檔案 {index}',
                            exact=True, include_hidden=True,
                        ),
                    ).to_be_hidden()
                    expect(message.get_by_role('link', name=name, exact=True)).to_be_visible()
            event('status', {'description': '部分完成', 'done': True})
            event('chat:completion', {'content': completed_content, 'done': True})
            expect(toggle).to_have_attribute('aria-expanded', 'false')
            expect(toggle).to_have_attribute('aria-label', '展開處理過程')
            expect(toggle.locator('.status-description')).to_be_hidden()
            expect(message.locator('#response-content-container p').first).to_have_text('工作狀態：部分完成')
            expect(message.locator('#response-content-container p').first).to_be_hidden()
            expect(message.get_by_text('已產生兩份結果；另一份需要補充資料。', exact=True)).to_be_visible()
            expect_completed_links()
            passed('completion_auto_collapses_process_and_shows_one_result')
            passed('completed_result_hides_entire_work_status_line')
            passed('completed_footer_hidden_and_named_result_downloads_preserved')
            message.screenshot(path='/tmp/status-history-completed-collapsed.png')
            toggle.click()
            expect(toggle).to_have_attribute('aria-expanded', 'true')
            expect(
                message.locator(
                    '[data-company-status-history] .status-description:visible',
                ).last,
            ).to_have_text('部分完成')
            expect(message.locator('#response-content-container p').first).to_be_hidden()
            expect_completed_links()
            passed('completed_process_expands_with_final_step_and_keeps_downloads')
            message.screenshot(path='/tmp/status-history-completed-expanded.png')
            # Native event delivery updates the live view; persist the synthetic
            # completion explicitly so reload validates a genuinely saved result.
            page.goto('about:blank')
            saved = client.post(f'/api/v1/chats/{chat_id}/messages/{aid}', json={'content': completed_content})
            saved.raise_for_status()
            assert saved.json()['chat']['history']['messages'][aid]['content'] == completed_content
            page.goto(browser_base+'/c/'+chat_id, wait_until='domcontentloaded')
            expect(message.locator('.company-status-toggle')).to_have_attribute('aria-expanded', 'false', timeout=30000)
            expect(message.locator('.company-status-toggle')).to_have_attribute('aria-label', '展開處理過程')
            expect(message.locator('.company-status-toggle .status-description')).to_be_hidden()
            expect(message.locator('#response-content-container p').first).to_be_hidden()
            expect(message.locator('#response-content-container p').first).to_have_text('工作狀態：部分完成')
            expect(message.get_by_text('已產生兩份結果；另一份需要補充資料。', exact=True)).to_be_visible()
            expect_completed_links()
            passed('saved_history_reopens_collapsed_after_reload')
            assert not errors, errors
            passed('no_browser_javascript_errors')
            context.close()
            browser.close()
        result = {
            'environment': 'dev', 'checks': checks, 'native_ui': True, 'native_socket_events': True, 'model_calls': 0, 'temporary_chat_removed': True,
            'completed_result_shown_once': True, 'completed_process_expands_on_click': True,
            'entire_work_status_line_hidden': True,
            'generated_footer_links_hidden': True, 'named_result_downloads_preserved': True,
            'screenshots': [
                '/tmp/status-history-collapsed.png', '/tmp/status-history-expanded.png',
                '/tmp/status-history-completed-collapsed.png', '/tmp/status-history-completed-expanded.png',
            ],
        }
        (ROOT/'docs/status-history-ui-dev-validation-2026-10-27.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    finally:
        response = client.delete('/api/v1/chats/'+chat_id)
        response.raise_for_status()
