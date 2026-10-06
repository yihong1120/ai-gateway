"""Browser-only confirmation bridge. No confirm function is an LLM tool."""
from __future__ import annotations

import secrets
import time


def calendar_source(store, owner, artifact):
    with store.db() as db:
        row = db.execute(
            'SELECT job,revision FROM eip_calendar_sources WHERE owner=%s AND artifact=%s',
            (owner, artifact),
        ).fetchone()
    if not row:
        raise ValueError('只能預覽本人經公文流程或行程工具驗證的行事曆 CSV。')
    _name, sha, payload = store.read_artifact(owner, artifact, row[0])
    return row[1], sha, payload


async def current_binding(owner, revision):
    from company_agent.runtime.context import verified_user
    from official_document_workflow.application import automation

    await verified_user(owner)
    db = automation.store()
    account = db.account(owner)
    if not account or account['generation'] != revision:
        raise ValueError('EIP 綁定已變更；舊帳號的行事曆不能匯入新帳號。')
    return db


async def preview(store, owner, artifact):
    from official_document_workflow.infrastructure.eip.calendar_csv import parse_csv
    from official_document_workflow.infrastructure.eip.sessions import manual_session
    from official_document_workflow.infrastructure.storage.state import STATE
    from official_document_workflow.infrastructure.storage.state import Journal
    from official_document_workflow.infrastructure.eip.calendar_sync import (
        verified_events,
    )

    revision, sha, payload = calendar_source(store, owner, artifact)
    db = await current_binding(owner, revision)
    journal = Journal()
    if len(payload) > 1_048_576:
        raise ValueError('CSV 超過 1 MiB。')
    rows, transport = parse_csv(payload)
    await STATE.sweep()
    if len(STATE.previews) >= 64:
        raise ValueError('待確認行事曆過多，請稍後再試。')
    async with manual_session(owner, STATE, db) as current:
        staged = await current.read('stage', transport)
        events = verified_events(rows, staged, current.link.adapter.user_id, journal)
        token = secrets.token_urlsafe(32)
        STATE.previews[token] = {
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
            'expires': STATE.previews[token]['expires'],
            'notice': '僅匯入本人 EIP 私人行事曆、不分享不寄信；工時、回報狀況、完成度不會寫入。預覽 10 分鐘有效。',
            'events': [
                {'row': e['row'], 'status': journal.status(e['key']) or 'new'}
                for e in events
            ],
        }


async def confirm(store, owner, token):
    from official_document_workflow.infrastructure.eip.sessions import manual_session
    from official_document_workflow.infrastructure.storage.state import STATE
    from official_document_workflow.infrastructure.storage.state import Journal
    from official_document_workflow.infrastructure.eip.calendar_sync import write_events

    value = STATE.owned(STATE.previews, token, owner)
    if 'company_artifact' not in value:
        raise ValueError('不是公司助理的行事曆預覽。')
    revision, sha, _ = calendar_source(store, owner, value['company_artifact'])
    if revision != value['company_revision'] or sha != value['company_sha256']:
        raise ValueError('CSV 版本已變更，請重新預覽。')
    db = await current_binding(owner, revision)
    journal = Journal()
    async with manual_session(owner, STATE, db) as current:
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
    store.audit(owner, None, 'eip_calendar_confirm', 'done')
    return result


class BindingGuard:
    def __init__(self, db, owner, revision):
        self.db, self.owner, self.revision = db, owner, revision

    def allowed(self):
        account = self.db.account(self.owner)
        return bool(account and account['generation'] == self.revision)
