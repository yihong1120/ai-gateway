"""One document's durable download, analysis and verified calendar stages."""
from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from . import integration
from examples.official_document_workflow.domain.calendar.relative_dates import (
    ReceiptContext,
)
from examples.official_document_workflow.domain.calendar.scheduling import EIP_REVIEW_NOTICE
from examples.official_document_workflow.domain.calendar.scheduling import TENTATIVE_NOTICE
from examples.official_document_workflow.domain.limits import MAX_EIP_ATTACHMENTS
from examples.official_document_workflow.infrastructure.eip import calendar_csv
from examples.official_document_workflow.infrastructure.eip import calendar_identity
from examples.official_document_workflow.infrastructure.eip import calendar_sync
from examples.official_document_workflow.infrastructure.eip.automatic_session import (
    AutomaticSession,
)
from examples.official_document_workflow.infrastructure.eip.calendar_projects import (
    prepare_project_matches,
)
from examples.official_document_workflow.infrastructure.storage.automation_store import (
    AutomationStore,
)
from examples.official_document_workflow.infrastructure.storage.state import Journal
from examples.official_document_workflow.services.output_delivery import output_job
from examples.official_document_workflow.services.output_delivery import output_part
from examples.shared.runtime.io import run_sync


def partial_calendar_ready(output):
    """Allow only independently approved rows with exclusively pending items."""
    pending = output.get('calendar_pending') or []
    errors = output.get('errors') or []
    return bool(
        not output.get('complete') and output.get('calendar_ready')
        and output.get('csv_ids') and pending and errors
        and not output.get('calendar_errors') and not output.get('warnings')
        and all(
            isinstance(error, str) and re.sub(r'^第 \d+ 組：', '', error) in pending
            for error in errors
        ),
    )


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
    slots: object = None
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
            # Record code locations only: exception text and frame locals may
            # contain credentials, model output or confidential document text.
            trace = error.__traceback__
            while trace is not None:
                module = trace.tb_frame.f_globals.get('__name__', '')
                if module.startswith('examples.official_document_workflow.'):
                    self.payload['last_error']['site'] = {
                        'module': module,
                        'function': trace.tb_frame.f_code.co_name,
                        'line': trace.tb_lineno,
                    }
                trace = trace.tb_next
            self.payload['notice'] = '文件處理失敗，本輪已中止，不會自動重試。'
            self.db.save_task(self.task, 'needs_review', self.payload)

    async def run(self):
        with output_job(self.owner, self.task['id']):
            if self.slots is None:
                raise RuntimeError('Automatic task requires an owned processing slot service.')
            async with self.slots.slot(self.owner):
                if not await run_sync(self.allowed) or not await self.verify(self.owner):
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
                    await run_sync(self.complete, events, attachments)
                    return self.task['phase'] == 'done'
            return False

    async def download(self):
        await run_sync(self.db.save_task, self.task, 'downloading', self.payload)
        doc = self.payload['document']
        self.stage = 'attachments'
        detail = await self.adapter.read('document_detail', doc['id'], doc['folder'])
        self.payload['date_context'] = ReceiptContext.from_detail(doc['id'], detail).record()
        attachments = detail['attachments']
        if not attachments:
            if self.payload.get('no_pdf_recheck_started'):
                self.payload['notice'] = (
                    '已於一小時後再次確認，仍沒有 PDF／DOCX／DOC 附件；'
                    '未將 EIP 表單網頁當成公文處理，已結束自動補件檢查。'
                    '後續若補上附件，請由 EIP 手動處理重新啟動。'
                )
                await run_sync(self.db.save_task, self.task, 'no_pdf_final', self.payload)
            else:
                self.payload['notice'] = (
                    '沒有 PDF／DOCX／DOC 附件；未將 EIP 表單網頁當成公文處理。'
                    '一小時後將再確認一次是否補件；仍無附件即不再自動掃描。'
                )
                await run_sync(self.db.save_task, self.task, 'no_pdf', self.payload)
            return False
        if len(attachments) > MAX_EIP_ATTACHMENTS:
            self.payload['input_issue'] = {
                'code': 'too_many_attachments',
                'attachment_count': len(attachments),
                'max_attachments': MAX_EIP_ATTACHMENTS,
            }
            self.payload['notice'] = (
                f'本件有 {len(attachments)} 個 PDF／Word 附件，超過每件最多 {MAX_EIP_ATTACHMENTS} 個附件的限制；'
                '請至工作台分批選取處理。其他文件繼續自動處理。'
            )
            await run_sync(self.db.save_task, self.task, 'needs_review', self.payload)
            return False
        self.payload['source_ids'] = []
        cache = self.payload.setdefault('source_files', {})
        for attachment in attachments:
            if not await run_sync(self.allowed):
                raise ValueError('已暫停自動處理。')
            key = hashlib.sha256((attachment['url'] + '\0' + attachment['name']).encode()).hexdigest()
            if key not in cache:
                self.stage = 'download'
                pdf = await self.adapter.read('download', attachment['url'], attachment['name'])
                self.stage = 'source_file'
                with output_part(key):
                    f = await integration.source_file(self.owner, pdf, attachment['name'])
                cache[key] = f.id
            self.payload['source_ids'].append(cache[key])
            await run_sync(self.db.save_task, self.task, 'downloading', self.payload)
        await run_sync(self.db.save_task, self.task, 'downloaded', self.payload)
        return True

    async def analyze(self):
        if not await run_sync(self.allowed):
            self.stopped = True
            return False
        await run_sync(self.db.save_task, self.task, 'processing', self.payload)
        self.stage = 'processing'
        source_ids = self.payload['source_ids']
        saved_context = self.payload.get('date_context')
        if not saved_context:
            doc = self.payload['document']
            detail = await self.adapter.read('document_detail', doc['id'], doc['folder'])
            saved_context = ReceiptContext.from_detail(doc['id'], detail).record()
            self.payload['date_context'] = saved_context
        if saved_context['flow_id'] != self.task['document_id']:
            raise ValueError('EIP 收文日期所屬文件不符，未處理。')
        date_context = ReceiptContext(
            saved_context['flow_id'],
            saved_context['reference_number'], saved_context['received_date'],
        )
        recheck = self.payload.get('relative_date_recheck') or {}
        retained = None
        if recheck.get('revision') == 'eip-relative-date-estimate-v1':
            retained = await integration.retained_document_exports(
                self.owner, recheck['retained_exports'], source_ids, recheck['retained_sha256'],
            )
        try:
            result = await integration.process(
                self.owner, source_ids, '', None, groups=[source_ids], date_context=date_context,
                **({'calendar_only': True} if retained is not None else {}),
                **({'tracking_reminders': True} if self.config.get('tracking_reminders_enabled') else {}),
            )
        except integration.DocumentValidationError as error:
            self.payload['input_issue'] = error.issue or {'code': 'document_validation_failed'}
            self.payload['notice'] = (
                (error.issue['reason'] + error.issue['next_action'] + '已保留來源檔案，其他文件繼續處理。')
                if error.issue else
                '本件公文欄位或原文依據未通過驗證，已保留來源檔案，請人工確認；'
                '未產生或匯入未驗證的行程。其他文件繼續自動處理。'
            )
            await run_sync(self.db.save_task, self.task, 'needs_review', self.payload)
            return False
        if retained is not None:
            await integration.retained_document_exports(
                self.owner, retained, source_ids, recheck['retained_sha256'],
            )
            result['files'] = [*retained, *result.get('files', [])]
            if not result.get('date_estimates') and not result.get('tracking_reminders'):
                message = '本次未確認可推估的相對期限；既有 PDF／Word 已保留，未當成無行程完成。'
                result.update(complete=False, calendar_ready=False, status='partial')
                result['errors'] = [*result.get('errors', []), message]
        self.payload['output'] = result
        if not result['complete'] and not partial_calendar_ready(result):
            if result.get('files'):
                self.payload['notice'] = (
                    '文件處理尚有待核對項目，已確認完成的檔案已保留供下載；'
                    '未匯入行事曆，不會自動重送未完成或結果不明的輸出。'
                )
                await run_sync(self.db.save_task, self.task, 'needs_review', self.payload)
                return False
            raise ValueError('公文處理未完成，本輪已中止；可用輸出已保留，未匯入行事曆。')
        await run_sync(self.db.save_task, self.task, 'converted', self.payload)
        return True

    async def prepare_calendar(self):
        if not self.payload['output'].get('complete') and not partial_calendar_ready(self.payload['output']):
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
            if not await run_sync(self.allowed):
                raise ValueError('已暫停自動處理。')
            self.stage = 'csv_validation'
            rows, csv_bytes = await integration.read_csv(self.owner, file_id, default_duration_minutes=duration)
            if any(calendar_csv.duration_notice(duration) in r.get('Description', '') for r in rows):
                self.payload['calendar_notice'] = calendar_csv.duration_notice(duration)
            if any(TENTATIVE_NOTICE in r.get('Description', '') for r in rows):
                self.payload['calendar_notice'] = '部分行程採用 AI 暫排，非公文明訂時間。' + EIP_REVIEW_NOTICE
            calendars.append((group_index, rows, csv_bytes))
        for group_index, rows, csv_bytes in calendars:
            self.stage = 'calendar_preview'
            staged = await self.adapter.read('stage', csv_bytes)
            group_events = await run_sync(calendar_sync.verified_events, rows, staged, self.adapter.user_id, self.journal)
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
        if partial_calendar_ready(self.payload['output']) and not events:
            raise ValueError('部分行程的 CSV 沒有可驗證事項，未匯入。')
        self.stage = 'calendar_existing_events'
        states = await calendar_sync.check_existing_events(
            events, self.journal, self.adapter.calendar_import_matches, block_conflicts=True,
            reviewer=partial(integration.review_calendar_matches, owner=self.owner),
        )
        fresh_events = [
            event for event in events
            if event['key'] not in states and event.get('existing_match', {}).get('status') == 'new'
        ]
        # Validate local attachment availability before the first
        # external write. Old imports are not attachment backfills.
        attachment_groups = []
        if self.config.get('calendar_attach_renamed_pdf'):
            self.stage = 'calendar_attachment_prepare'
            states = await run_sync(self.journal.statuses, [e['key'] for _, group in event_groups for e in group])
            for group_index, group_events in event_groups:
                fresh = [e for e in group_events if e in fresh_events]
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
        if not await run_sync(self.allowed) or not await self.verify(self.owner):
            self.stopped = True
            return False
        # Read the participant-project list immediately before
        # calendar writes.  This reflects project additions and
        # renames without trusting a model-provided EIP ID.
        if fresh_events:
            self.stage = 'calendar_project_catalog'
            projects = await self.adapter.read('calendar_projects')
            prepare_project_matches(fresh_events, projects)
            self.payload['calendar_projects'] = {
                'catalog_count': len(projects),
                'events': [
                    {
                        'status': item['project_match']['status'],
                        'document_project': item['project_match'].get('document_project'),
                        'project': item['project_match'].get('project'),
                    }
                    for item in fresh_events
                ],
            }
        return events, attachment_groups

    async def commit_calendar(self, events, attachment_groups):
        async with self.journal.calendar_lock():
            return await self._commit_calendar_locked(events, attachment_groups)

    async def _commit_calendar_locked(self, events, attachment_groups):
        # Durable phase before first network write; unknown outcomes
        # remain blocked by the shared journal across restarts.
        await run_sync(self.db.save_task, self.task, 'importing', self.payload)
        self.stage = 'calendar_write'
        self.payload['import'] = await calendar_sync.write_events(
            self.adapter, events, self.journal, self.allowed,
            reviewer=partial(integration.review_calendar_matches, owner=self.owner),
        )
        self.payload['calendar_reconciliation'] = [
            {'index': index, **event['existing_match']['ai_review']}
            for index, event in enumerate(events)
            if event.get('existing_match', {}).get('ai_review')
        ]
        # A matched project is a separate, fully verified edit
        # following the private import.  No match remains
        # private; guesses and previously imported events are
        # never changed by the hourly worker.
        await run_sync(self.db.save_task, self.task, 'assigning_projects', self.payload)
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
                await run_sync(self.db.save_task, self.task, 'attaching', self.payload)
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
            counts = self.payload['import']
            self.payload['notice'] = (
                f"已完成並匯入行事曆：新增 {counts['created']} 筆、略過 {counts['skipped']} 筆既有行程。"
                '已同步的 CSV 無須再次上傳 EIP。'
            )
            ai_checks = self.payload.get('calendar_reconciliation', [])
            if ai_checks:
                model_checks = sum(item.get('method', 'ai') == 'ai' for item in ai_checks)
                if model_checks:
                    self.payload['notice'] += f'AI 已自動核對 {model_checks} 筆有差異的行程。'
                source_checks = len(ai_checks) - model_checks
                if source_checks:
                    self.payload['notice'] += f'已依原文確認並保留 {source_checks} 筆手動行程，未呼叫行程核對模型。'
                revisions = sum(bool(item.get('schedule_revision')) for item in ai_checks)
                shared = sum(bool(item.get('shared_event')) for item in ai_checks)
                if shared:
                    self.payload['notice'] += f'已沿用 {shared} 筆共享行程，未另建本人複本。'
                if revisions:
                    self.payload['notice'] += f'{revisions} 筆確認為同一事件的排程版本，未重建；請核對原行程時間。'
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
        if estimates := self.payload['output'].get('date_estimates'):
            self.payload['notice'] += f'。包含 {len(estimates)} 筆 AI 推估期限，起算依據與計算方式已列於行事曆說明，請核對。'
        partial = self.payload['output'].get('calendar_pending') and self.payload['output'].get('calendar_ready')
        if partial:
            self.payload['notice'] = '已匯入獨立驗證通過的行程；其餘項目保留待核對，不會自動重送。'
        if tracking := self.payload['output'].get('tracking_reminders'):
            self.payload['notice'] += f'包含 {len(tracking)} 筆 AI 推估追蹤提醒，並非公文明定期限，請核對。'
        if partial:
            self.db.save_task(self.task, 'needs_review', self.payload)
        else:
            self.db.save_task(self.task, 'done', self.payload)
