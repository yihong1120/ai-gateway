"""DocumentProcessor responsibilities for the shared document processor."""

from __future__ import annotations

import asyncio
import json
import logging
from copy import copy
from pathlib import Path
from typing import Any

from open_webui.models.files import FileModel

from examples.shared.runtime.concurrency import run_ocr
from examples.official_document_workflow.application.instruction_planner import (
    current_user_message_from_body,
    plan_current_instruction,
)
from examples.official_document_workflow.domain.calendar.csv_schema import (
    CALENDAR_CSV_FORMAT,
    CALENDAR_CSV_FORMAT_KEY,
)
from examples.official_document_workflow.domain.calendar.operations import (
    apply_calendar_operations,
)
from examples.official_document_workflow.domain.documents import (
    DOCX_CONTENT_TYPE,
    CloudDocumentExtraction,
    OfficialDocument,
    PreparedDocument,
    ProcessedDocument,
    ProcessingError,
)
from examples.official_document_workflow.domain.workflow import (
    JobPlan,
    OutputSelection,
)
from examples.official_document_workflow.infrastructure.storage.processing_cache import (
    upload_outcome,
)

from .components import DocumentRequest
from .exports import DocumentExports
from .ocr import DocumentOCR
from .parsing import DocumentParsing
from .rendering import DocumentRendering
from .settings import DocumentSettings
from .sources import DocumentSources
from .transport import ModelTransport
from .values import DocumentValues

logger = logging.getLogger(__name__)

from .clarifications import CalendarClarifications


class DocumentProcessor:
    Valves = DocumentSettings

    def __init__(self, *, pending=None, content_cache=None, evidence_cache=None) -> None:
        self.name = '公文 PDF／圖片批次：原始 PDF 改名、可編輯版面 DOCX 與行事曆 CSV（雲端 LLM）'
        from examples.official_document_workflow.infrastructure.storage.pending import PostgresClarifications
        self._pending = pending if pending is not None else PostgresClarifications()
        self._request = DocumentRequest(self.Valves(), content_cache=content_cache, evidence_cache=evidence_cache)
        self._install_services()

    def _install_services(self):
        self._request.outputs = self._pending.outputs
        self.values = DocumentValues()
        self.rendering = DocumentRendering()
        self.parser = DocumentParsing(self._request, self.values)
        self.ocr = DocumentOCR(self._request)
        self.sources = DocumentSources(self._request, self.ocr)
        self.exports = DocumentExports(self._request, self.ocr, self.sources, self.rendering)
        self.transport = ModelTransport(self._request, self.parser, lambda *a, **kw: self.emit_status(*a, **kw))
        self.clarifications = CalendarClarifications(self._request, self.exports, self.values, self.transport, self._pending)

    @property
    def valves(self):
        return self._request.settings

    @valves.setter
    def valves(self, value):
        self._request.settings = value

    @property
    def _usage_owner(self):
        return self._request.owner

    @_usage_owner.setter
    def _usage_owner(self, value):
        self._request.owner = value

    @property
    def _eip_processing_cache(self):
        return self._request.cache if self.valves.document_analysis_cache_enabled else None

    @_eip_processing_cache.setter
    def _eip_processing_cache(self, value):
        self._request.cache = value

    @property
    def content_cache(self):
        return self._request.content_cache if self.valves.document_analysis_cache_enabled else None

    @content_cache.setter
    def content_cache(self, value):
        self._request.content_cache = value

    @property
    def evidence_cache(self):
        return self._request.evidence_cache if self.valves.document_analysis_cache_enabled else None

    @evidence_cache.setter
    def evidence_cache(self, value):
        self._request.evidence_cache = value

    @property
    def analysis_results(self):
        return self._request.analysis_results if self.valves.document_analysis_cache_enabled else None

    @analysis_results.setter
    def analysis_results(self, value):
        self._request.analysis_results = value

    def __copy__(self):
        clone = type(self)(pending=self._pending)
        clone._request = copy(self._request)
        clone._install_services()
        components = {'values', 'rendering', 'parser', 'ocr', 'sources', 'exports', 'transport', 'clarifications'}
        for name, value in vars(self).items():
            if name not in components | {'_request'} and not callable(value):
                setattr(clone, name, value)
        return clone

    @property
    def calendar(self):
        from examples.official_document_workflow.domain.calendar.calendar_semantics import CalendarRules
        return CalendarRules(self.valves.cloud_max_events, self.parser.calendar_rows)

    async def process(
        self,
        body: dict[str, Any],
        __user__: dict[str, Any] | None = None,
        __files__: list[dict[str, Any]] | None = None,
        __request__: Any = None,
        __event_emitter__: Any = None,
        __chat_id__: str | None = None,
        __task__: str | None = None,
        __metadata__: dict[str, Any] | None = None,
    ) -> str:
        """Export one document or automatically grouped related PDF uploads.

        PDF uploads read each source once, then identify letters and related
        attachments with source-scoped evidence, including a single merged PDF.
        Image sets keep their existing extraction route. Neither entry writes an EIP calendar.
        """
        if __task__:
            # Open WebUI auxiliary tasks inherit the chat's attachments. They
            # must never start another OCR/model/export workflow. An empty
            # title lets WebUI use its normal first-message title fallback.
            return json.dumps(
                {
                    'title_generation': {'title': ''},
                    'tags_generation': {'tags': []},
                    'follow_up_generation': {'follow_ups': []},
                    'query_generation': {'queries': []},
                }.get(__task__, {})
            )

        if __metadata__ is not None:
            from examples.official_document_workflow.application.background_upload import submit

            return await submit(self, __user__, __metadata__, __event_emitter__)

        prepared_sources: tuple[PreparedDocument, ...] = ()
        try:
            user_id = self._user_id(__user__)
            pending_scope = self.clarifications.scope(user_id, __chat_id__)
            if not __files__ and pending_scope is not None:
                continuation = await self.clarifications.complete(
                    body=body, user_id=user_id, scope=pending_scope, request=__request__,
                )
                if continuation is not None:
                    return continuation
            # The dedicated chat model is a fixed file-export action. Validate
            # ownership first; accompanying chat text is not a workflow command.
            source_kind, source_files = await self.sources.owned_sources(__files__, user_id)
            # A valid current upload deliberately supersedes any older
            # clarification in this conversation.  It can never inherit a
            # calendar target from a prior document batch.
            if pending_scope is not None:
                await asyncio.to_thread(self.clarifications.pending.discard, pending_scope)
            job_plan = JobPlan()
            if any(self.sources._source_file_kind(file) == 'office' for file in source_files):
                await self.emit_status(
                    __event_emitter__,
                    f'已收到 {len(source_files)} 份文件，正在將 Word／ODT 轉為 PDF。',
                )
            prepared_sources = await self.sources.prepare_owned_source_documents(
                source_kind,
                source_files,
            )
            if source_kind == 'pdf':
                from examples.official_document_workflow.application.upload_batch import (
                    process_uploads,
                )

                return await process_uploads(
                    self,
                    user_id,
                    prepared_sources,
                    __event_emitter__,
                    __request__,
                )
            batch_mode = len(prepared_sources) > 1
            await self.emit_status(
                __event_emitter__,
                (f'已收到 {len(prepared_sources)} 份公文；已確認本次輸出需求，正在啟動雲端判讀與本機版面處理。'),
            )
            results: list[ProcessedDocument] = []
            failures: list[tuple[int, PreparedDocument, str]] = []

            for index, prepared_source in enumerate(prepared_sources, start=1):
                try:
                    await self.emit_status(
                        __event_emitter__,
                        (f'正在處理第 {index}/{len(prepared_sources)} 份公文：雲端判讀與本機 OCR 並行中。'),
                    )
                    results.append(
                        await self._process_prepared_document(
                            prepared_source,
                            user_id,
                            force_derived_pdf=batch_mode,
                            event_emitter=__event_emitter__,
                            batch_index=index,
                            batch_total=len(prepared_sources),
                            output_selection=job_plan.outputs,
                        ),
                    )
                except ProcessingError as exc:
                    failures.append((index, prepared_source, str(exc)))
                except Exception:
                    logger.exception('Official-document batch item failed index=%s', index)
                    failures.append((index, prepared_source, '文件處理暫時失敗'))
                else:
                    continue
                await self.emit_status(
                    __event_emitter__,
                    f'第 {index}/{len(prepared_sources)} 份已略過；繼續處理其餘檔案，CSV 將只彙整成功公文的已確認行程。',
                )

            if not results:
                if len(failures) == 1:
                    return f'無法產生公文輸出：{failures[0][2]}'
                return '無法產生公文輸出：這批 PDF 都未能完成；請確認附件狀態後分批重試。'

            calendar_rows: list[dict[str, str]] = []
            for result in results:
                if not result.deadlines:
                    continue
                calendar_rows.extend(
                    self.parser._calendar_rows(
                        owner_name=self._calendar_owner_name(result.document),
                        document=result.document,
                        deadlines=list(result.deadlines),
                    ),
                )

            calendar_transform = apply_calendar_operations(calendar_rows, job_plan)
            calendar_rows = list(calendar_transform.rows)

            csv_file: FileModel | None = None
            csv_name = ''
            csv_error = ''
            if calendar_rows and job_plan.outputs.calendar_csv:
                try:
                    await self.emit_status(
                        __event_emitter__,
                        '正在建立行事曆 CSV 與下載檔。',
                    )
                    csv_name, csv_file = await self._store_batch_calendar_csv(
                        user_id=user_id,
                        results=results,
                        calendar_rows=calendar_rows,
                        batch_mode=batch_mode,
                    )
                except ProcessingError as exc:
                    # Per-document exports remain useful even if the optional
                    # aggregate CSV storage fails.  Do not erase successful
                    # PDF/DOCX output merely because one final file failed.
                    csv_error = str(exc)

            output = self._render_processing_result(
                results=results,
                failures=failures,
                csv_file=csv_file,
                csv_name=csv_name,
                csv_error=csv_error,
                batch_mode=batch_mode,
                request=__request__,
            )
            await self.emit_status(
                __event_emitter__,
                f'批次處理完成：成功 {len(results)} 份、略過 {len(failures)} 份；請查看下載檔及略過原因。'
                if failures
                else '公文處理完成；下載檔已準備完成。',
                done=True,
            )
            report = upload_outcome.get()
            if report is not None:
                report['status'] = 'partial' if failures or csv_error else 'done'
            return output
        except ProcessingError as exc:
            return f'無法產生公文輸出：{exc}'
        except Exception:
            logger.exception('Official-document Pipe failed')
            return '公文處理暫時失敗；請確認原始附件狀態後再試或聯絡管理員。'
        finally:
            for prepared_source in prepared_sources:
                prepared_source.cleanup()

    @staticmethod
    async def emit_status(
        event_emitter: Any,
        description: str,
        *,
        done: bool = False,
    ) -> None:
        """Show safe progress without exposing document content or model data."""
        if not callable(event_emitter):
            return
        try:
            await event_emitter(
                {
                    'type': 'status',
                    'data': {'description': description, 'done': done},
                },
            )
        except Exception:
            # A disconnected browser must not cancel a file-processing job or
            # turn a successfully generated document into a failed request.
            logger.debug('Official-document progress event could not be delivered')

    async def plan_current_user_request(self, body: dict[str, Any] | None) -> JobPlan:
        """Build a bounded JobPlan from only the latest Open WebUI user turn.

        The planning graph is intentionally separate from the document model:
        it receives no PDF/OCR content and its JSON contract cannot grant file,
        tool, credential, model, or external-write authority.
        """
        if not self.valves.instruction_planning_enabled:
            return JobPlan()

        current_message = current_user_message_from_body(body)
        if len(current_message) > self.valves.instruction_plan_max_input_characters:
            raise ProcessingError('本次工作指令過長，請縮短後再試')

        async def planner(message: str) -> dict[str, Any]:
            return await self.transport._cloud_instruction_plan_payload(message)

        try:
            return await plan_current_instruction(current_message, planner)
        except ValueError as exc:
            logger.info('Controlled instruction planning rejected: %s', str(exc))
            raise ProcessingError('無法安全理解本次工作指令；請以較明確的方式重試') from exc

    async def _process_prepared_document(
        self,
        prepared_source: PreparedDocument,
        user_id: str,
        *,
        force_derived_pdf: bool,
        event_emitter: Any = None,
        batch_index: int = 1,
        batch_total: int = 1,
        output_selection: OutputSelection | None = None,
    ) -> ProcessedDocument:
        """Create selected exports and source-validated calendar rows.

        The semantic extraction remains mandatory for every document because
        it provides the verified metadata and deadlines.  Expensive positioned
        OCR is skipped only when the user explicitly opted out of DOCX.
        """
        selection = output_selection or OutputSelection()
        source_file = prepared_source.source_file
        source_path = prepared_source.pdf_path
        output_pdf_is_derived = force_derived_pdf or not (
            prepared_source.is_original_pdf and self.valves.rename_original_attachment
        )
        source_metadata = self.sources._derived_source_metadata(prepared_source)

        # This pipeline intentionally has one semantic path. A fully native
        # PDF contributes its raw text; a scan or mixed PDF contributes one
        # PaddleOCR-VL layout reading. That complete source goes to exactly
        # one cloud request. It never silently retries with a different model,
        # PDF vision request, local parser, or partial OCR reading.
        cloud_task: asyncio.Task[CloudDocumentExtraction] | None = None
        paddle_spotting_task: asyncio.Task[dict[str, Any]] | None = None
        try:
            scan_page_indices, paddle_response = await self.ocr.ocr_context(source_path)
            cloud_source = await asyncio.to_thread(
                self.ocr.semantic_source,
                source_path,
                scan_page_indices,
                paddle_response,
            )
            cloud_task = asyncio.create_task(
                self.transport._cloud_ocr_text_document_extraction(cloud_source),
            )
            # Let the cloud request leave the event loop before optional
            # positioned OCR starts. The two independent stages overlap, but
            # neither becomes a fallback for the other.
            await asyncio.sleep(0)
            if selection.editable_docx:
                await self.emit_status(
                    event_emitter,
                    (
                        f'第 {batch_index}/{batch_total} 份公文正在以 '
                        f'{"GPU OCR" if scan_page_indices else "PDF 原生文字層"} '
                        '建立可編輯 Word 版面，並進行雲端欄位與期限判讀。'
                    ),
                )
                if scan_page_indices:
                    paddle_spotting_task = asyncio.create_task(
                        run_ocr(
                            self.ocr.paddleocr_vl_spotting_response,
                            source_path,
                        ),
                    )
            cloud_result = await cloud_task
            paddle_spotting_response = await paddle_spotting_task if paddle_spotting_task is not None else None
        except BaseException:
            for task in (cloud_task, paddle_spotting_task):
                if task is not None and not task.done():
                    task.cancel()
            for task in (cloud_task, paddle_spotting_task):
                if task is not None:
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
            raise
        document = cloud_result.document
        deadlines = cloud_result.deadlines
        output_pdf_name = self.rendering.pdf_filename(document)
        await self.emit_status(
            event_emitter,
            (f'第 {batch_index}/{batch_total} 份公文正在建立所選下載檔。'),
        )
        output_pdf: FileModel | None = None
        docx_file: FileModel | None = None
        docx_name = ''
        docx_label = self.rendering._docx_download_label()
        searchable_pdf_file: FileModel | None = None
        searchable_pdf_name = ''
        try:
            if selection.renamed_pdf:
                if force_derived_pdf:
                    output_pdf = await self.exports._create_or_rename_pdf(
                        prepared_source,
                        user_id,
                        output_pdf_name,
                        document,
                        force_derived=True,
                    )
                else:
                    output_pdf = await self.exports._create_or_rename_pdf(
                        prepared_source,
                        user_id,
                        output_pdf_name,
                        document,
                    )

            if selection.editable_docx:
                docx_bytes = await asyncio.to_thread(
                    self.exports.render_word,
                    source_path,
                    paddle_spotting_response,
                )
                docx_name = self.rendering._docx_filename(document)
                docx_file = await self.exports.store_output(
                    user_id=user_id,
                    payload=docx_bytes,
                    display_name=docx_name,
                    content_type=DOCX_CONTENT_TYPE,
                    source_file_id=source_file.id,
                    kind='document_docx',
                    extra_metadata=source_metadata,
                )

            # Keep internal OCR for analysis/Word, but never publish another
            # PDF, including when persisted valves or old plans request one.
        except BaseException:
            if docx_file is not None:
                await self.exports.delete_output(docx_file)
            if output_pdf is not None and output_pdf_is_derived:
                await self.exports.delete_output(output_pdf)
            raise

        return ProcessedDocument(
            prepared_source=prepared_source,
            document=document,
            deadlines=deadlines,
            output_pdf=output_pdf,
            output_pdf_name=output_pdf_name,
            docx_file=docx_file,
            docx_name=docx_name,
            docx_label=docx_label,
            searchable_pdf_file=searchable_pdf_file,
            searchable_pdf_name=searchable_pdf_name,
        )

    async def _store_batch_calendar_csv(
        self,
        *,
        user_id: str,
        results: list[ProcessedDocument],
        calendar_rows: list[dict[str, str]],
        batch_mode: bool,
    ) -> tuple[str, FileModel]:
        """Store one calendar CSV after all successful documents are known."""
        first_result = results[0]
        if batch_mode:
            csv_name = self._batch_csv_filename(results)
            metadata: dict[str, Any] = {
                **self.sources._derived_source_metadata(first_result.prepared_source),
                'official_document_batch': {
                    'document_count': len(results),
                    'source_file_ids': [str(result.prepared_source.source_file.id) for result in results],
                },
            }
        else:
            csv_name = self.rendering._csv_filename(first_result.document)
            metadata = self.sources._derived_source_metadata(first_result.prepared_source)

        csv_file = await self.exports.store_output(
            user_id=user_id,
            payload=self.values.calendar_csv(calendar_rows),
            display_name=csv_name,
            content_type='text/csv; charset=utf-8',
            source_file_id=first_result.prepared_source.source_file.id,
            kind='calendar_csv',
            extra_metadata={**metadata, CALENDAR_CSV_FORMAT_KEY: CALENDAR_CSV_FORMAT},
        )
        return csv_name, csv_file


    def _batch_csv_filename(self, results: list[ProcessedDocument]) -> str:
        """Use a deterministic, human-readable name for one batch export."""
        first_date = results[0].document.issued.compact_roc
        return f'{first_date}_批次_{len(results)}份公文.csv'

    def _render_processing_result(
        self,
        *,
        results: list[ProcessedDocument],
        failures: list[tuple[int, PreparedDocument, str]],
        csv_file: FileModel | None,
        csv_name: str,
        csv_error: str,
        batch_mode: bool,
        request: Any,
    ) -> str:
        """Render only local, source-scoped status and download links."""
        lines = []
        if failures:
            lines.append(f'批次處理完成：成功 {len(results)} 份、略過 {len(failures)} 份。\n')
        for index, result in enumerate(results, start=1):
            if batch_mode:
                lines.append(f'### 第 {index} 份')
            for label, file, name in (
                ('PDF', result.output_pdf, result.output_pdf_name),
                ('Word', result.docx_file, result.docx_name),
            ):
                if file is not None and name:
                    lines.append(f'- {label}：{self.exports.download_link(request, file.id, name)}')
            lines.append('')
        if csv_file is not None:
            lines.append(f'- CSV：{self.exports.download_link(request, csv_file.id, csv_name)}')
            if failures:
                lines.append('CSV 僅包含成功公文中已確認的行程；已略過的檔案未納入。')
        elif csv_error:
            lines.append(f'CSV 未完成：{csv_error}')
        else:
            lines.append('成功處理的文件未含可確認的行程日期，未產生 CSV。')
        for index, prepared_source, message in failures:
            source_name = Path(str(prepared_source.source_file.filename)).name or f'第 {index} 份 PDF'
            lines.append(f'- 第 {index} 份（{source_name}）已略過（未納入 CSV）：{message}')
        return '\n'.join(lines)

    @staticmethod
    def _user_id(user: dict[str, Any] | None) -> str:
        user_id = (user or {}).get('id')
        if not isinstance(user_id, str) or not user_id.strip():
            raise ProcessingError('無法確認目前登入者，請重新登入後再試')
        return user_id

    @staticmethod
    def _calendar_owner_name(document: OfficialDocument) -> str:
        """Return only an evidenced document handler for the CSV Name cell."""
        return DocumentValues._csv_value(document.handler_name)

