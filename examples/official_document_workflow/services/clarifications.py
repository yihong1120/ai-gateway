"""Source-bound, expiring calendar clarification state transitions."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

from open_webui.models.files import FileModel
from open_webui.models.files import Files

from .calendar_delivery import CalendarOutputDelivery
from .components import ConfiguredService
from examples.official_document_workflow.application.instruction_planner import current_user_message_from_body
from examples.official_document_workflow.application.instruction_planner import resolve_pending_reminder_choice
from examples.official_document_workflow.application.pending import PendingClarificationScope
from examples.official_document_workflow.application.pending import PendingRepository
from examples.official_document_workflow.domain.calendar.operations import apply_calendar_operations
from examples.official_document_workflow.domain.calendar.operations import CalendarTransformResult
from examples.official_document_workflow.domain.documents import PendingCalendarReminder
from examples.official_document_workflow.domain.documents import ProcessedDocument
from examples.official_document_workflow.domain.documents import ProcessingError
from examples.official_document_workflow.domain.workflow import JobPlan
from examples.official_document_workflow.domain.workflow import PendingReminderResolution
from examples.shared.runtime.io import run_sync

logger = logging.getLogger(__name__)


class CalendarClarifications(ConfiguredService):
    def __init__(
        self,
        request,
        exports,
        values,
        transport,
        pending: PendingRepository[PendingCalendarReminder],
    ):
        super().__init__(request)
        self.exports, self.values, self.transport = exports, values, transport
        self.pending = pending

        self.delivery = CalendarOutputDelivery(pending.outputs, exports, values)

    @staticmethod
    def scope(
        user_id: str,
        chat_id: str | None,
    ) -> PendingClarificationScope | None:
        """Build a follow-up scope only from Open WebUI server parameters."""

        try:
            return PendingClarificationScope(
                user_id=user_id,
                chat_id=chat_id or '',
            )
        except ValueError:
            # Direct API/legacy calls may not carry stable metadata.  In that
            # case no state is retained rather than weakening the scope to a
            # user-only key.
            return None

    @staticmethod
    def _attachment_fingerprint(source_files: tuple[FileModel, ...]) -> str:
        """Hash stable owned-file identities without storing their contents."""

        material = '\x1e'.join(
            sorted(
                f'{str(file.id)}\x1f{str(file.hash or "")}' for file in source_files
            ),
        )
        if not material:
            raise ProcessingError('無法建立本次附件的安全識別')
        return 'sha256:' + hashlib.sha256(material.encode('utf-8')).hexdigest()

    @classmethod
    def _attachment_fingerprint_for_results(
        cls,
        results: list[ProcessedDocument],
    ) -> str:
        source_files: list[FileModel] = []
        seen_ids: set[str] = set()
        for result in results:
            for source_file in result.prepared_source.source_files:
                file_id = str(source_file.id)
                if file_id not in seen_ids:
                    seen_ids.add(file_id)
                    source_files.append(source_file)
        return cls._attachment_fingerprint(tuple(source_files))

    @staticmethod
    def _first_resolvable_reminder_operation(
        calendar_rows: tuple[dict[str, str], ...],
        job_plan: JobPlan,
        calendar_transform: CalendarTransformResult,
        *,
        selected_deadline_indices: dict[int, int] | None = None,
    ) -> int | None:
        """Find only a genuine one-vs-all target choice, never another error."""

        if len(calendar_rows) < 2:
            return None
        selected = selected_deadline_indices or {}
        for index, (operation, report) in enumerate(
            zip(job_plan.calendar_operations, calendar_transform.reports),
        ):
            if (
                operation.operation == 'add_relative_reminder'
                and operation.target == 'single_verified_deadline'
                and index not in selected
                and report.applied == 0
                and report.clarification
            ):
                return index
        return None

    @staticmethod
    def _pending_payload_size_bytes(payload: PendingCalendarReminder) -> int:
        """Measure only a server-created snapshot before encrypting the continuation."""

        try:
            serializable = {
                'calendar_rows': payload.calendar_rows,
                'job_plan': payload.job_plan.model_dump(mode='json'),
                'selected_deadline_indices': payload.selected_deadline_indices,
                'unresolved_operation_index': payload.unresolved_operation_index,
                'source_file_ids': payload.source_file_ids,
                'csv_base_name': payload.csv_base_name,
                'batch_mode': payload.batch_mode,
            }
            return len(
                json.dumps(
                    serializable,
                    ensure_ascii=False,
                    separators=(',', ':'),
                    sort_keys=True,
                ).encode('utf-8'),
            )
        except (TypeError, ValueError) as exc:
            raise ProcessingError('無法安全暫存本次期限選擇') from exc

    def _pending_payload_is_within_limit(
        self, payload: PendingCalendarReminder,
    ) -> bool:
        """Return false rather than failing core exports when the continuation is too large."""

        try:
            return (
                self._pending_payload_size_bytes(payload)
                <= self.valves.pending_clarification_max_payload_bytes
            )
        except ProcessingError:
            logger.warning('Could not measure pending calendar snapshot size')
            return False

    def _retain_pending_calendar_state(
        self,
        *,
        scope: PendingClarificationScope,
        results: list[ProcessedDocument],
        payload: PendingCalendarReminder,
    ) -> bool:
        """Save a bounded, source-bound continuation record when it is safe."""

        if not self._pending_payload_is_within_limit(payload):
            return False
        self.pending.put(
            scope,
            attachment_fingerprint=self._attachment_fingerprint_for_results(results),
            payload=payload,
            ttl_seconds=self.valves.pending_clarification_ttl_seconds,
        )
        return True

    @staticmethod
    def _pending_deadline_options(
        calendar_rows: tuple[dict[str, str], ...],
    ) -> tuple[str, ...]:
        """Expose only canonical verified dates to the choice model."""

        return tuple(str(row.get('Start Date', '')).strip() for row in calendar_rows)

    @staticmethod
    def _pending_question(
        payload: PendingCalendarReminder,
        clarification: str,
    ) -> str:
        options = CalendarClarifications._pending_deadline_options(
            payload.calendar_rows,
        )
        rendered_options = '\n'.join(
            f'- {index}：{date_value}' for index, date_value in enumerate(options)
        )
        return (
            f'仍需要釐清：{clarification}\n\n'
            '請直接回覆「全部」或指定下列其中一個期限的日期／編號；'
            '在有效時間內不需重新上傳附件。\n'
            f'{rendered_options}'
        )

    async def _owned_pending_source_files(
        self,
        payload: PendingCalendarReminder,
        user_id: str,
    ) -> tuple[FileModel, ...] | None:
        """Re-authorise the original source IDs before a follow-up writes CSV."""

        files: list[FileModel] = []
        for file_id in payload.source_file_ids:
            source_file = await Files.get_file_by_id_and_user_id(file_id, user_id)
            if source_file is None:
                return None
            files.append(source_file)
        return tuple(files)

    @staticmethod
    def _plan_with_all_deadlines(job_plan: JobPlan, operation_index: int) -> JobPlan:
        """Change exactly one previously-approved operation to all targets."""

        plan_payload = job_plan.model_dump()
        operations = plan_payload.get('calendar_operations')
        if not isinstance(operations, list) or not 0 <= operation_index < len(
            operations,
        ):
            raise ProcessingError('待確認的提醒操作已失效')
        operation = operations[operation_index]
        if not isinstance(operation, dict):
            raise ProcessingError('待確認的提醒操作格式無效')
        operation['target'] = 'all_verified_deadlines'
        return JobPlan.model_validate(plan_payload)

    async def _claim_pending(self, existing, fingerprint):
        task = asyncio.create_task(
            run_sync(
                self.pending.claim,
                existing.scope,
                expected_generation=existing.generation,
                attachment_fingerprint=fingerprint,
            ),
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # The SQL thread cannot be cancelled halfway through a claim.
            # Drain it and release only its own generation before returning.
            result = await asyncio.gather(task, return_exceptions=True)
            if result[0] is not None and not isinstance(result[0], BaseException):
                await run_sync(self.pending.restore, result[0])
            raise

    async def complete(
        self,
        *,
        body: dict[str, Any] | None,
        user_id: str,
        scope: PendingClarificationScope | None,
        request: Any,
    ) -> str | None:
        """Optionally finish one safe no-reupload clarification turn.

        This path is intentionally narrower than the normal Pipe: it cannot
        re-run OCR/cloud extraction, alter original files, or accept a new
        calendar operation.  It only resolves the existing one-vs-all choice
        and, when requested, stores a replacement CSV.
        """

        if scope is None:
            return None
        existing = await run_sync(self.pending.get, scope)
        reviewing = existing is None
        if reviewing:
            existing = await run_sync(self.pending.review, scope)
        if existing is None or not isinstance(
            existing.payload, PendingCalendarReminder,
        ):
            return None
        pending_sources = await self._owned_pending_source_files(
            existing.payload, user_id,
        )
        if pending_sources is None:
            await run_sync(
                self.pending.discard, scope, expected_generation=existing.generation,
            )
            return (
                '先前待確認的公文附件已不存在或不再屬於目前帳號；請重新附加檔案後處理。'
            )
        try:
            attachment_fingerprint = self._attachment_fingerprint(pending_sources)
        except ProcessingError:
            await run_sync(
                self.pending.discard, scope, expected_generation=existing.generation,
            )
            return '先前待確認的公文附件識別已失效；請重新附加檔案後處理。'
        if reviewing:
            if attachment_fingerprint != existing.attachment_fingerprint:
                return '先前附件版本已變更；輸出結果需核對，請重新附加目前附件。'
            result = await self.delivery.recover(existing)
            if result.state == 'not_written':
                await run_sync(self.pending.restore, existing)
                return '先前下載檔確認未建立，期限選擇仍保留；請再次回覆期限選項。'
            if result.state == 'stored':
                await run_sync(self.pending.finalize, existing)
                return (
                    '已找回先前建立的更新後行事曆 CSV：'
                    + self.exports.download_link(request, result.file.id, result.name)
                )
            return self._pending_output_review_notice(result.output_id)
        record = await self._claim_pending(existing, attachment_fingerprint)
        if record is None or not isinstance(record.payload, PendingCalendarReminder):
            return '這個期限選擇已失效或已被其他請求完成；請重新附加檔案後處理。'

        payload = record.payload
        current_message = current_user_message_from_body(body)
        if not current_message:
            await run_sync(self.pending.restore, record)
            return self._pending_question(
                payload,
                '請選擇提醒要套用所有期限或其中一個期限。',
            )

        write_started = False
        try:
            resolution = await self._resolve_pending_reminder_choice(
                current_message,
                self._pending_deadline_options(payload.calendar_rows),
            )
            if resolution.decision == 'needs_clarification':
                await run_sync(self.pending.restore, record)
                return self._pending_question(
                    payload,
                    resolution.clarification or '請選擇所有期限或其中一個期限。',
                )

            updated_plan = payload.job_plan
            selected_indices = dict(payload.selected_deadline_indices)
            if resolution.decision == 'all_verified_deadlines':
                updated_plan = self._plan_with_all_deadlines(
                    updated_plan,
                    payload.unresolved_operation_index,
                )
                selected_indices.pop(payload.unresolved_operation_index, None)
            elif resolution.decision == 'selected_verified_deadline':
                selected_index = resolution.selected_deadline_index
                if selected_index is None:
                    raise ProcessingError('期限選擇沒有指定有效項目')
                selected_indices[payload.unresolved_operation_index] = selected_index
            else:
                raise ProcessingError('期限選擇結果無法識別')

            calendar_transform = apply_calendar_operations(
                list(payload.calendar_rows),
                updated_plan,
                selected_deadline_indices=selected_indices,
            )
            next_index = self._first_resolvable_reminder_operation(
                payload.calendar_rows,
                updated_plan,
                calendar_transform,
                selected_deadline_indices=selected_indices,
            )
            if next_index is not None:
                remaining_ttl = self.pending.remaining(record)
                if remaining_ttl <= 0:
                    await run_sync(self.pending.finalize, record)
                    return '期限選擇已逾時；請重新附加檔案後處理。'
                next_payload = PendingCalendarReminder(
                    calendar_rows=payload.calendar_rows,
                    job_plan=updated_plan,
                    selected_deadline_indices=tuple(sorted(selected_indices.items())),
                    unresolved_operation_index=next_index,
                    source_file_ids=payload.source_file_ids,
                    csv_base_name=payload.csv_base_name,
                    batch_mode=payload.batch_mode,
                )
                if not self._pending_payload_is_within_limit(next_payload):
                    await run_sync(self.pending.finalize, record)
                    return (
                        '剩餘提醒的行事曆資料超過短暫續接的資料上限，'
                        '因此未建立更新後 CSV；請重新附加公文並明確指定各提醒的期限。'
                    )
                if not await run_sync(
                    self.pending.advance, record, next_payload,
                ):
                    return '期限選擇已被新附件取代或逾時；請重新確認目前附件。'
                return self._pending_question(
                    next_payload,
                    '還有另一個提醒需要選擇適用的期限。',
                )

            csv_file, csv_name, csv_error = None, '', ''
            if updated_plan.outputs.calendar_csv and calendar_transform.rows:
                write_started = True
                task = asyncio.create_task(
                    self.delivery.deliver(record, payload, calendar_transform.rows),
                )
                try:
                    delivery = await asyncio.shield(task)
                except asyncio.CancelledError:
                    results = await asyncio.gather(task, return_exceptions=True)
                    if (
                        not isinstance(results[0], BaseException)
                        and results[0].state == 'not_written'
                    ):
                        await run_sync(self.pending.restore, record)
                    raise
                if delivery.state == 'not_written':
                    await run_sync(self.pending.restore, record)
                    return (
                        '本次下載檔確認未建立，期限選擇仍保留；請稍後以相同選項重試。'
                    )
                if delivery.state == 'unknown':
                    return self._pending_output_review_notice(delivery.output_id)
                csv_name, csv_file = delivery.name, delivery.file
            output = self._render_pending_calendar_completion(
                calendar_transform=calendar_transform,
                csv_file=csv_file,
                csv_name=csv_name,
                csv_error=csv_error,
                job_plan=updated_plan,
                request=request,
            )
            await run_sync(self.pending.finalize, record)
            return output
        except asyncio.CancelledError:
            if not write_started:
                await run_sync(self.pending.restore, record)
            raise
        except ValueError:
            # The one-shot claim must not lose a valid choice merely because
            # the small planner request or output storage had a transient
            # failure.  ``restore`` preserves the original expiry.
            if write_started:
                return self._pending_output_review_notice(self.delivery.key(record))
            await run_sync(self.pending.restore, record)
            return (
                '無法安全完成這次期限選擇；請以「全部」或有效期限日期／編號再試一次。'
            )
        except Exception:
            logger.exception('Pending calendar clarification failed')
            if write_started:
                return self._pending_output_review_notice(self.delivery.key(record))
            await run_sync(self.pending.restore, record)
            return '期限選擇暫時失敗；請稍後以相同對話回覆，或重新附加檔案後處理。'

    @staticmethod
    def _pending_output_review_notice(output_id):
        return (
            '這個期限選擇正在處理，或先前輸出結果尚待核對；未重送寫入。'
            '請稍後於相同對話再次回覆以查詢結果。輸出識別碼：' + str(output_id)
        )

    async def _resolve_pending_reminder_choice(
        self,
        current_message: str,
        deadline_options: tuple[str, ...],
    ) -> PendingReminderResolution:
        """Resolve only a server-issued deadline target question."""

        if len(current_message) > self.valves.instruction_plan_max_input_characters:
            raise ValueError('期限選擇訊息過長')

        async def resolver(
            message: str,
            options: tuple[str, ...],
        ) -> dict[str, Any]:
            return await self.transport._cloud_pending_reminder_choice_payload(
                message, options,
            )

        return await resolve_pending_reminder_choice(
            current_message,
            deadline_options,
            resolver,
        )

    def _render_pending_calendar_completion(
        self,
        *,
        calendar_transform: CalendarTransformResult,
        csv_file: FileModel | None,
        csv_name: str,
        csv_error: str,
        job_plan: JobPlan,
        request: Any,
    ) -> str:
        """Render the bounded follow-up result without re-rendering documents."""

        lines = ['## 行事曆提醒已更新']
        for report in calendar_transform.reports:
            if report.applied:
                lines.append(
                    f'- 使用者提醒「{report.title}」：已加入 {report.applied} 項 CSV 行事曆事件。',
                )
            if report.clarification:
                lines.append(
                    f'> 提醒「{report.title}」尚未加入：{report.clarification}',
                )
        if job_plan.outputs.calendar_csv:
            if csv_file is not None:
                lines.append(
                    f'- 更新後行事曆 CSV：{self.exports.download_link(request, csv_file.id, csv_name)}',
                )
            elif csv_error:
                lines.append(f'> 已完成期限選擇，但無法儲存更新後 CSV：{csv_error}')
        preview = calendar_transform.eip_sync_preview
        if preview is not None:
            lines.append(
                f'> EIP 行事曆：已建立 {preview.unique_event_count} 項去重後寫入預覽；'
                '尚未寫入 EIP，請開啟 EIP 工作台連結帳號、預覽並明確確認。',
            )
        if csv_file is not None:
            lines.append(f'\n[EIP 行事曆預覽／確認匯入](/eip?csv={csv_file.id})')
        return '\n'.join(lines)
