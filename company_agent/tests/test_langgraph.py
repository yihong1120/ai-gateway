from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool
from langsmith import get_tracing_context
from langsmith import tracing_context

import company_agent.services.office.documents as _documents_imports
from company_agent.graph import build_graph
from company_agent.runtime.execution import DocumentAgent
from company_agent.runtime.storage import Store
from company_agent.services.files.tools import file_tools
from company_agent.tests.postgres_fixture import checkpointer
from company_agent.tests.test_agent import call
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_agent import ScriptedModel

"""Real graph/checkpointer tests; only the model is replaced by scripted messages."""


class NativeGraphTests(unittest.IsolatedAsyncioTestCase, Fixture):
    async def pending(self):
        self.file('source.txt', b'private-source-canary-7921')

        def propose(messages):
            artifact = json.loads(messages[-1].content)
            return call('propose_publish', {'artifact_id': artifact['id'], 'root': 'project', 'path': 'out.txt'}, 3)
        result = await DocumentAgent(
            self.config, ScriptedModel([
                call('read_file', {'root': 'project', 'path': 'source.txt'}, 1),
                call('create_text', {'name': 'out.txt', 'content': 'draft-canary-4532'}, 2), propose,
            ]),
        ).run('alice', '建立草稿並存回 project')
        self.assertEqual(result['status'], 'awaiting_approval', result)
        return result

    def decision(self, result, choice='approve'):
        return {'proposal_id': result['proposals'][0]['proposal_id'], 'decision': choice}

    def final_model(self):
        return ScriptedModel([AIMessage(content='操作已完成。')])

    async def test_standard_tools_hide_runtime_and_graph_has_native_nodes(self):
        for tool in file_tools():
            self.assertIsInstance(tool, BaseTool)
            self.assertNotIn('runtime', tool.tool_call_schema.model_json_schema()['properties'])
        async with checkpointer(self.store) as saver:
            graph = build_graph(self.final_model(), saver)
            self.assertTrue({'model', 'tools', 'CompanyPolicy.before_model'} <= set(graph.get_graph().nodes))

    async def test_approval_survives_actual_process_restart(self):
        pending = await self.pending()
        config = self.base/'config.json'
        config.write_text(self.config.model_dump_json())
        script = '''import asyncio,json,sys
from pathlib import Path
from company_agent.runtime.execution import DocumentAgent
from company_agent.config import Settings
from company_agent.tests.test_agent import ScriptedModel
async def main():
    agent=DocumentAgent(Settings.model_validate_json(Path(sys.argv[1]).read_text()),
                        ScriptedModel([{'content':'完成'}]))
    print(json.dumps(await agent.resume('alice',sys.argv[2],json.loads(sys.argv[3]))))
asyncio.run(main())
'''
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-c', script, str(config), pending['job_id'],
            json.dumps(self.decision(pending)), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
        self.assertEqual(process.returncode, 0, stderr.decode())
        result = json.loads(stdout)
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(len(result['sources']), 1)
        self.assertEqual((self.root/'out.txt').read_bytes(), b'draft-canary-4532')
        with self.assertRaises(ValueError):
            await DocumentAgent(self.config, self.final_model()).resume('alice', pending['job_id'], self.decision(pending))
        self.assertEqual(len(self.store.files('alice', pending['job_id'])), 1)

    async def test_reject_keeps_draft_without_external_write(self):
        pending = await self.pending()
        result = await DocumentAgent(self.config, self.final_model()).resume('alice', pending['job_id'], self.decision(pending, 'reject'))
        self.assertEqual(result['status'], 'done', result)
        self.assertFalse((self.root/'out.txt').exists())
        self.assertEqual(len(result['files']), 1)
        self.assertEqual(result['notices'], ['使用者拒絕存檔；草稿保留，不要重新提出相同申請。'])
        self.assertEqual(
            self.store.proposal(
                'alice', pending['job_id'], self.decision(
                    pending,
                )['proposal_id'],
            )['status'], 'rejected',
        )

    async def test_repeated_proposal_does_not_ask_for_rejected_write_again(self):
        pending = await self.pending()
        model = ScriptedModel([
            call(
                'propose_publish', {
                    'artifact_id': pending['files']
                    [0]['id'], 'root': 'project', 'path': 'out.txt',
                }, 4,
            ),
            AIMessage(content='草稿已保留，不存回。'),
        ])
        result = await DocumentAgent(self.config, model).resume('alice', pending['job_id'], self.decision(pending, 'reject'))
        self.assertEqual(result['status'], 'done', result)
        self.assertFalse(result['proposals'])
        self.assertFalse((self.root/'out.txt').exists())
        self.assertEqual(result['notices'], ['使用者拒絕存檔；草稿保留，不要重新提出相同申請。'])

    async def test_wrong_owner_and_invalid_approval_do_not_consume_interrupt(self):
        pending = await self.pending()
        agent = DocumentAgent(self.config, self.final_model())
        with self.assertRaises(ValueError):
            await agent.resume('bob', pending['job_id'], self.decision(pending))
        for invalid in (
            None, {}, True, {'proposal_id': 'wrong', 'decision': 'approve'},
            {**self.decision(pending), 'owner': 'admin'},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                await agent.resume('alice', pending['job_id'], invalid)
        self.assertFalse((self.root/'out.txt').exists())
        self.assertEqual((await agent.resume('alice', pending['job_id'], self.decision(pending)))['status'], 'done')

    async def test_revoked_write_permission_is_rechecked_after_restart(self):
        pending = await self.pending()
        self.config.roots[0].writers = []
        result = await self.failed_job(DocumentAgent(self.config, self.final_model()).resume('alice', pending['job_id'], self.decision(pending)), ValueError)
        self.assertEqual(result['status'], 'error', result)
        self.assertFalse((self.root/'out.txt').exists())

    async def test_checkpoint_and_pending_writes_are_encrypted(self):
        pending = await self.pending()
        key = self.private/'checkpoint.key'
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(key.stat().st_size, 32)
        with self.store.db() as db:
            data = b''.join(
                r[0] for r in db.execute(
                    'SELECT blob FROM checkpoint_blobs WHERE blob IS NOT NULL',
                ).fetchall()
            )
            data += b''.join(
                r[0]
                for r in db.execute('SELECT blob FROM checkpoint_writes WHERE blob IS NOT NULL').fetchall()
            )
        self.assertFalse((self.private/'checkpoints.sqlite3').exists())
        self.assertNotIn(b'private-source-canary-7921', data)
        self.assertNotIn(b'draft-canary-4532', data)
        self.assertTrue(pending['resumable'])

    async def test_lost_checkpoint_key_fails_closed(self):
        pending = await self.pending()
        (self.private/'checkpoint.key').unlink()  # Only this test's disposable key.
        with self.assertRaises(FileNotFoundError):
            await DocumentAgent(self.config, self.final_model()).resume('alice', pending['job_id'], self.decision(pending))
        self.assertFalse((self.root/'out.txt').exists())

    async def test_wrong_key_raises_native_crypto_error_before_publish(self):
        pending = await self.pending()
        (self.private / 'checkpoint.key').write_bytes(b'x' * 32)
        with self.assertRaisesRegex(ValueError, 'MAC check failed'):
            await DocumentAgent(self.config, self.final_model()).resume(
                'alice', pending['job_id'], self.decision(pending),
            )
        self.assertFalse((self.root / 'out.txt').exists())

    async def test_model_error_can_resume_without_recreating_draft(self):
        result = await self.failed_job(
            DocumentAgent(
                self.config, ScriptedModel([
                    call('create_text', {'name': 'draft.txt', 'content': 'saved'}, 1),
                ]),
            ).run('alice', '建立草稿'), RuntimeError,
        )
        self.assertTrue(result['checkpoint'].next)
        ids = [f['id'] for f in result['files']]
        resumed = await DocumentAgent(self.config, self.final_model()).resume('alice', result['job_id'])
        self.assertEqual(resumed['status'], 'done', resumed)
        self.assertEqual([f['id'] for f in resumed['files']], ids)
        self.assertEqual(resumed['model_steps'], 5)  # Handoff, tool, failure, specialist and coordinator answers.

    async def test_resume_does_not_reset_model_attempt_budget(self):
        self.config.max_steps = 2  # Coordinator handoff and document tool call share this budget.
        result = await self.failed_job(
            DocumentAgent(
                self.config, ScriptedModel([
                    call('create_text', {'name': 'draft.txt', 'content': 'saved'}, 1),
                ]),
            ).run('alice', '建立草稿'), ModelCallLimitExceededError,
        )
        model = self.final_model()
        resumed = await self.failed_job(DocumentAgent(self.config, model).resume('alice', result['job_id']), ModelCallLimitExceededError)
        self.assertEqual(resumed['status'], 'error', resumed)
        self.assertFalse(model.messages)
        self.assertEqual(self.store.attempts(resumed['job_id']), 2)

    async def test_revoked_read_permission_prevents_resending_persisted_source(self):
        self.file()
        result = await self.failed_job(
            DocumentAgent(
                self.config, ScriptedModel([
                    call('read_file', {'root': 'project', 'path': 'a.txt'}, 1),
                ]),
            ).run('alice', '讀取文件'), RuntimeError,
        )
        self.config.roots[0].model_access = False
        model = self.final_model()
        resumed = await self.failed_job(DocumentAgent(self.config, model).resume('alice', result['job_id']), ValueError)
        self.assertEqual(resumed['status'], 'error')
        self.assertFalse(model.messages)

    async def test_edit_uses_checkpoint_source_version_and_rechecks_current_file(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                data = _documents_imports.create_word('原標題', [{'text': '原內容', 'heading': False}])
                source = self.file('edit.docx', data)
                run = next(r for b in _documents_imports.extract(data, '.docx') for r in b['runs'])
                stopped = await self.failed_job(
                    DocumentAgent(
                        self.config, ScriptedModel([
                            call('read_file', {'root': 'project', 'path': 'edit.docx'}, 1),
                        ]),
                    ).run('alice', '閱讀並修改文件'), RuntimeError,
                )
                if changed:
                    source.write_bytes(data+b'changed')
                child = next(s for s in stopped['subgraphs'].values() if s.get('sources'))
                args = {
                    'root': 'project', 'path': 'edit.docx', 'sha256': child['sources'][0]['sha256'],
                    'location': run['location'], 'expected': run['text'], 'replacement': '新標題', 'name': 'new.docx',
                }
                operation = DocumentAgent(
                    self.config, ScriptedModel([
                        call('edit_text_run', args, 2), AIMessage(content='處理結果已回傳'),
                    ]),
                ).resume('alice', stopped['job_id'])
                resumed = await self.failed_job(operation, ValueError) if changed else await operation
                self.assertEqual(resumed['status'], 'error' if changed else 'done', resumed)
                self.assertEqual(len(resumed['files']), 0 if changed else 1)

    async def test_native_tool_runtime_overrides_model_supplied_identity(self):
        result = await DocumentAgent(
            self.config, ScriptedModel([
                call('create_text', {'name': 'own.txt', 'content': 'own', 'runtime': {'owner': 'admin'}}, 1),
                AIMessage(content='完成'),
            ]),
        ).run('alice', '建立草稿')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(len(result['files']), 1)
        with self.assertRaises(ValueError):
            self.store.read_artifact('admin', result['files'][0]['id'])

    async def test_repeated_provider_id_across_requests_is_namespaced(self):
        args = {'name': 'draft.txt', 'content': 'saved'}
        result = await DocumentAgent(
            self.config, ScriptedModel([
                call('create_text', args, 1), call('create_text', {'name': 'second.txt', 'content': 'second'}, 1),
                AIMessage(content='完成'),
            ]),
        ).run('alice', '建立草稿')
        self.assertEqual(result['status'], 'done')
        self.assertEqual(len(result['files']), 2)

    async def test_text_mode_preserves_provider_stop_metadata_without_custom_rejection(self):
        result = await DocumentAgent(
            self.config, ScriptedModel([
                AIMessage(content='incomplete', response_metadata={'finish_reason': 'length'}),
            ]),
        ).run('alice', '摘要')
        self.assertEqual(result['status'], 'done', result)
        self.assertIn('incomplete', result['answer'])

    async def test_progress_does_not_leak_source_or_instruction(self):
        events = []
        async def progress(event): events.append(event)
        self.file(data=b'secret-file-canary')
        result = await DocumentAgent(
            self.config, ScriptedModel([
                call('read_file', {'root': 'project', 'path': 'a.txt'}, 1), AIMessage(content='完成'),
            ]),
        ).run('alice', 'secret-instruction-canary', progress)
        self.assertEqual(result['status'], 'done')
        self.assertTrue(events)
        self.assertNotIn('secret-', json.dumps(events))

    async def test_host_wide_tracing_is_not_inherited(self):

        def response(messages):
            self.assertFalse(get_tracing_context()['enabled'])
            return AIMessage(content='完成')
        with tracing_context(enabled=True):
            result = await DocumentAgent(self.config, ScriptedModel([response])).run('alice', '私人任務')
        self.assertEqual(result['status'], 'done', result)

    async def test_same_job_concurrent_resume_is_rejected(self):
        pending = await self.pending()
        with self.store.mutex('job:' + pending['job_id']):
            with self.assertRaisesRegex(ValueError, '正在執行'):
                await DocumentAgent(self.config, self.final_model()).resume('alice', pending['job_id'], self.decision(pending))
        self.assertFalse((self.root/'out.txt').exists())

    async def test_legacy_job_is_readable_but_not_graph_resumable(self):
        artifact = self.store.artifact('alice', self.job, 'old.txt', b'legacy')
        self.assertEqual(self.store.read_artifact('alice', artifact['id'])[2], b'legacy')
        with self.assertRaisesRegex(ValueError, '舊版或不相容'):
            await DocumentAgent(self.config, self.final_model()).resume('alice', self.job)

    async def test_cancellation_preserves_checkpoint_for_explicit_resume(self):
        entered = asyncio.Event()

        class BlockedModel(ScriptedModel):
            async def ainvoke(self, messages):
                entered.set()
                await asyncio.Event().wait()
        agent = DocumentAgent(self.config, BlockedModel([]))
        task = asyncio.create_task(agent.run('alice', '可恢復工作'))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.store.db() as db:
            job = db.execute("SELECT id FROM jobs WHERE status='running' ORDER BY created DESC LIMIT 1").fetchone()[0]
        result = await DocumentAgent(self.config, self.final_model()).resume('alice', job)
        self.assertEqual(result['status'], 'done', result)

    async def test_configured_timeout_propagates_and_releases_job_for_resume(self):
        for eip_enabled in (False, True):
            with self.subTest(eip_enabled=eip_enabled):
                self.config.execution_timeout_seconds = 1
                self.config.enable_eip_processing = eip_enabled

                class SlowModel(ScriptedModel):
                    async def ainvoke(self, messages):
                        await asyncio.sleep(3)
                        raise AssertionError('Configured one-second timeout was not applied')
                saved = await self.failed_job(
                    DocumentAgent(self.config, SlowModel([])).run('alice', '等待模型回覆'), TimeoutError,
                )
                self.assertEqual(saved['status'], 'error')
                self.assertTrue(saved['resumable'])
                self.assertIn('逾時', saved['answer'])
                result = await DocumentAgent(self.config, self.final_model()).resume('alice', saved['job_id'])
                self.assertEqual(result['status'], 'done')


class ReplayTests(Fixture):
    def test_completed_tool_receipt_is_reused_and_unknown_is_not_replayed(self):
        args = {'name': 'draft.txt', 'content': 'x'}
        self.assertIsNone(self.store.begin_tool('alice', self.job, '1', 'create_text', args))
        with self.assertRaises(ValueError):
            self.store.begin_tool('alice', self.job, '1', 'create_text', args)
        self.store.finish_tool('alice', self.job, '1', {'id': 'artifact'})
        self.assertEqual(
            Store(self.private).begin_tool(
                'alice', self.job, '1', 'create_text', args,
            ), {'id': 'artifact'},
        )
        with self.assertRaises(ValueError):
            self.store.begin_tool('alice', self.job, '1', 'create_text', {})

    def test_completed_publish_replay_does_not_touch_filesystem(self):
        artifact = self.store.artifact('alice', self.job, 'out.txt', b'file')
        proposal = self.store.propose('alice', self.job, artifact['id'], 'project', 'out.txt')
        self.store.confirm('alice', proposal['proposal_id'], self.access)
        with patch.object(self.access, 'publish_new', side_effect=AssertionError('duplicate write')):
            result = self.store.confirm('alice', proposal['proposal_id'], self.access, replay_ok=True)
        self.assertTrue(result['already_completed'])
        with self.assertRaises(ValueError):
            self.store.reject('alice', self.job, proposal['proposal_id'])
