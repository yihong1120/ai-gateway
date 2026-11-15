"""Bounded document-analysis workflow for the existing EIP entry points.

Coordinator and document analyst have independent model contexts/contracts.
Export, validation, date arithmetic, authentication and commits are NOT LLM
agents. No persistent checkpoint contains source text or credentials here;
the scheduler's existing journal remains the external-side-effect boundary.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from langsmith import tracing_context

from .analysis_state import AnalysisState
from .calendar_stage import CalendarStage
from .group_processing import outcome
from .group_processing import parse_group
from .group_processing import parse_group_document
from .long_source import group_text
from .long_source import source_lines
from .outcomes import CalendarResult
from .outcomes import DocumentExportResult
from .outcomes import DocumentResult
from .outcomes import GroupResult
from .prompts import EIP_RELATIVE_DATE_PROMPT
from .prompts import EIP_TRACKING_REMINDER_PROMPT
from .prompts import EMPTY_EVENTS_REPAIR_PROMPT
from .prompts import group_prompt
from .prompts import preparation_prompt
from .prompts import REPAIR_PROMPT
from examples.official_document_workflow.application.calendar_review import consolidate_events
from examples.official_document_workflow.application.calendar_review import review_calendar
from examples.official_document_workflow.application.preparation_review import (
    reviewed_preparations,
)
from examples.official_document_workflow.domain import documents as module
from examples.official_document_workflow.domain.calendar.calendar_semantics import literal_source_candidates
from examples.official_document_workflow.domain.calendar.calendar_semantics import temporal_source_candidates
from examples.official_document_workflow.domain.calendar.calendar_semantics import uncovered_time_candidates
from examples.official_document_workflow.domain.calendar.contracts import (
    GROUP_ANALYSIS_SCHEMA,
)
from examples.official_document_workflow.domain.calendar.preparation import (
    preparation_rows,
)
from examples.official_document_workflow.domain.calendar.relative_dates import (
    RelativeDateProof,
)
from examples.official_document_workflow.domain.calendar.tracking_reminders import (
    TrackingReminderProof,
)
from examples.official_document_workflow.domain.limits import MAX_EIP_ATTACHMENTS
from examples.official_document_workflow.domain.validation import content_issue
from examples.official_document_workflow.domain.validation import ContentValidationError
from examples.official_document_workflow.domain.workflow import JobPlan
from examples.official_document_workflow.services.analysis_reuse import analyze_content
from examples.official_document_workflow.services.analysis_reuse import source_revisions
from examples.official_document_workflow.services.source_readings import read_source
from examples.shared.runtime.modules import ModuleReference

_pymupdf = ModuleReference('pymupdf')


class DocumentAgent:
    def __init__(self, pipe,  emitter, *, preparation_date=None, correction_enabled=False):
        self.pipe = pipe
        self.emitter = emitter
        self.preparation_date = preparation_date
        self.correction_enabled = correction_enabled

    async def run(self, members, group_index, group_count, *, sources=None, allow_chunking=False):
        state = await self.analyze_group(
            members, group_index, group_count, sources=sources, allow_chunking=allow_chunking,
        )
        return await self.finish_calendar(state)

    async def analyze_group(self, members, group_index, group_count, *, sources=None, allow_chunking=False, document_focus=None, payload=None):
        state: AnalysisState = {
            'members': members,
            'group_index': group_index,
            'group_count': group_count,
            'document_focus': document_focus,
            'allow_chunking': allow_chunking,
            'primary': None,
            'document': None,
            'deadlines': [],
            'rows': [],
            'payload': None,
            'analysis_status': 'not_started',
            'analysis_attempts': 0,
            'repair_history': [],
        }
        state.update(await self.read_sources(state) if sources is None else {'sources': sources})
        state.update(
            await self.analyze(state) if payload is None else {
                'payload': payload, 'analysis_status': 'returned', 'analysis_attempts': 1,
                'analysis_input': {
                    'sources': [
                        {'source_id': sid, 'name': member.source_file.filename, 'lines': source_lines(source)}
                        for (sid, source), member in zip(state['sources'].items(), members, strict=True)
                    ], 'document_focus': document_focus,
                },
            },
        )
        await self.validate_with_correction(state, self.validate_document)
        return state

    async def validate_with_correction(self, state, validate):
        """Correct recognized content errors once, reusing in-memory sources.

        Invocation is outside the catch: provider/permission/storage failures
        must not become content retries. One budget covers metadata, literal
        calendar checks and the existing empty/incomplete-event correction.
        """
        if not self.correction_enabled:
            state.update(validate(state))
            return
        while True:
            try:
                state.update(validate(state))
                return
            except (ValueError, module.CloudExtractionError) as error:
                issue = content_issue(
                    error, metadata=isinstance(error, module.CloudExtractionError),
                    group_index=state['group_index'],
                    repair_attempted=state.get('analysis_attempts', 0) >= 2,
                )
                if issue is None:
                    raise
                if not issue.can_auto_repair or state.get('analysis_attempts', 0) >= 2:
                    raise issue from error
                self.prepare_correction(state, [str(error)], issue.code)
            # Never retry transport errors, or re-read/redo OCR in this loop.
            state.update(await self.analyze(state))

    def prepare_correction(self, state, errors, code):
        previous = state['payload'].get('events')
        state.update(
            csv_errors=errors,
            repair_history=[*state.get('repair_history', []), {'attempt': 1, 'code': code}],
            repair_had_events=bool(previous),
            repair_event_count=len(previous) if isinstance(previous, list) else 0,
        )

    def pending_calendar(self, state, issue):
        """Keep verified document exports, never an unverified calendar row."""
        state.update(
            rows=[], deadlines=[], analysis_status='partial',
            csv_errors=[issue.describe()], validation_issues=[issue.as_issue()],
        )

    async def finish_calendar(self, state):
        """Keep validated document metadata independent of later calendar calls."""
        try:
            await self.validate_with_correction(state, self.validate)
        except ContentValidationError as issue:
            self.pending_calendar(state, issue)
            return state
        if self.needs_repair(state):
            self.prepare_correction(state, state['csv_errors'], 'event_completeness_check')
            state.update(await self.analyze(state))
            try:
                await self.validate_with_correction(state, self.validate)
            except ContentValidationError as issue:
                self.pending_calendar(state, issue)
                return state
        if self.needs_review(state):
            try:
                state.update(await self.review_semantics(state))
            except ValueError as error:
                if not self.correction_enabled:
                    raise
                issue = content_issue(
                    error, group_index=state['group_index'],
                    repair_attempted=state.get('analysis_attempts', 0) >= 2,
                )
                if issue is None:
                    raise
                self.pending_calendar(state, issue)
        return state

    async def read_sources(self, state):
        sources = {}
        for index, p in enumerate(state['members'], 1):
            sid = f"S{index}"
            await self.pipe.emit_status(
                self.emitter,
                f"公文判讀 Agent：第 {state['group_index']}/{state['group_count']} 組，讀取附件 {index}/{len(state['members'])}",
            )
            sources[sid], _ = await read_source(self.pipe, p)
        return {'sources': sources}

    async def analyze(self, state):
        text, reading = await group_text(
            self.pipe,  state['members'], state['sources'], self.emitter,
            allow_chunking=state['allow_chunking'],
        )
        analysis_input = json.loads(text)
        # Receipt dates and flow IDs belong to each recipient. The model extracts
        # relative-period specifications; the verified local calculator binds
        # them to that recipient only after the common analysis is returned.
        if state.get('document_focus'):
            analysis_input['document_focus'] = state['document_focus']
            text = json.dumps(analysis_input, ensure_ascii=False, separators=(',', ':'))
        await self.pipe.emit_status(
            self.emitter,
            f"公文判讀 Agent：第 {state['group_index']}/{state['group_count']} 組，共同閱讀 {len(state['members'])} 個附件",
        )
        # Its separate prompt selects primary source, description spans,
        # evidence and deadlines. It cannot change the coordinator plan.
        prompt = group_prompt(self.pipe.valves.cloud_max_events)
        if getattr(self.pipe, '_estimate_relative_dates', False):
            prompt += EIP_RELATIVE_DATE_PROMPT
        if getattr(self.pipe, '_eip_tracking_reminders', False):
            prompt += EIP_TRACKING_REMINDER_PROMPT
        if self.preparation_date is not None:
            prompt += preparation_prompt(None if self.pipe.analysis_results is not None else self.preparation_date)
        if state.get('document_focus'):
            prompt += (
                '\n輸入 document_focus 指定本組要處理的本件文號、主文來源及原始行號範圍。'
                'primary_source_id 與 document.reference_number 必須符合此焦點，公文欄位及證據只能取自此主文範圍。'
                'sources 另含明確相關的轉函及附件供補充判讀，不能改選其他轉函當本件，也不能把引用文號當本件文號。'
                '只收錄與本件交辦有關的行程；相關原函可補充事項、日期、地點，但不要替中間收文者重複建立已轉交的工作。'
                '每個來源只保留相關公文及附件範圍，原始行號不變；不可引用未提供的其他公文行號。'
            )
        if state.get('analysis_attempts'):
            prompt += REPAIR_PROMPT
            if state['payload'].get('events') == []:
                prompt += EMPTY_EVENTS_REPAIR_PROMPT
            # Include the previous candidates as data, not prompt examples.
            # Without this, a stateless correction cannot preserve its items.
            request = json.loads(text)
            request['correction'] = {
                'errors': state.get('csv_errors', []),
                'previous_document': state['payload'].get('document'),
                'previous_events': state['payload'].get('events'),
                'literal_matches': literal_source_candidates(state['payload'].get('events'), state['sources']),
                'temporal_index': temporal_source_candidates(state['sources']),
                'uncovered_time_candidates': uncovered_time_candidates(
                    state['payload'].get('events') or [], state['sources'],
                ),
            }
            text = json.dumps(request, ensure_ascii=False, separators=(',', ':'))
        revisions = await source_revisions(self.pipe, state['members'])
        payload = await analyze_content(
            self.pipe,
            module.CloudSemanticSource(text=text, kind='paddle_layout_ocr'),
            stage='document-group', revisions=revisions,
            prompt=prompt,
            schema=GROUP_ANALYSIS_SCHEMA,
        )
        analysis_input['document_revisions'] = revisions
        return {
            'payload': payload,
            'analysis_status': 'returned',
            'analysis_attempts': state.get('analysis_attempts', 0) + 1,
            'source_reading': reading,
            'analysis_input': analysis_input,
        }

    def document_source(self, state):
        focus = state.get('document_focus')
        if focus:
            if (
                state['payload']['primary_source_id'] != focus['primary_source_id']
                or self.pipe.normalise_text(state['payload']['document'].get('reference_number') or '')
                != self.pipe.normalise_text(focus['reference_number'])
            ):
                raise ValueError('本組判讀文號或主文與分組不符，未套用其他函文的欄位。')
            source = state['sources'][focus['primary_source_id']]
            included = {n for span in focus['main_spans'] for n in range(span['start_line'], span['end_line'] + 1)}
            document_source = module.CloudSemanticSource(
                '\n'.join(line if n in included else '' for n, line in enumerate(source.text.splitlines(), 1)),
                source.kind,
            )
            return document_source
        return None

    def validate_document(self, state):
        primary, document = parse_group_document(
            self.pipe,  state['payload'], state['sources'],
            document_source=self.document_source(state),
        )
        return {'primary': primary, 'document': document}

    def validate(self, state):
        new_events = state['payload'].get('events')
        if new_events is None:
            raise ValueError('公文判讀 Agent 回應缺少 events。')
        if (state.get('repair_had_events') and new_events == []) or len(new_events) < state.get(
            'repair_event_count', 0,
        ):
            raise ValueError('格式更正省略了先前行程，請人工核對；未當成無事件完成。')
        diagnostics = []
        accepted = []
        primary, doc, deadlines, rows, errors = parse_group(
            self.pipe,

            state['payload'],
            state['sources'],
            diagnostics=diagnostics,
            accepted_events=accepted,
            document_source=self.document_source(state),
        )
        return {
            'primary': primary,
            'document': doc,
            'deadlines': deadlines,
            'rows': rows,
            'accepted_events': accepted,
            'metadata_notices': diagnostics,
            'csv_errors': errors,
            'analysis_status': 'validated' if not errors else 'partial',
        }

    def needs_repair(self, state):
        # Never loop on a cloud failure, missing/relative date, ambiguous group,
        # or semantic conflict. Only one provenance/format correction per group.
        previous_events = state.get('payload', {}).get('events')
        if (
            state.get('analysis_attempts') == 1
            and previous_events == []
            and state.get('payload', {}).get('calendar_contract_version') == 2
        ):
            return True
        if len(previous_events) > self.pipe.valves.cloud_max_events:
            return False  # Capacity is not a format error to "fix" by truncation.
        missing_times = uncovered_time_candidates(previous_events, state.get('sources', {}))
        if (
            state.get('analysis_attempts') == 1
            and state.get('payload', {}).get('calendar_contract_version') == 2
            and missing_times
        ):
            return True
        return False

    def needs_review(self, state):
        return bool(state.get('rows') and state.get('payload', {}).get('calendar_contract_version') == 2)

    async def review_semantics(self, state):
        if os.getenv('EIP_MAINTENANCE_MODEL_DELAY'):
            await asyncio.sleep(float(os.environ['EIP_MAINTENANCE_MODEL_DELAY']))
        await self.pipe.emit_status(self.emitter, '行事曆核對 Agent：確認事件、日期角色與附件關聯')
        review = await review_calendar(
            self.pipe,

            state['analysis_input'],
            state['accepted_events'],
            state['sources'],
        )
        if review['approved']:
            if any(c['decision'] in ('merge', 'reject') for c in review.get('checks', [])):
                events = consolidate_events(state['accepted_events'], review['checks'])
                deadlines, rows = self.pipe.calendar.events(

                    {'events': events},
                    state['sources'],
                    state['document'],
                )
                pending = [
                    {
                        **state['accepted_events'][c['index']],
                        'status': 'needs_review',
                        'start_date': None,
                        'start_time': None,
                        'end_date': None,
                        'end_time': None,
                        'review_reason': c['reason'],
                    }
                    for c in review['checks']
                    if c['decision'] == 'reject'
                ]
                messages = ['行事曆語意核對待確認：' + e['review_reason'] for e in pending]
                return {
                    'semantic_review': review,
                    'accepted_events': events,
                    'rows': rows,
                    'deadlines': deadlines,
                    'pending_events': pending,
                    'csv_errors': [*state.get('csv_errors', []), *messages],
                    'analysis_status': ('partial' if pending else state['analysis_status']),
                }
            return {'semantic_review': review}
        message = '行事曆語意核對待確認：' + '；'.join(review['issues'])
        return {
            'semantic_review': review,
            'rows': [],
            'deadlines': [],
            'csv_errors': [*state.get('csv_errors', []), message],
            'analysis_status': 'partial',
        }


def write_pdf_pages(source_path, destination, page_numbers):
    """Select original PDF pages using PyMuPDF; never overwrite the upload."""
    pymupdf = _pymupdf.load()

    with pymupdf.open(source_path) as pdf:
        if not page_numbers or any(not 1 <= n <= pdf.page_count for n in page_numbers):
            raise ValueError('拆分頁碼與原始 PDF 不符，未輸出錯誤頁面。')
        pdf.select([n - 1 for n in page_numbers])
        pdf.save(destination, garbage=3, deflate=True, no_new_id=True)


class GroupExporter:
    """Store deterministic outputs while preserving original geometry and text."""

    def __init__(self, pipe, owner, group, document: DocumentResult, plan, emitter):
        self.pipe = pipe
        self.owner = owner
        self.group = group
        self.document = document
        self.plan = plan
        self.emitter = emitter

    async def save(self, prepared, data, name, content_type, kind, metadata):
        stored = await self.pipe.exports.store_output(
            user_id=self.owner,
            payload=data,
            display_name=name,
            content_type=content_type,
            source_file_id=prepared.source_file.id,
            kind=kind,
            extra_metadata=metadata,
        )
        return {
            'id': stored.id,
            'name': stored.filename,
            'kind': kind,
            'group_index': self.document.group_index,
            'source_name': prepared.source_file.filename,
        }

    async def export(self, prepared, index, *, spotting_required=True, filename_stem=None, extra_metadata=None, page_numbers=None, load_word_document=None) -> DocumentExportResult:
        files = []
        try:
            primary = self.document.primary
            document = self.document.document
            group_index = self.document.group_index
            sid = f"S{index}"
            label = f"第 {group_index} 組附件 {index}（{prepared.source_file.filename}）"
            if filename_stem is not None:
                stem = filename_stem
            elif document is None:
                stem = f"待確認_公文組{group_index:02d}_檔案{index:02d}"
            else:
                # This is the output's position in the group, not an inferred
                # attachment number from the document or its original filename.
                suffix = '' if sid == primary else f"_檔案{index:02d}"
                stem = Path(self.pipe.rendering.pdf_filename(document, suffix=suffix)).stem
            metadata = {
                'eip_group_source_ids': list(self.group),
                'eip_source_file_id': prepared.source_file.id,
                **(extra_metadata or {}),
            }
            with ExitStack() as cleanup:
                if page_numbers is not None:
                    directory = cleanup.enter_context(tempfile.TemporaryDirectory(prefix='official-split-'))
                    split_path = Path(directory) / 'document.pdf'
                    await asyncio.to_thread(write_pdf_pages, prepared.pdf_path, split_path, page_numbers)
                    prepared = replace(prepared, pdf_path=split_path)
                    metadata['official_document_source_pages'] = list(page_numbers)
                # Both formats use the same selected pages. The parent file ID is
                # retained for ownership/provenance, while the temporary PDF dies here.
                if self.plan.outputs.renamed_pdf:
                    data = await asyncio.to_thread(prepared.pdf_path.read_bytes)
                    files.append(
                        await self.save(
                            prepared,
                            data,
                            stem + '.pdf',
                            'application/pdf',
                            'document_pdf',
                            metadata,
                        ),
                    )
                if self.plan.outputs.editable_docx:
                    await self.pipe.emit_status(
                        self.emitter, '文件輸出工具：' + label + '，建立可編輯 Word' if load_word_document is None
                        else f"正在輸出第 {group_index} 組 Word（相同頁面的轉檔結果共用）。",
                    )
                    data = await self.pipe.exports.word_payload(
                        prepared.pdf_path, spotting_required=spotting_required, load_word_document=load_word_document,
                    )
                    files.append(
                        await self.save(
                            prepared,
                            data,
                            stem + '.docx',
                            module.DOCX_CONTENT_TYPE,
                            'document_docx',
                            metadata,
                        ),
                    )
        except (ValueError, OSError) as exc:
            # Never retry a failed/uncertain write. Earlier acknowledged outputs
            # remain downloads even when a later format could not be completed.
            return DocumentExportResult(tuple(files), (str(exc),))
        return DocumentExportResult(tuple(files))

    async def run(self) -> DocumentExportResult:
        files, errors = [], []
        for index, prepared in enumerate(self.document.members, 1):
            result = await self.export(
                prepared, index,
                spotting_required=prepared.source_file.id not in self.document.native_source_ids,
            )
            files.extend(result.files)
            errors.extend(f'附件 {index}（{prepared.source_file.filename}）部分輸出未完成：{error}' for error in result.errors)
        return DocumentExportResult(tuple(files), tuple(errors))


class BatchWorkflow:
    def __init__(self, pipe,  owner, ids, groups, prepared, emitter, instruction):
        self.pipe = pipe
        self.owner = owner
        self.ids = ids
        self.groups = groups
        self.prepared = prepared
        self.emitter = emitter
        self.instruction = instruction
        self.calendar_stage = CalendarStage(pipe, owner)
        self.pipe._estimate_relative_dates = True
        self.preparation_date = datetime.now(
            ZoneInfo('Asia/Taipei'),
        ).date() if getattr(pipe, '_eip_tracking_reminders', False) else None
        self.seen_preparations = set()
        self.document_agent = DocumentAgent(
            pipe,  emitter, preparation_date=self.preparation_date, correction_enabled=True,
        )

    async def coordinate(self):
        await self.pipe.emit_status(self.emitter, '工作協調 Agent：理解本次指令與輸出需求')
        if getattr(self.pipe, '_eip_calendar_only', False):
            return JobPlan(outputs={'renamed_pdf': False, 'editable_docx': False, 'calendar_csv': True})
        body = {'messages': [{'role': 'user', 'content': self.instruction}]} if self.instruction else {'messages': []}
        value = await self.pipe.plan_current_user_request(body)
        return JobPlan.model_validate(value)

    async def delegate_group(self, state):
        index = state['index']
        group = self.groups[index]
        # Membership is server-owned; the model cannot delegate arbitrary files.
        analysis = await self.document_agent.run(
            [p for p in self.prepared if p.source_file.id in group],
            index + 1,
            len(self.groups),
        )
        group_result = GroupResult.from_analysis(analysis)
        calendar = group_result.calendar
        plan = state['plan']
        errors = []
        rows = []
        notices = []
        calendar_errors = []
        if self.preparation_date is not None and analysis.get('rows') and analysis.get('semantic_review', {}).get('approved') is True:
            try:
                accepted = set(await reviewed_preparations(self.pipe,  analysis))
                position = 0
                filtered = []
                for event in analysis.get('accepted_events', []):
                    suggestions = event.get('preparation_reminders', [])
                    kept = [value for n, value in enumerate(suggestions, position) if n in accepted]
                    position += len(suggestions)
                    filtered.append({**event, 'preparation_reminders': kept})
                if len(accepted) < position:
                    notices.append('部分 AI 事前準備建議未通過獨立核對，未加入行事曆；原定行程保留。')
                prep_analysis = {**analysis, 'accepted_events': filtered}
                additions, prep_notices = preparation_rows(
                    self.pipe.calendar, prep_analysis, today=self.preparation_date, seen=self.seen_preparations,
                )
                calendar = calendar.with_reminders(additions)
                notices.extend(prep_notices)
                if additions:
                    notices.append(f'已加入 {len(additions)} 筆 AI 建議事前準備提醒；非公文明定期限。')
            except Exception:
                notices.append('事前準備提醒未通過獨立核對或核對服務暫不可用；原定行程仍依原規則處理。')
        if plan.outputs.calendar_csv:
            errors.extend(calendar.issues)
            rows = CalendarStage.approved_rows(calendar)
            if not (rows and calendar.reviewed):
                calendar_errors.extend(calendar.issues)
        errors.extend(analysis.get('metadata_notices', []))
        if analysis['analysis_status'] == 'validated' and not analysis['deadlines'] and not analysis.get('csv_errors'):
            notices.append(f"第 {index+1} 組沒有明確期限，未新增行程。")
        exports = await GroupExporter(self.pipe,  self.owner, group, group_result.document, plan, self.emitter).run()
        errors.extend(exports.errors)
        if analysis.get('document') is None:
            notices.append(f"第 {index+1} 組公文欄位未確認；可用輸出暫以「待確認_公文組{index+1:02d}_檔案序號」命名。")
        state['index'] += 1
        state['files'].extend(exports.files)
        state['calendar_pending'].extend(analysis.get('csv_errors', []))
        state['validation_issues'].extend(analysis.get('validation_issues', []))
        state['date_estimates'].extend(
            {'group_index': index + 1, **event['_relative_date_proof'].audit()}
            for event in analysis.get('accepted_events', [])
            if analysis.get('rows') and analysis.get('semantic_review', {}).get('approved')
            and isinstance(event.get('_relative_date_proof'), RelativeDateProof)
        )
        state['tracking_reminders'].extend(
            {'group_index': index + 1, **event['_tracking_reminder_proof'].audit()}
            for event in analysis.get('accepted_events', [])
            if analysis.get('rows') and analysis.get('semantic_review', {}).get('approved')
            and isinstance(event.get('_tracking_reminder_proof'), TrackingReminderProof)
        )
        state['rows'].extend(rows)
        state['errors'].extend(f"第 {index+1} 組：{error}" for error in errors)
        state['calendar_errors'].extend(calendar_errors)
        state['notices'].extend(notices)
        state['has_deadlines'] |= bool(analysis['deadlines'])
        state['agent_runs'].append(
            {
                'agent': 'document_analyst',
                'group_index': index + 1,
                'source_count': len(group),
                'status': analysis['analysis_status'],
                'analysis_attempts': analysis.get('analysis_attempts', 0),
                'repair_history': analysis.get('repair_history', []),
                'source_reading': analysis.get('source_reading', {}),
                'semantic_review': analysis.get('semantic_review'),
            },
        )

    async def calendar(self, state):
        if state['plan'].outputs.calendar_csv:
            await self.pipe.emit_status(self.emitter, '行事曆工具：驗證期限、套用使用者提醒並產生 CSV')
            prepared = self.calendar_stage.prepare(
                CalendarResult(tuple(state['rows']), tuple(state['calendar_errors']), True), state['plan'],
            )
            state['warnings'].extend(prepared.pending)
            state['notices'].extend(prepared.notices)
            result = await self.calendar_stage.save(
                prepared, f"行事曆_附件組_{len(self.groups)}組.csv", self.ids[0],
                {
                    'eip_group_source_ids': self.ids, 'relative_date_estimates': state['date_estimates'],
                    'tracking_reminders': state['tracking_reminders'],
                },
            )
            if result.error:
                state['errors'].append('CSV 未完成：' + result.error)
                state['calendar_errors'].append(result.error)
            if result.file is not None:
                f = result.file
                if result.notice:
                    state['notices'].append(result.notice)
                state['files'].append({'id': f.id, 'name': f.filename, 'kind': 'calendar_csv'})
                state['csv_ids'].append(f.id)

    def finish(self, state):
        errors = state['errors']
        if state['plan'].needs_clarification:
            errors = [f"工作指令待釐清：{state['plan'].clarification}"]
        result = outcome(
            state['files'],
            state['csv_ids'],
            errors,
            state['notices'],
            state['has_deadlines'],
            state['warnings'],
        )
        result['calendar_ready'] = (
            bool(state['csv_ids'])
            and not state.get('calendar_errors')
            and not state['warnings']
            and not state['plan'].needs_clarification
        )
        result['calendar_errors'] = list(state.get('calendar_errors', []))
        result['calendar_pending'] = list(state.get('calendar_pending', []))
        result['validation_issues'] = list(state.get('validation_issues', []))
        result['date_estimates'] = list(state.get('date_estimates', []))
        result['tracking_reminders'] = list(state.get('tracking_reminders', []))
        if result['tracking_reminders']:
            result['notices'].append('包含 AI 推估追蹤提醒；日期不是公文明定期限，請核對。')
        if result['date_estimates']:
            result['notices'].append('相對期限已依收文日期或發文日期推估，行事曆以「期限推估」標示，請核對實際期限。')
        result['calendar_status'] = (
            'ready'
            if result['calendar_ready']
            else (
                'needs_review'
                if state.get('calendar_errors')
                else 'no_events' if not state['has_deadlines'] else 'not_ready'
            )
        )
        result['orchestration'] = {
            'framework': 'direct',
            'mode': 'bounded_multi_agent',
            'agents': [
                {
                    'agent': 'coordinator',
                    'status': ('needs_clarification' if state['plan'].needs_clarification else 'planned'),
                    'model_used': bool(state['instruction'].strip()),
                },
                *state['agent_runs'],
            ],
        }
        return result

    async def run(self):
        state = {
            'instruction': self.instruction,
            'index': 0,
            'files': [],
            'csv_ids': [],
            'errors': [],
            'notices': [],
            'warnings': [],
            'rows': [],
            'has_deadlines': False,
            'agent_runs': [],
            'calendar_errors': [],
            'calendar_pending': [],
            'validation_issues': [],
            'date_estimates': [],
            'tracking_reminders': [],
        }
        state['plan'] = await self.coordinate()
        if not state['plan'].needs_clarification:
            while state['index'] < len(self.groups):
                await self.delegate_group(state)
            await self.calendar(state)
        return self.finish(state)


async def process_with_team(pipe,  owner, ids, groups, instruction='', emitter=None):
    flat = [fid for group in groups for fid in group]
    if (
        not ids
        or len(ids) > MAX_EIP_ATTACHMENTS
        or len(set(ids)) != len(ids)
        or len(flat) != len(ids)
        or set(flat) != set(ids)
        or any(not g for g in groups)
    ):
        raise ValueError('附件分組與本次選取不符，請重新查詢。')
    kind, owned = await pipe.owned_sources([{'id': i, 'type': 'file'} for i in ids], owner)
    prepared = await pipe.prepare_owned_source_documents(kind, owned)
    with ExitStack() as cleanup:
        for item in prepared:
            cleanup.callback(item.cleanup)
        if len(prepared) != len(ids) or {p.source_file.id for p in prepared} != set(ids):
            raise ValueError('已驗證的來源與附件分組不符。')
        cleanup.enter_context(tracing_context(enabled=False))
        return await BatchWorkflow(pipe,  owner, ids, groups, prepared, emitter, instruction).run()
