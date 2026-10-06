from __future__ import annotations

import json
import unittest
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import httpx
from langchain_core.tools import BaseTool

from company_agent.agents.research.tools import research_tools
from company_agent.api.pipe import markdown
from company_agent.runtime.context import Context
from company_agent.runtime.execution import DocumentAgent
from company_agent.services.research import web as research
from company_agent.tests.test_agent import call
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_multi_agent import RawRoleModel

CONFIG = {'key': 'private-api-canary-829', 'count': 3, 'filters': [], 'fetch_filters': []}
URL = 'https://docs.tavily.com/documentation/api-reference/endpoint/search'
ROW = {'url': URL, 'title': 'Tavily Search', 'content': 'Official search documentation'}


class ResearchTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def setUp(self):
        super().setUp()
        self.config.enable_web_research = True
        self.config.structured_completion = True
        self.addCleanup(patch.stopall)
        self.config_mock = patch(
            'company_agent.services.research.web.web_config',
            AsyncMock(return_value=CONFIG),
        ).start()
        self.addresses = patch(
            'company_agent.services.research.web.public_addresses',
            AsyncMock(return_value=['docs.tavily.com', '8.8.8.8']),
        ).start()
        self.request = patch(
            'company_agent.services.research.web.tavily_request', AsyncMock(
                return_value={'results': [ROW], 'usage': {'credits': 1}},
            ),
        ).start()

    def runtime(self, call_id='web-call'):
        return SimpleNamespace(
            context=Context('alice', self.job, self.config, self.store),
            state={}, tool_call_id=call_id, stream_writer=lambda value: None,
        )

    async def invoke(self, name='search_web', args=None, call_id='web-call'):
        tool = next(t for t in research_tools() if t.name == name)
        return await tool.coroutine(runtime=self.runtime(call_id), **(args or {'query': 'Tavily documentation'}))

    async def test_real_graph_handoff_citations_and_typed_completion(self):
        def finish(messages):
            handoff = json.loads(messages[-1].content)
            self.assertEqual(handoff['web_sources'][0]['url'], URL)
            self.assertNotIn(CONFIG['key'], str(messages))
            return call('complete_job', {'outcome': 'completed', 'summary': '已取得官方搜尋文件。', 'artifact_ids': []}, 'done')
        model = RawRoleModel(
            coordinator=[call('delegate_research', {'task': '查詢 Tavily 官方文件'}, 'delegate'), finish],
            research=[
                call('search_web', {'query': 'Tavily documentation', 'count': 3}, 'query'),
                call(
                    'complete_assignment', {
                        'outcome': 'completed',
                        'summary': '官方文件說明搜尋 API。', 'artifact_ids': [],
                    }, 'end',
                ),
            ],
        )
        result = await DocumentAgent(self.config, model).run('alice', '查詢 Tavily 官方文件')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(result['sources'], [])
        self.assertEqual(result['web_sources'][0]['url'], URL)
        self.assertIn('research', result['agent_visits'])
        self.assertNotIn(CONFIG['key'], json.dumps(result))
        self.request.assert_awaited_once()

    async def test_parallel_searches_and_fetch_are_accepted(self):
        from company_agent.tests.test_safe_batches import batch
        self.request.return_value = {'results': [{**ROW, 'raw_content': 'Official page content'}]}
        model = RawRoleModel(
            coordinator=[
                call('delegate_research', {'task': '比較復建工期'}, 'delegate'),
                call('complete_job', {'outcome': 'completed', 'summary': '已比較', 'artifact_ids': []}, 'done'),
            ],
            research=[
                batch(
                    call('search_web', {'query': '地震復建工期'}, 'q1'),
                    call('search_web', {'query': '颱風復建工期'}, 'q2'),
                    call('fetch_url', {'url': URL}, 'page'),
                ),
                call('complete_assignment', {'outcome': 'completed', 'summary': '已查詢', 'artifact_ids': []}, 'end'),
            ],
        )
        result = await DocumentAgent(self.config, model).run('alice', '比較復建工期')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(self.request.await_count, 3)
        self.assertEqual(result['web_sources'][0]['url'], URL)

    async def test_missing_page_does_not_poison_completed_research(self):
        from company_agent.tests.test_safe_batches import batch

        async def provider(endpoint, payload, key):
            return {'results': [] if endpoint == 'extract' else [ROW]}
        self.request.side_effect = provider
        summary = '已依搜尋資料整理；其中一頁無法讀取全文，沒有可靠整體平均。'
        model = RawRoleModel(
            coordinator=[
                call('delegate_research', {'task': '查復建工期'}, 'delegate'),
                call('complete_job', {'outcome': 'completed', 'summary': summary, 'artifact_ids': []}, 'done'),
            ],
            research=[
                batch(*[call('search_web', {'query': f'復建工期 {i}'}, f'q{i}') for i in range(9)]),
                call('fetch_url', {'url': URL}, 'empty'),
                call('complete_assignment', {'outcome': 'completed', 'summary': summary, 'artifact_ids': []}, 'end'),
            ],
        )
        result = await DocumentAgent(self.config, model).run('alice', '查復建工期')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(result['answer'], summary)
        self.assertNotIn('errors', result)
        self.assertEqual(result['delegations'][0]['status'], 'completed')
        self.assertEqual(self.request.await_count, 10)
        self.assertEqual(result['web_sources'][0]['kind'], 'search_snippet')

    async def test_provider_failure_remains_error_and_is_not_replayed(self):
        self.request.side_effect = ValueError('Tavily 回應 HTTP 429')
        with self.assertRaisesRegex(ValueError, '429'):
            await self.invoke()
        with self.assertRaisesRegex(ValueError, '結果不明'):
            await self.invoke()
        self.request.assert_awaited_once()

    async def test_official_tools_hide_runtime_and_key(self):
        for tool in research_tools():
            self.assertIsInstance(tool, BaseTool)
            props = tool.tool_call_schema.model_json_schema()['properties']
            self.assertNotIn('runtime', props)
            self.assertNotIn('key', props)

    async def test_disabled_or_revoked_permission_sends_nothing(self):
        self.config.enable_web_research = False
        with self.assertRaisesRegex(ValueError, '未啟用'):
            await self.invoke()
        self.config.enable_web_research = True
        self.config_mock.side_effect = ValueError('此帳號沒有 Open WebUI 網路搜尋權限。')
        with self.assertRaisesRegex(ValueError, '權限'):
            await self.invoke()
        self.request.assert_not_awaited()

    async def test_search_uses_bounded_basic_and_returns_snippet_provenance(self):
        self.request.return_value = {'results': [{**ROW, 'content': 'a'*1600}], 'usage': {'credits': 1}}
        command = await self.invoke(args={'query': 'test', 'count': 5})
        endpoint, payload, _key = self.request.call_args.args
        self.assertEqual(endpoint, 'search')
        self.assertEqual(payload['max_results'], 3)
        self.assertEqual(payload['search_depth'], 'basic')
        self.assertFalse(payload['auto_parameters'])
        self.assertFalse(payload['include_answer'])
        result = json.loads(command.update['messages'][0].content)
        self.assertEqual(len(result['results'][0]['content']), 1500)
        self.assertTrue(result['results'][0]['truncated'])
        self.assertNotIn('content', command.update['web_sources'][0])

    async def test_extract_empty_is_explicit_no_content_and_cached(self):
        self.request.return_value = {'results': [], 'failed_results': [{'error': 'private provider details'}]}
        for _ in range(2):
            command = await self.invoke('fetch_url', {'url': URL})
            value = json.loads(command.update['messages'][0].content)
            self.assertEqual(value['status'], 'no_content')
            self.assertEqual(value['results'], [])
            self.assertIn('未讀取全文', value['notice'])
            self.assertEqual(command.update['web_sources'], [])
            self.assertNotIn('private provider details', str(value))
        self.request.assert_awaited_once()

    async def test_extract_returns_real_text_with_explicit_truncation(self):
        self.request.return_value = {'results': [{**ROW, 'raw_content': 'x'*13000}], 'usage': {'credits': 1}}
        command = await self.invoke('fetch_url', {'url': URL})
        value = json.loads(command.update['messages'][0].content)
        self.assertEqual(value['results'][0]['kind'], 'page_extract')
        self.assertEqual(len(value['results'][0]['content']), 12000)
        self.assertTrue(value['results'][0]['truncated'])
        self.assertEqual(self.request.call_args.args[1]['urls'], [URL])

    async def test_success_replay_and_more_than_eight_calls(self):
        await self.invoke()
        await self.invoke()
        self.request.assert_awaited_once()
        for index in range(10):
            await self.invoke(call_id=str(index))
        self.assertEqual(self.request.await_count, 11)
        await self.invoke()  # replay still uses the persisted receipt
        self.assertEqual(self.request.await_count, 11)

    async def test_unknown_receipt_not_replayed(self):
        self.store.begin_tool(
            'alice', self.job, 'unknown', 'search_web', {
                'query': 'x', 'count': 5,
            }, limit_names=research.WEB_TOOLS,
        )
        with self.assertRaisesRegex(ValueError, '結果不明'):
            await self.invoke(args={'query': 'x'}, call_id='unknown')
        self.request.assert_not_awaited()

    async def test_blocked_extract_domain_not_sent(self):
        with patch('company_agent.services.research.web.host_allowed', return_value=False):
            with self.assertRaisesRegex(ValueError, '網域'):
                await self.invoke('fetch_url', {'url': URL})
        self.request.assert_not_awaited()

    async def test_empty_search_is_honest_and_extract_upgrade_preserves_kind(self):
        self.request.return_value = {'results': []}
        value = await self.invoke()
        self.assertEqual(value.update['web_sources'], [])
        merged = research.merge_web_sources(
            [{'url': URL, 'kind': 'page_extract'}], [
                {'url': URL, 'kind': 'search_snippet'},
            ],
        )
        self.assertEqual(merged[0]['kind'], 'page_extract')


class URLAndTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_webui_config_enable_engine_key_and_live_permission_checks(self):
        config_module = ModuleType('open_webui.models.config')
        permissions_module = ModuleType('open_webui.utils.access_control')
        constants_module = ModuleType('open_webui.config')
        constants_module.WEB_FETCH_FILTER_LIST = []
        values = {
            'web.search.enable': True, 'web.search.engine': 'tavily', 'web.search.tavily_api_key': 'key-canary',
            'web.search.result_count': 5, 'user.permissions': {'features': {'web_search': True}},
        }
        config_module.Config = SimpleNamespace(get=AsyncMock(side_effect=lambda key: values.get(key)))
        permissions_module.has_permission = AsyncMock(return_value=True)
        with patch.dict(
            'sys.modules', {
                'open_webui.models.config': config_module,
                'open_webui.utils.access_control': permissions_module, 'open_webui.config': constants_module,
            },
        ), \
                patch('company_agent.runtime.context.verified_user', AsyncMock(return_value={'id': 'alice', 'role': 'user'})):
            configured = await research.web_config('alice')
            self.assertEqual(configured['key'], 'key-canary')
            permissions_module.has_permission.assert_awaited_with(
                'alice', 'features.web_search', values['user.permissions'],
            )
            permissions_module.has_permission.return_value = False
            with self.assertRaisesRegex(ValueError, '權限'):
                await research.web_config('alice')
            permissions_module.has_permission.return_value = True
            for key, value, error in [
                ('web.search.enable', False, '啟用'), ('web.search.engine', 'other', '啟用'),
                ('web.search.tavily_api_key', '', '金鑰'),
            ]:
                previous = values[key]
                values[key] = value
                with self.assertRaisesRegex(ValueError, error):
                    await research.web_config('alice')
                values[key] = previous

    def test_private_credentials_and_unsafe_links_rejected(self):
        for url in (
            'javascript:alert(1)', 'http://127.0.0.1', 'http://10.0.0.1/x', 'http://[::1]',
            'http://localhost/x', 'https://nas.local/x', 'https://u:p@example.com/',
            'https://example.com:5001/x', 'https://example.com/?token=secret', 'https://example.com/\nx',
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                research.public_url(url)
        self.assertEqual(research.public_url(URL), URL)

    async def test_private_dns_rejected_before_extract(self):
        loop = __import__('asyncio').get_running_loop()
        with patch.object(loop, 'getaddrinfo', AsyncMock(return_value=[(2, 1, 6, '', ('192.168.1.1', 443))])):
            with self.assertRaises(ValueError):
                await research.public_addresses('https://example.com')

    async def test_http_error_timeout_and_oversize_redacted_no_retries(self):
        real_client = httpx.AsyncClient
        for mode in ('429', 'timeout', 'oversize', 'invalid'):
            calls = []

            def respond(request):
                calls.append(request)
                if mode == 'timeout':
                    raise httpx.ReadTimeout('secret-key-canary', request=request)
                if mode == 'oversize':
                    return httpx.Response(200, content=b'x'*2_000_001)
                if mode == 'invalid':
                    return httpx.Response(200, json={'secret': 'secret-key-canary'})
                return httpx.Response(429, text='secret-key-canary')
            with patch(
                'company_agent.services.research.web.httpx.AsyncClient', side_effect=lambda **kwargs: real_client(
                    transport=httpx.MockTransport(respond), **kwargs,
                ),
            ):
                with self.assertRaises({'429': httpx.HTTPStatusError, 'timeout': httpx.ReadTimeout}.get(mode, ValueError)):
                    await research.tavily_request('search', {'query': 'test'}, 'secret-key-canary')
            self.assertEqual(len(calls), 1)
            self.assertEqual(str(calls[0].url), 'https://api.tavily.com/search')

    def test_pipe_uses_safe_fixed_citation_labels(self):
        result = {
            'id': 'test', 'status': 'done', 'progress': 'done', 'files': [],
            'web_sources': [{'url': 'https://example.com/a_(b)', 'title': '[evil](javascript:x)'}],
        }
        value = markdown(result)
        self.assertIn('https://example.com/a_%28b%29', value)
        self.assertNotIn('javascript', value)
        result['web_sources'].append({'url': 'javascript:alert(1)'})
        with self.assertRaises(ValueError):
            markdown(result)
