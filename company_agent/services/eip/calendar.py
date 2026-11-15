"""Browser-only confirmation bridge. No confirm function is an LLM tool."""
from __future__ import annotations
from examples.shared.runtime.io import run_sync

import secrets
import time


def calendar_source(store, owner, artifact):
    from examples.official_document_workflow.infrastructure.eip.calendar_csv import decode_csv

    with store.db() as db:
        row = db.execute(
            'SELECT job,revision,csv_format FROM eip_calendar_sources WHERE owner=%s AND artifact=%s',
            (owner, artifact),
        ).fetchone()
    if not row:
        raise ValueError('只能預覽本人經公文流程或行程工具驗證的行事曆 CSV。')
    _name, sha, payload = store.read_artifact(owner, artifact, row[0])
    return row[1], sha, decode_csv(payload, row[2])


async def current_binding(owner, revision):
    from examples.identity.adapters.webui import verified_user
    from examples.official_document_workflow.infrastructure.storage.automation_store import store as workflow_store

    await verified_user(owner)
    db = await run_sync(workflow_store,)
    account = await run_sync(db.account,owner)
    if not account or account['generation'] != revision:
        raise ValueError('EIP 綁定已變更；舊帳號的行事曆不能匯入新帳號。')
    return db


async def preview(store, owner, artifact):
    from examples.official_document_workflow.infrastructure.eip.calendar_csv import parse_csv
    from examples.official_document_workflow.infrastructure.eip.sessions import manual_session
    from examples.official_document_workflow.runtime import current_resources
    state = current_resources().state
    from examples.official_document_workflow.infrastructure.eip.calendar_sync import (
        verified_events,
    )

    revision, sha, payload = await run_sync(calendar_source,store, owner, artifact)
    db = await current_binding(owner, revision)
    journal = await run_sync(lambda: current_resources().journal)
    if len(payload) > 1_048_576:
        raise ValueError('CSV 超過 1 MiB。')
    rows, transport = parse_csv(payload)
    await state.sweep()
    if len(state.previews) >= 64:
        raise ValueError('待確認行事曆過多，請稍後再試。')
    async with manual_session(owner, state, db) as current:
        staged = await current.read('stage', transport)
        events = await run_sync(verified_events,rows, staged, current.link.adapter.user_id, journal)
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
                {'row': e['row'], 'status': await run_sync(journal.status,e['key']) or 'new'}
                for e in events
            ],
        }


async def confirm(store, owner, token):
    from examples.official_document_workflow.infrastructure.eip.sessions import manual_session
    from examples.official_document_workflow.runtime import current_resources
    state = current_resources().state
    from examples.official_document_workflow.infrastructure.eip.calendar_sync import write_events

    value = state.owned(state.previews, token, owner)
    if 'company_artifact' not in value:
        raise ValueError('不是公司助理的行事曆預覽。')
    revision, sha, _ = await run_sync(calendar_source,store, owner, value['company_artifact'])
    if revision != value['company_revision'] or sha != value['company_sha256']:
        raise ValueError('CSV 版本已變更，請重新預覽。')
    db = await current_binding(owner, revision)
    journal = await run_sync(lambda: current_resources().journal)
    async with manual_session(owner, state, db) as current:
        if current.link.generation != value['generation']:
            raise ValueError('登入綁定已變更，請重新預覽。')

        # Commit has no session-expiry replay. Journal reserves each row before
        # network writes, and unknown outcomes always stop instead of resending.

        result = await write_events(
            current.link.adapter,
            value['events'],
            journal,
            allowed=BindingGuard(db, owner, revision).allowed,
        )
    await run_sync(store.audit,owner, None, 'eip_calendar_confirm', 'done')
    return result


class BindingGuard:
    def __init__(self, db, owner, revision):
        self.db, self.owner, self.revision = db, owner, revision

    def allowed(self):
        account = self.db.account(self.owner)
        return bool(account and account['generation'] == self.revision)
