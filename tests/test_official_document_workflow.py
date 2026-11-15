"""Tests for the bounded instruction workflow.

These tests exercise no OCR, Open WebUI storage, LiteLLM, EIP, or network
service.  They ensure that planner output is schema-gated before it can alter
deterministic calendar rows.
"""
from __future__ import annotations

import asyncio
import unittest

from pydantic import ValidationError

from examples.official_document_workflow.application.instruction_planner import current_user_message_from_body
from examples.official_document_workflow.application.instruction_planner import plan_current_instruction
from examples.official_document_workflow.application.instruction_planner import resolve_pending_reminder_choice
from examples.official_document_workflow.application.pending import PendingClarificationScope
from examples.official_document_workflow.application.pending import PendingClarificationStore
from examples.official_document_workflow.domain.calendar.operations import apply_calendar_operations


def _valid_plan(
    *,
    target: str = 'single_verified_deadline',
    offset_days: int = 7,
) -> dict[str, object]:
    return {
        'version': 1,
        'scope': 'current_uploads',
        'outputs': {
            'renamed_pdf': True,
            'searchable_ocr_pdf': True,
            'editable_docx': True,
            'calendar_csv': True,
        },
        'calendar_operations': [
            {
                'operation': 'add_relative_reminder',
                'anchor': 'verified_document_deadline',
                'target': target,
                'offset_days': offset_days,
                'title': '追蹤進度',
                'all_day': True,
            },
        ],
        'eip_calendar': {'request': 'write_after_confirmation'},
        'needs_clarification': False,
        'clarification': None,
    }


def _row(*, date: str = '2026/09/10', reference: str = '文號A') -> dict[str, str]:
    return {
        'Name': '鍾羽雯',
        'Subject': '提送評估報告',
        'Start Date': date,
        'Start Time': '',
        'Start Time(24hr)': '',
        'End Date': date,
        'End Time': '',
        'End Time(24hr)': '',
        'Work Hour': '',
        'All day event': '真',
        'Private': '假',
        'Description': '期限原文：於115年9月10日前提送評估報告。',
        'Priority': '普通',
        'Categories': '',
        'Custom Field1': reference,
        'Custom Field2': '115/08/13',
        'Custom Field3': '',
        'Custom Field4': '',
        'Custom Field5': '',
        'Report Status': '',
        'Completed': '0',
    }


class InstructionPlanningTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_message_uses_default_without_model_call(self) -> None:
        async def must_not_run(_: str) -> dict[str, object]:
            raise AssertionError('planner should not run for an empty message')

        plan = await plan_current_instruction('', must_not_run)
        self.assertTrue(plan.outputs.renamed_pdf)
        self.assertEqual(plan.calendar_operations, [])

    async def test_valid_agent_plan_is_schema_validated(self) -> None:
        received: list[str] = []

        async def planner(message: str) -> dict[str, object]:
            received.append(message)
            return _valid_plan()

        plan = await plan_current_instruction('期限後七天提醒我追蹤', planner)
        self.assertEqual(received, ['期限後七天提醒我追蹤'])
        self.assertEqual(plan.calendar_operations[0].offset_days, 7)
        self.assertEqual(plan.eip_calendar.request, 'write_after_confirmation')

    async def test_extra_agent_field_is_rejected(self) -> None:
        async def planner(_: str) -> dict[str, object]:
            payload = _valid_plan()
            payload['tool_call'] = 'rm -rf /'
            return payload

        with self.assertRaises(ValidationError):
            await plan_current_instruction('處理並做其他事', planner)

    async def test_planner_rejects_coerced_protocol_version(self) -> None:
        for invalid_version in (True, '1', 1.0):
            with self.subTest(invalid_version=invalid_version):
                async def planner(_: str, value: object = invalid_version) -> dict[str, object]:
                    payload = _valid_plan()
                    payload['version'] = value
                    return payload

                with self.assertRaises(ValidationError):
                    await plan_current_instruction('處理', planner)

    async def test_timed_relative_reminder_is_rejected_until_a_time_contract_exists(self) -> None:
        async def planner(_: str) -> dict[str, object]:
            payload = _valid_plan()
            operation = payload['calendar_operations'][0]  # type: ignore[index]
            operation['all_day'] = False  # type: ignore[index]
            return payload

        with self.assertRaises(ValidationError):
            await plan_current_instruction('期限後七天上午提醒', planner)

    async def test_reminder_requires_a_csv_or_eip_preview_destination(self) -> None:
        async def planner(_: str) -> dict[str, object]:
            payload = _valid_plan()
            payload['outputs']['calendar_csv'] = False  # type: ignore[index]
            payload['eip_calendar'] = {'request': 'none'}
            return payload

        with self.assertRaises(ValidationError):
            await plan_current_instruction('期限後七天提醒', planner)

    async def test_non_json_agent_result_is_rejected(self) -> None:
        async def planner(_: str) -> dict[str, object]:
            return []  # type: ignore[return-value]

        with self.assertRaises(ValidationError):
            await plan_current_instruction('處理', planner)


class PendingReminderResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolver_can_select_only_a_server_supplied_deadline_index(self) -> None:
        async def resolver(
            message: str,
            options: tuple[str, ...],
        ) -> dict[str, object]:
            self.assertEqual(message, '第二個')
            self.assertEqual(options, ('2026/09/10', '2026/09/11'))
            return {
                'version': 1,
                'decision': 'selected_verified_deadline',
                'selected_deadline_index': 1,
                'clarification': None,
            }

        resolution = await resolve_pending_reminder_choice(
            '第二個',
            ('2026/09/10', '2026/09/11'),
            resolver,
        )
        self.assertEqual(resolution.selected_deadline_index, 1)

    async def test_resolver_cannot_select_an_out_of_range_deadline(self) -> None:
        async def resolver(
            _message: str,
            _options: tuple[str, ...],
        ) -> dict[str, object]:
            return {
                'version': 1,
                'decision': 'selected_verified_deadline',
                'selected_deadline_index': 7,
                'clarification': None,
            }

        with self.assertRaises(ValueError):
            await resolve_pending_reminder_choice(
                '第八個',
                ('2026/09/10', '2026/09/11'),
                resolver,
            )

    async def test_resolver_rejects_coerced_deadline_indices(self) -> None:
        for invalid_index in (True, False, '1', 1.0):
            with self.subTest(invalid_index=invalid_index):
                async def resolver(
                    _message: str,
                    _options: tuple[str, ...],
                    value: object = invalid_index,
                ) -> dict[str, object]:
                    return {
                        'version': 1,
                        'decision': 'selected_verified_deadline',
                        'selected_deadline_index': value,
                        'clarification': None,
                    }

                with self.assertRaises(ValidationError):
                    await resolve_pending_reminder_choice(
                        '第一個',
                        ('2026/09/10', '2026/09/11'),
                        resolver,
                    )

    async def test_resolver_rejects_coerced_protocol_version(self) -> None:
        async def resolver(
            _message: str,
            _options: tuple[str, ...],
        ) -> dict[str, object]:
            return {
                'version': True,
                'decision': 'all_verified_deadlines',
                'selected_deadline_index': None,
                'clarification': None,
            }

        with self.assertRaises(ValidationError):
            await resolve_pending_reminder_choice(
                '全部',
                ('2026/09/10', '2026/09/11'),
                resolver,
            )


class PlanningFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_failures_propagate_unchanged(self):
        for resolve in (False, True):
            with self.subTest(resolve=resolve):
                failure = RuntimeError('private provider response')

                async def adapter(*_args):
                    raise failure

                with self.assertRaises(RuntimeError) as caught:
                    if resolve:
                        await resolve_pending_reminder_choice('第一個', ('2026/09/10',), adapter)
                    else:
                        await plan_current_instruction('處理', adapter)
                self.assertIs(caught.exception, failure)

    async def test_user_safe_adapter_errors_are_preserved(self):
        for resolve in (False, True):
            with self.subTest(resolve=resolve):
                failure = ValueError('請稍後再試')

                async def adapter(*_args):
                    raise failure

                with self.assertRaises(ValueError) as caught:
                    if resolve:
                        await resolve_pending_reminder_choice('第一個', ('2026/09/10',), adapter)
                    else:
                        await plan_current_instruction('處理', adapter)
                self.assertIs(caught.exception, failure)

    async def test_resolver_non_object_response_is_rejected(self):
        async def resolver(*_args):
            return []

        with self.assertRaises(ValidationError):
            await resolve_pending_reminder_choice('第一個', ('2026/09/10',), resolver)


class MessageIsolationTests(unittest.TestCase):
    def test_only_latest_user_message_is_planned(self) -> None:
        body = {
            'messages': [
                {'role': 'user', 'content': '舊公文：不要輸出 DOCX'},
                {'role': 'assistant', 'content': '完成'},
                {'role': 'user', 'content': '這次只處理新附件'},
            ],
        }
        self.assertEqual(current_user_message_from_body(body), '這次只處理新附件')

    def test_non_text_attachment_blocks_are_not_planner_input(self) -> None:
        body = {
            'messages': [
                {
                    'role': 'user',
                    'content': [
                        {'type': 'input_file', 'file_id': 'previous-file'},
                        {'type': 'input_text', 'text': '新增七天後提醒'},
                    ],
                },
            ],
        }
        self.assertEqual(current_user_message_from_body(body), '新增七天後提醒')


class CalendarOperationTests(unittest.TestCase):
    def _plan(self, *, target: str = 'single_verified_deadline', offset_days: int = 7):
        async def planner(_: str) -> dict[str, object]:
            return _valid_plan(target=target, offset_days=offset_days)

        return asyncio.run(plan_current_instruction('期限後七天提醒我追蹤', planner))

    def test_relative_reminder_uses_verified_date_and_not_handler_name(self) -> None:
        original = _row()
        result = apply_calendar_operations([original], self._plan())
        self.assertEqual(len(result.rows), 2)
        reminder = result.rows[1]
        self.assertEqual(reminder['Start Date'], '2026/09/17')
        self.assertEqual(reminder['End Date'], '2026/09/17')
        self.assertEqual(reminder['Name'], '')
        self.assertEqual(reminder['All day event'], '真')
        self.assertEqual(reminder['Completed'], '0')
        self.assertIn('使用者建立的追蹤提醒', reminder['Description'])
        self.assertEqual(original['Name'], '鍾羽雯')
        self.assertIsNotNone(result.eip_sync_preview)
        self.assertTrue(result.eip_sync_preview.requires_confirmation)  # type: ignore[union-attr]

    def test_ambiguous_single_deadline_request_is_not_applied(self) -> None:
        result = apply_calendar_operations(
            [_row(reference='文號A'), _row(date='2026/09/11', reference='文號B')],
            self._plan(),
        )
        self.assertEqual(len(result.rows), 2)
        self.assertEqual(result.reports[0].applied, 0)
        self.assertIn('2 項', result.reports[0].clarification)

    def test_all_deadlines_target_creates_one_reminder_per_verified_event(self) -> None:
        result = apply_calendar_operations(
            [_row(reference='文號A'), _row(date='2026/09/11', reference='文號B')],
            self._plan(target='all_verified_deadlines'),
        )
        self.assertEqual(len(result.rows), 4)
        self.assertEqual(result.reports[0].applied, 2)

    def test_selected_deadline_index_adds_only_that_verified_event(self) -> None:
        result = apply_calendar_operations(
            [_row(reference='文號A'), _row(date='2026/09/11', reference='文號B')],
            self._plan(),
            selected_deadline_indices={0: 1},
        )
        self.assertEqual(len(result.rows), 3)
        self.assertEqual(result.rows[-1]['Start Date'], '2026/09/18')
        self.assertEqual(result.reports[0].applied, 1)

    def test_date_arithmetic_outside_supported_calendar_range_raises(self):
        with self.assertRaises(OverflowError):
            apply_calendar_operations([_row(date='9999/12/31')], self._plan(offset_days=365))

    def test_planner_rejects_coerced_reminder_offsets(self) -> None:
        for invalid_offset in (True, False, '7', 7.0):
            with self.subTest(invalid_offset=invalid_offset):
                async def planner(_: str, value: object = invalid_offset) -> dict[str, object]:
                    return _valid_plan(offset_days=value)  # type: ignore[arg-type]

                with self.assertRaises(ValidationError):
                    asyncio.run(plan_current_instruction('期限後七天提醒', planner))


class PendingClarificationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1_000.0
        self.store: PendingClarificationStore[dict[str, str]] = PendingClarificationStore(
            clock=lambda: self.now,
        )
        self.scope = PendingClarificationScope(
            user_id='user-a',
            chat_id='chat-a',
        )

    def test_record_is_bound_to_user_chat_and_attachment_fingerprint(self) -> None:
        self.store.put(
            self.scope,
            attachment_fingerprint='sha256:attachment-a',
            payload={'state': 'pending'},
            ttl_seconds=60,
        )
        other_user = PendingClarificationScope(
            user_id='user-b',
            chat_id='chat-a',
        )
        other_chat = PendingClarificationScope(
            user_id='user-a',
            chat_id='chat-b',
        )
        self.assertIsNone(
            self.store.claim(other_user, attachment_fingerprint='sha256:attachment-a'),
        )
        self.assertIsNone(
            self.store.claim(other_chat, attachment_fingerprint='sha256:attachment-a'),
        )
        self.assertIsNone(
            self.store.claim(self.scope, attachment_fingerprint='sha256:another-attachment'),
        )
        self.assertEqual(len(self.store), 1)
        record = self.store.claim(self.scope, attachment_fingerprint='sha256:attachment-a')
        self.assertIsNotNone(record)
        self.assertEqual(record.payload, {'state': 'pending'})  # type: ignore[union-attr]
        self.assertEqual(len(self.store), 0)

    def test_expiry_and_restore_keep_the_original_deadline(self) -> None:
        record = self.store.put(
            self.scope,
            attachment_fingerprint='sha256:attachment-a',
            payload={'state': 'pending'},
            ttl_seconds=10,
        )
        claimed = self.store.claim(self.scope, attachment_fingerprint='sha256:attachment-a')
        self.assertIs(claimed, record)
        self.assertTrue(self.store.restore(record))
        self.now += 11
        self.assertIsNone(self.store.get(self.scope))
        self.assertFalse(self.store.restore(record))

    def test_new_scope_is_evicted_when_the_small_bounded_store_is_full(self) -> None:
        store: PendingClarificationStore[str] = PendingClarificationStore(
            max_entries=1,
            clock=lambda: self.now,
        )
        first = PendingClarificationScope('user-a', 'chat-a')
        second = PendingClarificationScope('user-b', 'chat-b')
        store.put(first, attachment_fingerprint='one', payload='first', ttl_seconds=120)
        store.put(second, attachment_fingerprint='two', payload='second', ttl_seconds=60)
        self.assertIsNone(store.get(first))
        self.assertEqual(store.get(second).payload, 'second')  # type: ignore[union-attr]

    def test_restore_at_capacity_evicts_stored_record_and_its_marker(self) -> None:
        store = PendingClarificationStore(max_entries=1, clock=lambda: self.now)
        other = PendingClarificationScope('user-b', 'chat-b')
        original = store.put(self.scope, attachment_fingerprint='one', payload='original', ttl_seconds=120)
        self.assertIs(store.claim(self.scope, attachment_fingerprint='one'), original)
        displaced = store.put(other, attachment_fingerprint='two', payload='other', ttl_seconds=60)
        self.assertTrue(store.restore(original))
        self.assertIs(store.get(self.scope), original)
        self.assertIsNone(store.get(other))
        self.assertFalse(store.restore(displaced))
        self.assertEqual(len(store), 1)

    def test_ttl_must_be_a_finite_positive_duration(self) -> None:
        for ttl in (0, -1, float('inf'), float('nan'), True):
            with self.subTest(ttl=ttl):
                with self.assertRaises(ValueError):
                    self.store.put(
                        self.scope,
                        attachment_fingerprint='sha256:attachment-a',
                        payload={'state': 'pending'},
                        ttl_seconds=ttl,  # type: ignore[arg-type]
                    )

    def test_discard_during_claim_prevents_stale_restore(self) -> None:
        record = self.store.put(
            self.scope,
            attachment_fingerprint='sha256:attachment-a',
            payload={'state': 'pending'},
            ttl_seconds=60,
        )
        claimed = self.store.claim(
            self.scope,
            attachment_fingerprint='sha256:attachment-a',
        )
        self.assertIs(claimed, record)
        # A new upload invalidating this scope must win over a concurrent
        # failed follow-up request trying to put the claimed state back.
        self.assertFalse(self.store.discard(self.scope))
        self.assertFalse(self.store.restore(record))
        self.assertIsNone(self.store.get(self.scope))

    def test_finalize_releases_claim_generation(self) -> None:
        record = self.store.put(
            self.scope,
            attachment_fingerprint='sha256:attachment-a',
            payload={'state': 'pending'},
            ttl_seconds=60,
        )
        claimed = self.store.claim(
            self.scope,
            attachment_fingerprint='sha256:attachment-a',
        )
        self.assertIs(claimed, record)
        self.assertTrue(self.store.finalize(record))
        self.assertFalse(self.store.finalize(record))

    def test_generation_markers_remain_bounded_under_abandoned_claims(self) -> None:
        store: PendingClarificationStore[str] = PendingClarificationStore(
            max_entries=1,
            clock=lambda: self.now,
        )
        for index in range(6):
            scope = PendingClarificationScope(f"user-{index}", f"chat-{index}")
            store.put(
                scope,
                attachment_fingerprint=f"hash-{index}",
                payload='pending',
                ttl_seconds=60,
            )
            self.assertIsNotNone(
                store.claim(scope, attachment_fingerprint=f"hash-{index}"),
            )
        self.assertLessEqual(len(store._generations), 2)  # type: ignore[attr-defined]

    def test_trimmed_marker_cannot_match_a_later_same_scope_record(self) -> None:
        store: PendingClarificationStore[str] = PendingClarificationStore(
            max_entries=1,
            clock=lambda: self.now,
        )
        original = store.put(
            self.scope,
            attachment_fingerprint='original',
            payload='old',
            ttl_seconds=60,
        )
        self.assertIsNotNone(store.claim(self.scope, attachment_fingerprint='original'))
        # Force the original in-flight marker out of the bounded marker set.
        for index in range(3):
            scope = PendingClarificationScope(f"other-{index}", f"chat-{index}")
            store.put(scope, attachment_fingerprint=f"hash-{index}", payload='other', ttl_seconds=60)
            self.assertIsNotNone(store.claim(scope, attachment_fingerprint=f"hash-{index}"))
        replacement = store.put(
            self.scope,
            attachment_fingerprint='replacement',
            payload='new',
            ttl_seconds=60,
        )
        self.assertNotEqual(original.generation, replacement.generation)
        self.assertIsNotNone(store.claim(self.scope, attachment_fingerprint='replacement'))
        self.assertFalse(store.restore(original))


class RetiredOutputTests(unittest.TestCase):
    def test_old_output_preference_is_discarded_without_mutating_saved_plan(self):
        from examples.official_document_workflow.domain.workflow import JobPlan, OutputSelection
        old = {'searchable_ocr_pdf': True, 'editable_docx': False}
        plan = JobPlan.model_validate({'outputs': old})
        self.assertTrue(old['searchable_ocr_pdf'])
        self.assertFalse(plan.outputs.editable_docx)
        self.assertNotIn('searchable_ocr_pdf', plan.model_dump()['outputs'])
        self.assertNotIn('searchable_ocr_pdf', OutputSelection.model_json_schema()['properties'])

    def test_retiring_one_preference_does_not_accept_other_unknown_fields(self):
        from examples.official_document_workflow.domain.workflow import JobPlan
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            JobPlan(outputs={'searchable_ocr_pdf': True, 'upload_url': 'https://invalid.example'})

    def test_shared_clarification_validation_rejects_multiline_and_nul(self):
        from examples.official_document_workflow.domain.workflow import JobPlan, PendingReminderResolution
        from pydantic import ValidationError
        for value in ('第一行\n第二行', '第一行\r第二行', '第一行\x00第二行'):
            for model, fields in (
                (JobPlan, {'needs_clarification': True}),
                (PendingReminderResolution, {'decision': 'needs_clarification'}),
            ):
                with self.subTest(model=model.__name__, value=value):
                    with self.assertRaises(ValidationError):
                        model(clarification=value, **fields)


if __name__ == '__main__':
    unittest.main()
