"""One document's durable download, analysis and verified calendar stages."""
from __future__ import annotations

import hashlib
import time
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from . import integration
from official_document_workflow.infrastructure.eip import calendar_csv
from official_document_workflow.infrastructure.eip import calendar_identity
from official_document_workflow.infrastructure.eip import calendar_sync
from official_document_workflow.infrastructure.eip.automatic_session import (
    AutomaticSession,
)
from official_document_workflow.infrastructure.eip.calendar_projects import (
    prepare_project_matches,
)
from official_document_workflow.infrastructure.storage.automation_store import (
    AutomationStore,
)
from official_document_workflow.infrastructure.storage.state import Journal
from official_document_workflow.infrastructure.storage.state import STATE


@dataclass
class AutomaticTask:
    db: AutomationStore
    journal: Journal
    owner: str
    task: dict
    adapter: AutomaticSession
    config: dict
    allowed: Callable[[], bool]
    verify: Callable[[str], Awaitable[bool]]
    stopped: bool = False
    stage: str = 'eligibility'

    @property
    def payload(self):
        return self.task['payload']

    def finished(self, task):
        error = None if task.cancelled() else task.exception()
        if error is not None:
            self.payload['last_error'] = {
                'stage': self.stage,
                'type': type(error).__name__,
                'at': time.time(),
            }
            self.payload['notice'] = '文件處理失敗，本輪已中止，不會自動重試。'
            self.db.save_task(self.task, 'needs_review', self.payload)

    async def run(self):
        async with STATE.processing.slot(self.owner):
            if not self.allowed() or not await self.verify(self.owner):
                self.stopped = True
                return False
            if self.task['phase'] == 'discovered' and not await self.download():
                return False
            if self.task['phase'] == 'downloaded' and not await self.analyze():
                return self.task['phase'] == 'done'
            if self.task['phase'] == 'converted':
                prepared = await self.prepare_calendar()
                if prepared is False:
                    return False
                events, attachment_groups = prepared
                attachments = await self.commit_calendar(events, attachment_groups)
                self.complete(events, attachments)
                return True
        return False

    async def download(self):
        self.db.save_task(self.task, 'downloading', self.payload)
        doc = self.payload['document']
        self.stage = 'attachments'
        attachments = await self.adapter.read('attachments', doc['id'], doc['folder'])
        if not attachments:
            if self.payload.get('no_pdf_recheck_started'):
                self.payload['notice'] = (
                    '已於一小時後再次確認，仍沒有 PDF／DOCX／DOC 附件；'
                    '未將 EIP 表單網頁當成公文處理，已結束自動補件檢查。'
                    '後續若補上附件，請由 EIP 手動處理重新啟動。'
                )
                self.db.save_task(self.task, 'no_pdf_final', self.payload)
            else:
                self.payload['notice'] = (
                    '沒有 PDF／DOCX／DOC 附件；未將 EIP 表單網頁當成公文處理。'
                    '一小時後將再確認一次是否補件；仍無附件即不再自動掃描。'
                )
                self.db.save_task(self.task, 'no_pdf', self.payload)
            return False
        if len(attachments) > 5:
            raise ValueError('單份文件超過 5 個 PDF／Word 附件，請分批手動處理。')
        self.payload['source_ids'] = []
        cache = self.payload.setdefault('source_files', {})
        for attachment in attachments:
            if not self.allowed():
                raise ValueError('已暫停自動處理。')
            key = hashlib.sha256((attachment['url'] + '\0' + attachment['name']).encode()).hexdigest()
            if key not in cache:
                self.stage = 'download'
                pdf = await self.adapter.read('download', attachment['url'], attachment['name'])
                self.stage = 'source_file'
                f = await integration.source_file(self.owner, pdf, attachment['name'])
                cache[key] = f.id
            self.payload['source_ids'].append(cache[key])
            self.db.save_task(self.task, 'downloading', self.payload)
        self.db.save_task(self.task, 'downloaded', self.payload)
        return True

    async def analyze(self):
        if not self.allowed():
            self.stopped = True
            return False
        self.db.save_task(self.task, 'processing', self.payload)
        self.stage = 'processing'
        source_ids = self.payload['source_ids']
        result = await integration.process(self.owner, source_ids, '', None, groups=[source_ids])
        self.payload['output'] = result
        if not result['complete']:
            if result.get('files'):
                self.payload['notice'] = (
                    '文件轉檔已完成，重新命名的 PDF 與可編輯 Word 已保留供下載；'
                    '附件中的行程分析未完整通過，未匯入行事曆。'
                )
                self.db.save_task(self.task, 'needs_review', self.payload)
                return False
            raise ValueError('公文處理未完成，本輪已中止；可用輸出已保留，未匯入行事曆。')
        self.db.save_task(self.task, 'converted', self.payload)
        return True

    async def prepare_calendar(self):
        if not self.payload['output'].get('complete'):
            raise ValueError('既有處理結果不完整，已停止自動匯入；請人工核對。')
        events = []
        event_groups = []
        duration = calendar_csv.automatic_duration_minutes()
        calendars = []
        csv_ids = self.payload['output'].get('csv_ids', [])
        for group_index, file_id in enumerate(
            csv_ids,
            start=1,
        ):
            if not self.allowed():
                raise ValueError('已暫停自動處理。')
            self.stage = 'csv_validation'
            rows, csv_bytes = await integration.read_csv(self.owner, file_id, default_duration_minutes=duration)
            if any(calendar_csv.duration_notice(duration) in r.get('Description', '') for r in rows):
                self.payload['calendar_notice'] = calendar_csv.duration_notice(duration)
            calendars.append((group_index, rows, csv_bytes))
        for group_index, rows, csv_bytes in calendars:
            self.stage = 'calendar_preview'
            staged = await self.adapter.read('stage', csv_bytes)
            group_events = calendar_sync.verified_events(rows, staged, self.adapter.user_id, self.journal)
            self.stage = 'calendar_replay_check'
            await calendar_identity.prepare_source_replay(
                group_events,
                self.adapter.user_id,
                self.journal,
                partial(self.adapter.read, 'stage'),
                integration.read_csv,
            )
            events.extend(group_events)
            event_groups.append((group_index, group_events))
        # Validate local attachment availability before the first
        # external write. Old imports are not attachment backfills.
        attachment_groups = []
        if self.config.get('calendar_attach_renamed_pdf'):
            self.stage = 'calendar_attachment_prepare'
            states = self.journal.statuses(e['key'] for _, group in event_groups for e in group)
            for group_index, group_events in event_groups:
                fresh = [e for e in group_events if e['key'] not in states]
                if not fresh:
                    continue
                pdfs = [
                    item
                    for item in self.payload['output'].get('files', [])
                    if item.get('kind') == 'document_pdf' and str(item.get('group_index', 1)) == str(group_index)
                ]
                if not pdfs:
                    raise ValueError('已啟用行事曆附件，但本組沒有可用的原始 PDF；未新增行程。')
                files = await integration.read_calendar_attachments(self.owner, pdfs)
                attachment_groups.append((fresh, files))
        if not self.allowed() or not await self.verify(self.owner):
            self.stopped = True
            return False
        # Read the participant-project list immediately before
        # calendar writes.  This reflects project additions and
        # renames without trusting a model-provided EIP ID.
        if events:
            self.stage = 'calendar_project_catalog'
            projects = await self.adapter.read('calendar_projects')
            prepare_project_matches(events, projects)
            self.payload['calendar_projects'] = {
                'catalog_count': len(projects),
                'events': [
                    {
                        'status': item['project_match']['status'],
                        'document_project': item['project_match'].get('document_project'),
                        'project': item['project_match'].get('project'),
                    }
                    for item in events
                ],
            }
        return events, attachment_groups

    async def commit_calendar(self, events, attachment_groups):
        # Durable phase before first network write; unknown outcomes
        # remain blocked by the shared journal across restarts.
        self.db.save_task(self.task, 'importing', self.payload)
        self.stage = 'calendar_write'
        self.payload['import'] = await calendar_sync.write_events(self.adapter, events, self.journal, self.allowed)
        # A matched project is a separate, fully verified edit
        # following the private import.  No match remains
        # private; guesses and previously imported events are
        # never changed by the hourly worker.
        self.db.save_task(self.task, 'assigning_projects', self.payload)
        self.stage = 'calendar_project_write'
        self.payload['calendar_project_assignments'] = await calendar_sync.write_project_assignments(
            self.adapter,
            events,
            self.journal,
            self.allowed,
        )
        # The CSV contract cannot include files.  When the
        # owner explicitly opts in, attach only renamed source
        # PDFs from the same analysis group after create/detail
        # verification; never attach DOCX, CSV or OCR copies.
        attachment_result = {'attached_events': 0, 'skipped_events': 0}
        if self.config.get('calendar_attach_renamed_pdf'):
            for group_events, files in attachment_groups:
                self.db.save_task(self.task, 'attaching', self.payload)
                self.stage = 'calendar_attachment_write'
                result = await calendar_sync.write_attachments(
                    self.adapter, group_events, files, self.journal, self.allowed,
                )
                attachment_result['attached_events'] += result['attached_events']
                attachment_result['skipped_events'] += result['skipped_events']
            self.payload['calendar_attachments'] = attachment_result
        return attachment_result

    def complete(self, events, attachment_result):
        assignment = self.payload.get('calendar_project_assignments', {})
        if events:
            self.payload['notice'] = '已完成並匯入行事曆'
            if assignment.get('assigned_events'):
                self.payload['notice'] += f"；已依公文工程名稱歸入 {assignment['assigned_events']} 筆參與專案"
            if assignment.get('private_events'):
                self.payload[
                    'notice'
                ] += f"；{assignment['private_events']} 筆維持私人事項（未啟用專案成員分享或無唯一符合專案）"
        else:
            self.payload['notice'] = '文件已完成；未找到明確期限，沒有新增行程'
        if attachment_result['attached_events']:
            self.payload['notice'] += f"；已夾帶 {attachment_result['attached_events']} 筆行程的改名後原始 PDF"
        if self.payload['output'].get('errors'):
            self.payload['notice'] = '行事曆已獨立驗證並匯入；文件輸出仍有待核對項目，已保留可用檔案。'
        if self.payload.get('calendar_notice'):
            self.payload['notice'] += '。' + self.payload['calendar_notice']
        if self.payload['output'].get('calendar_pending') and self.payload['output'].get('calendar_ready'):
            self.payload['notice'] = '已匯入獨立驗證通過的行程；其餘項目保留待核對，不會自動重送。'
            self.db.save_task(self.task, 'needs_review', self.payload)
        else:
            self.db.save_task(self.task, 'done', self.payload)
