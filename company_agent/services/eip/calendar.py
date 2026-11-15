"""Browser-only confirmation bridge. No confirm function is an LLM tool."""
from __future__ import annotations

import secrets
import time

import examples.identity.adapters.webui as _webui_imports
import examples.official_document_workflow.infrastructure.eip.calendar_csv as _calendar_csv_imports
import examples.official_document_workflow.infrastructure.eip.calendar_sync as _calendar_sync_imports
import examples.official_document_workflow.infrastructure.eip.sessions as _sessions_imports
import examples.official_document_workflow.infrastructure.storage.automation_store as _automation_store_imports
import examples.official_document_workflow.runtime as _runtime_imports
from examples.shared.runtime.io import run_sync


def calendar_source(store, owner, artifact):

    with store.db() as db:
        row = db.execute(
            'SELECT job,revision,csv_format FROM eip_calendar_sources WHERE owner=%s AND artifact=%s',
            (owner, artifact),
        ).fetchone()
    if not row:
        raise ValueError('只能預覽本人經公文流程或行程工具驗證的行事曆 CSV。')
    _name, sha, payload = store.read_artifact(owner, artifact, row[0])
    return row[1], sha, _calendar_csv_imports.decode_csv(payload, row[2])


async def current_binding(owner, revision):

    await _webui_imports.verified_user(owner)
    db = await run_sync(_automation_store_imports.store)
    account = await run_sync(db.account, owner)
    if not account or account['generation'] != revision:
        raise ValueError('EIP 綁定已變更；舊帳號的行事曆不能匯入新帳號。')
    return db


async def preview(store, owner, artifact):
    state = _runtime_imports.current_resources().state

    revision, sha, payload = await run_sync(calendar_source, store, owner, artifact)
    db = await current_binding(owner, revision)
    journal = await run_sync(lambda: _runtime_imports.current_resources().journal)
    if len(payload) > 1_048_576:
        raise ValueError('CSV 超過 1 MiB。')
    rows, transport = _calendar_csv_imports.parse_csv(payload)
    await state.sweep()
    if len(state.previews) >= 64:
        raise ValueError('待確認行事曆過多，請稍後再試。')
    async with _sessions_imports.manual_session(owner, state, db) as current:
        staged = await current.read('stage', transport)
        events = await run_sync(_calendar_sync_imports.verified_events, rows, staged, current.link.adapter.user_id, journal)
        token = secrets.token_urlsafe(32)
        state.previews[token] = {
            'owner': owner,
            'expires': time.time() + 600,
            'generation': current.link.generation,
            'events': events,
            'company_artifact': artifact,
            'company_sha256': sha,
            'company_revision': revision,
        }
        return {
            'token': token,
            'account': current.link.account,
            'expires': state.previews[token]['expires'],
            'notice': '僅匯入本人 EIP 私人行事曆、不分享不寄信；工時、回報狀況、完成度不會寫入。預覽 10 分鐘有效。',
            'events': [
                {'row': e['row'], 'status': await run_sync(journal.status, e['key']) or 'new'}
                for e in events
            ],
        }


async def confirm(store, owner, token):
    state = _runtime_imports.current_resources().state

    value = state.owned(state.previews, token, owner)
    if 'company_artifact' not in value:
        raise ValueError('不是公司助理的行事曆預覽。')
    revision, sha, _ = await run_sync(calendar_source, store, owner, value['company_artifact'])
    if revision != value['company_revision'] or sha != value['company_sha256']:
        raise ValueError('CSV 版本已變更，請重新預覽。')
    db = await current_binding(owner, revision)
    journal = await run_sync(lambda: _runtime_imports.current_resources().journal)
    async with _sessions_imports.manual_session(owner, state, db) as current:
        if current.link.generation != value['generation']:
            raise ValueError('登入綁定已變更，請重新預覽。')

        # Commit has no session-expiry replay. Journal reserves each row before
        # network writes, and unknown outcomes always stop instead of resending.

        result = await _calendar_sync_imports.write_events(
            current.link.adapter,
            value['events'],
            journal,
            allowed=BindingGuard(db, owner, revision).allowed,
        )
    await run_sync(store.audit, owner, None, 'eip_calendar_confirm', 'done')
    return result


class BindingGuard:
    def __init__(self, db, owner, revision):
        self.db, self.owner, self.revision = db, owner, revision

    def allowed(self):
        account = self.db.account(self.owner)
        return bool(account and account['generation'] == self.revision)
