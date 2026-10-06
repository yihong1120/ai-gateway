"""Prompt contracts and real graph boundaries; no paid model or remote writes.

Scripted responses test orchestration, not the semantic reliability of a live LLM.
"""
from __future__ import annotations

import itertools
import json
import re
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from langchain_core.messages import HumanMessage

from company_agent.agents.registry import PROMPT
from company_agent.agents.registry import ROLE_PROMPTS
from company_agent.agents.registry import ROLE_TOOLS
from company_agent.config import ModelProfile
from company_agent.config import Settings
from company_agent.runtime.completion import completion_tool
from company_agent.runtime.execution import DocumentAgent
from company_agent.runtime.prompts import capability_guidance
from company_agent.runtime.prompts import model_messages
from company_agent.tests import all_tools
from company_agent.tests.test_agent import call
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_enterprise_completion import finish
from company_agent.tests.test_multi_agent import RawRoleModel


class RolePromptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tools = {
            tool.name: tool for tool in [
                *all_tools(), completion_tool('coordinator'), completion_tool('document'),
            ]
        }

    def context(self, compact=False):
        settings = Settings(
            state_dir='/tmp/prompt-test-unused', compact_prompts=compact,
            require_artifact_review=True,
            enable_cad=True, enable_ocr=True, enable_spreadsheet_reports=True,
            enable_eip_read=True, enable_vision=True, enable_eip_processing=True,
            enable_web_research=True,
        )
        return SimpleNamespace(
            settings=settings, store=SimpleNamespace(files=Mock(return_value=[])),
            owner='alice', job='job',
        )

    def state(self, role):
        messages = [HumanMessage(content='查資料並製作報告，依需求安排各角色。')]
        if role != 'coordinator':
            messages = [
                HumanMessage(
                    content=json.dumps({
                        'user_request': '查資料並製作報告', 'task': '本次只讀取摘要，不產檔。',
                        'selected_context': 'selected source',
                    }),
                ),
            ]
        return {'messages': messages}

    def test_common_policy_has_no_role_identity_or_specialized_tool_commands(self):
        self.assertNotIn('你是', PROMPT)
        tools = set().union(*ROLE_TOOLS.values())
        self.assertFalse(tools & set(re.findall(r'[a-z]+(?:_[a-z]+)+', PROMPT)))
        for policy in ('不可信資料', '等待本人確認', '不自行新增檔案', '不盲目重送', '虛構示例'):
            self.assertIn(policy, PROMPT)

    async def test_all_role_and_mode_combinations_have_one_identity_and_exact_tool_scope(self):
        for role, compact, structured in itertools.product(ROLE_TOOLS, (False, True), (False, True)):
            with self.subTest(role=role, compact=compact, structured=structured):
                ctx = self.context(compact)
                expected = ROLE_TOOLS[role] | (
                    {'complete_job' if role == 'coordinator' else 'complete_assignment'} if structured else set()
                )
                parameters = {field for name in expected for field in self.tools[name].tool_call_schema.model_fields}
                messages = await model_messages(role, self.state(role), ctx, [{'name': 'NAS_SCOPE_CANARY'}], structured, expected, parameters)
                prompt = messages[0].content
                self.assertEqual(prompt.count('你是'), 1)
                self.assertIn(ROLE_PROMPTS[role], prompt)
                self.assertIn(f'本次角色 ID：{role}', prompt)
                line = next(s for s in prompt.splitlines() if s.startswith('本角色工具白名單'))
                self.assertEqual(set(line.split('：', 1)[1].split(', ')), expected)
                if role != 'coordinator':
                    self.assertNotIn('全體能力目錄', prompt)
                    self.assertIn('只完成本次 task', prompt)
                    request = json.loads(messages[1].content)
                    self.assertEqual(request['task'], '本次只讀取摘要，不產檔。')
                if (role == 'coordinator' and structured) or 'artifact_id' in parameters:
                    ctx.store.files.assert_called_once_with('alice', 'job')
                    self.assertIn('伺服器本工作產出清單', prompt)
                else:
                    ctx.store.files.assert_not_called()
                if role in ('research', 'eip'):
                    self.assertNotIn('NAS_SCOPE_CANARY', prompt)
                self.assertIn('結構化完成模式' if structured else '一般回覆完成模式', prompt)
                if role == 'reviewer' and structured:
                    self.assertIn('交回實際讀取的 artifact_ids', prompt)
                    self.assertNotIn('artifact_ids=[]', prompt)

    def test_feature_flags_only_describe_tools_owned_by_the_role(self):
        ctx = self.context()
        expected = {
            'coordinator': 7, 'knowledge': 2, 'document': 2, 'data': 1,
            'research': 1, 'engineering': 3, 'eip': 2, 'reviewer': 3,
        }
        for role, count in expected.items():
            self.assertEqual(capability_guidance(role, ctx.settings, ROLE_TOOLS[role]).count('已啟用'), count, role)
        for name in (
            'enable_cad', 'enable_ocr', 'enable_spreadsheet_reports', 'enable_eip_read',
            'enable_vision', 'enable_eip_processing', 'enable_web_research',
        ):
            setattr(ctx.settings, name, False)
        for role, count in expected.items():
            self.assertEqual(capability_guidance(role, ctx.settings, ROLE_TOOLS[role]).count('未啟用'), count, role)

    async def test_prompt_uses_actual_bound_tools_instead_of_static_role_allowlist(self):
        messages = await model_messages('document', self.state('document'), self.context(), [], False, {'read_file'}, {'root'})
        line = next(s for s in messages[0].content.splitlines() if s.startswith('本角色工具白名單'))
        self.assertEqual(line.split('：', 1)[1], 'read_file')
        self.assertNotIn('本地 OCR：', messages[0].content)

    async def test_renamed_tools_keep_sources_and_model_profiles_from_their_schema(self):
        ctx = self.context()
        ctx.settings.model_profiles = {
            'small': ModelProfile(
                model='configured', description='測試圖片模型', supports_images=True,
            ),
        }
        for original in ('read_file', 'understand_image'):
            with self.subTest(tool=original):
                tool = self.tools[original].model_copy(update={'name': 'renamed_tool'})
                parameters = set(tool.tool_call_schema.model_fields)
                messages = await model_messages(
                    'document', self.state('document'), ctx, [{'name': 'NAS_SCOPE_CANARY'}],
                    False, {tool.name}, parameters,
                )
                self.assertIn('NAS_SCOPE_CANARY', messages[0].content)
                self.assertEqual('輔助分析工具可選用的模型設定' in messages[0].content, original == 'understand_image')

    def test_specialists_do_not_receive_other_domains_operating_recipes(self):
        for role in ('research', 'eip', 'engineering'):
            self.assertNotIn('edit_text_run', ROLE_PROMPTS[role])
            self.assertNotIn('create_spreadsheet_report', ROLE_PROMPTS[role])
        self.assertIn('edit_text_run', ROLE_PROMPTS['document'])
        self.assertIn('edit_spreadsheet', ROLE_PROMPTS['data'])
        self.assertIn('eip_calendar_prepare', ROLE_PROMPTS['eip'])
        self.assertIn('create_cad', ROLE_PROMPTS['engineering'])

    def test_eip_document_links_are_required_in_both_handoffs(self):
        self.assertIn('view_link', ROLE_PROMPTS['eip'])
        self.assertIn('view_link', ROLE_PROMPTS['coordinator'])


class RoleBoundaryGraphTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def setUp(self):
        super().setUp()
        self.config.structured_completion = True
        self.config.require_artifact_review = True

    async def test_every_role_rejects_another_roles_tool_before_execution(self):
        for role in ROLE_TOOLS:
            with self.subTest(role=role):
                tool, args = (
                    'eip_query', {'folder': 'inbox', 'start': '2026-09-01', 'end': '2026-09-02'},
                ) if role == 'document' else ('create_text', {'name': 'forbidden.txt', 'content': 'forbidden'})
                self.assertNotIn(tool, ROLE_TOOLS[role])

                def acknowledge(messages):
                    self.assertEqual(messages[-1].status, 'error')
                    self.assertEqual(messages[-1].name, tool)
                    return finish('complete_job' if role == 'coordinator' else 'complete_assignment', 'blocked')
                steps = [call(tool, args, 'forbidden'), acknowledge]
                scripts = {role: steps} if role == 'coordinator' else {
                    'coordinator': [call('delegate_' + role, {'task': '只讀取摘要，不產檔'}, 'handoff'), finish(outcome='blocked')], role: steps,
                }
                model = RawRoleModel(**scripts)
                result = await DocumentAgent(self.config, model).run('alice', '只讀取摘要，不要產檔或操作 EIP。')
                self.assertEqual(result['status'], 'blocked', result)
                self.assertEqual([r['tool'] for r in result['tool_feedback']], [tool])
                self.assertFalse(result['files'])
                self.assertFalse(list(self.root.iterdir()))

    async def test_specialists_cannot_delegate_or_promote_themselves(self):
        for role in ROLE_TOOLS.keys() - {'coordinator'}:
            with self.subTest(role=role):
                model = RawRoleModel(
                    **{
                        'coordinator': [call('delegate_' + role, {'task': '只讀摘要'}, 'handoff'), finish(outcome='blocked')],
                        role: [call('delegate_document', {'task': '自行接手建立報告'}, 'forbidden'), finish('complete_assignment', 'blocked')],
                    },
                )
                result = await DocumentAgent(self.config, model).run('alice', '只讀摘要')
                self.assertEqual(result['status'], 'blocked', result)
                self.assertEqual([r['tool'] for r in result['tool_feedback']], ['delegate_document'])
                self.assertEqual(model.invocations, ['coordinator', role, role, 'coordinator'])
                self.assertFalse(result['files'])

    async def test_document_read_only_assignment_returns_without_creating_or_reviewing_files(self):
        self.file('source.txt', '來源內容：本週已完成路基檢查。'.encode())

        def read(messages):
            self.assertIn('不自行建立 Word', messages[0].content)
            request = json.loads(messages[1].content)
            self.assertEqual(request['task'], '只閱讀 source.txt 並摘要，不產檔。')
            return call('read_file', {'root': 'project', 'path': 'source.txt'}, 'read')
        model = RawRoleModel(
            coordinator=[
                call('delegate_document', {'task': '只閱讀 source.txt 並摘要，不產檔。'}, 'handoff'),
                finish(summary='來源顯示本週已完成路基檢查。'),
            ],
            document=[read, finish('complete_assignment', summary='已閱讀：本週完成路基檢查。')],
        )
        result = await DocumentAgent(self.config, model).run('alice', '閱讀 source.txt，直接摘要，不要製作文件。')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(result['files'], [])
        self.assertEqual(len(result['sources']), 1)
        self.assertNotIn('reviewer', model.invocations)
        self.assertEqual((self.root / 'source.txt').read_text(), '來源內容：本週已完成路基檢查。')
