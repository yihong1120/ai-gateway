"""Codex protocol, credential scope and shared-attribution regressions."""

import json
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
import httpx

from examples.company_ai_access.routers.assets import assets_router
from examples.company_ai_access.routers.control import create_router
from examples.company_ai_access.routers.inference import inference_router
from examples.company_ai_access.services.inference import InferenceProxy
from examples.company_ai_access.schemas import Principal
from examples.company_ai_access.protocols.codex_catalog import candidate, COMPANY_MODELS
from examples.company_ai_access.config import Settings
from examples.company_ai_access.protocols.responses import envelope
from examples.company_ai_access.adapters.upstream import LiteLLM
from tests.test_company_ai_access import Fixture


class CodexAPI(Fixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.catalog_path = Path(self.temp.name) / 'catalog.json'
        self.catalog_path.write_text(json.dumps(candidate(COMPANY_MODELS, 32768)))
        self.settings = replace(
            self.settings,
            models=COMPANY_MODELS,
            codex_models=COMPANY_MODELS,
            codex_release='0.162.0-alpha.17.2',
            codex_catalog_path=self.catalog_path,
        )
        self.service.settings = self.settings
        self.forwarded = []
        self.upstream_status = 200

        def upstream(request):
            self.forwarded.append(request)
            if self.upstream_status != 200:
                return httpx.Response(
                    self.upstream_status,
                    json={
                        'error': {
                            'type': 'budget_exceeded',
                            'message': 'private-provider-detail',
                        }
                    },
                )
            if json.loads(request.content).get('stream'):
                return httpx.Response(
                    200,
                    headers={'content-type': 'text/event-stream'},
                    content=(
                        b'event: response.output_item.added\ndata: {"type":"response.output_item.added","item":{"type":"function_call","call_id":"call_1"}}\n\n'
                        b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_test","status":"completed"}}\n\n'
                    ),
                )
            return httpx.Response(200, json={'id': 'resp_test', 'output': []})

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.addAsyncCleanup(self.http.aclose)
        self.app = FastAPI()
        self.app.state.company_ai_access = self.service
        self.app.state.company_ai_inference = InferenceProxy(self.settings, self.http)
        self.app.include_router(inference_router())
        self.app.include_router(assets_router())
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=('127.0.0.1', 4)),
            base_url=self.settings.origin,
        )
        self.addAsyncCleanup(self.client.aclose)
        self.ready = await self.enroll()
        self.headers = {'Authorization': 'Bearer ' + self.ready['key']}

    async def post(self, **values):
        return await self.client.post(
            '/company-ai/v1/responses',
            headers=self.headers,
            json={'model': COMPANY_MODELS[0], 'input': [], **values},
        )

    async def test_optional_resource_policy_is_shared_public_metadata_without_model_key(self):
        for enabled in (False, True):
            self.service.settings = replace(
                self.settings, issuer='https://identity.example/realms/company',
                mcp_url='https://resources.example/company-mcp' if enabled else '',
                mcp_codex_client='company-codex' if enabled else '',
                mcp_claude_client='company-claude' if enabled else '',
            )
            policy = self.service.policy()['mcp']
            for path in ('/company-ai/v1/models', '/company-ai/v1/codex/config'):
                reply = await self.client.get(path, headers=self.headers)
                self.assertEqual(reply.status_code, 200, reply.text)
                self.assertEqual(reply.json()['mcp'], policy)
                self.assertNotIn(self.ready['key'], reply.text)
            if enabled:
                self.assertEqual(policy['resource_mode'], 'all_owned_bindings')
                self.assertEqual(policy['scope'], 'company.resources')
                self.assertEqual(policy['codex_callback_port'], 18431)
                self.assertEqual(policy['claude_callback_port'], 18432)
            else:
                self.assertIsNone(policy)
            self.assertEqual(self.service.settings.models, COMPANY_MODELS)

    async def test_responses_preserve_tool_ids_outputs_and_streams(self):
        inputs = [
            {'role': 'user', 'content': [{'type': 'input_text', 'text': 'read file'}]},
            {
                'type': 'function_call',
                'call_id': 'call_1',
                'name': 'shell_command',
                'arguments': '{"command":"pwd"}',
            },
            {
                'type': 'function_call_output',
                'call_id': 'call_1',
                'output': '/workspace',
            },
            {
                'type': 'reasoning',
                'encrypted_content': 'opaque-reasoning',
                'summary': [],
            },
            {
                'type': 'custom_tool_call',
                'call_id': 'call_2',
                'name': 'apply_patch',
                'input': 'patch',
            },
            {'type': 'custom_tool_call_output', 'call_id': 'call_2', 'output': 'done'},
        ]
        tools = [
            {
                'type': 'function',
                'name': 'shell_command',
                'parameters': {'type': 'object'},
            },
            {'type': 'custom', 'name': 'apply_patch', 'format': {'type': 'text'}},
        ]
        response = await self.post(input=inputs, tools=tools, stream=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('event: response.completed', response.text)
        self.assertEqual(response.headers['x-accel-buffering'], 'no')
        body = json.loads(self.forwarded[-1].content)
        self.assertEqual(body['input'], inputs)
        self.assertEqual(body['tools'], tools)
        self.assertIs(body['store'], False)
        self.assertEqual(self.forwarded[-1].url.path, '/v1/responses')

    async def test_subject_and_routing_cannot_be_spoofed(self):
        response = await self.post(
            user='bob',
            metadata={'user_id': 'bob'},
            litellm_metadata={'user': 'bob'},
            api_key='attacker',
            api_base='https://evil.example',
            extra_body={'api_base': 'https://evil.example'},
            service_tier='priority',
            prompt_cache_key='other-user',
            safety_identifier='bob',
        )
        self.assertEqual(response.status_code, 200)
        request = self.forwarded[-1]
        body = json.loads(request.content)
        self.assertEqual(body['user'], 'alice')
        self.assertEqual(body['metadata'], {'user_id': 'alice'})
        self.assertEqual(body['litellm_metadata'], {'user': 'alice'})
        self.assertEqual(request.headers['x-litellm-customer-id'], 'alice')
        for name in (
            'api_key',
            'api_base',
            'extra_body',
            'service_tier',
            'prompt_cache_key',
            'safety_identifier',
        ):
            self.assertNotIn(name, body)
        self.assertEqual(request.headers['authorization'], self.headers['Authorization'])

    async def test_claude_and_codex_share_all_three_models(self):
        for model in COMPANY_MODELS:
            self.assertEqual((await self.post(model=model)).status_code, 200)
            response = await self.client.post(
                '/company-ai/v1/messages',
                headers=self.headers,
                json={'model': model, 'messages': []},
            )
            self.assertEqual(response.status_code, 200)
        models = await self.client.get('/company-ai/v1/models', headers=self.headers)
        config = await self.client.get('/company-ai/v1/codex/config', headers=self.headers)
        self.assertEqual([m['id'] for m in models.json()['data']], config.json()['models'])
        self.assertEqual(config.json()['models'], list(COMPANY_MODELS))
        self.assertEqual(config.json()['default_model'], COMPANY_MODELS[0])
        self.assertEqual((await self.post(model='outside-policy')).status_code, 403)

    async def test_catalog_is_authenticated_and_matches_policy(self):
        path = '/company-ai/v1/codex/config'
        self.assertEqual((await self.client.get(path)).status_code, 401)
        response = await self.client.get(path, headers=self.headers)
        self.assertEqual(response.json()['catalog'], candidate(COMPANY_MODELS, 32768))
        self.assertEqual(response.json()['release'], self.settings.codex_release)
        self.assertNotIn(self.ready['key'], response.text)
        self.assertEqual(self.service.policy()['codex_models'], list(COMPANY_MODELS))

    async def test_install_policy_exposes_only_installer_capability(self):
        for enabled in (True, False):
            if not enabled:
                self.service.settings = replace(
                    self.settings,
                    codex_models=(),
                    codex_release='',
                    codex_catalog_path=None,
                )
            response = await self.client.get('/company-ai/v1/codex/install-policy')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json(),
                {
                    'allowed': True,
                    'codex_enabled': enabled,
                    'codex_release': self.settings.codex_release if enabled else None,
                },
            )
            self.assertNotIn(self.ready['key'], response.text)

    async def test_install_policy_retains_private_network_and_query_gates(self):
        path = '/company-ai/v1/codex/install-policy'
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=('203.0.113.9', 4)),
            base_url=self.settings.origin,
        ) as public:
            denied = await public.get(path)
        self.assertEqual(denied.status_code, 403)
        self.assertNotIn('codex_release', denied.text)
        response = await self.client.get(path + '?api_key=untrusted')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            (await self.client.get('/company-ai/network-access')).json(),
            {'allowed': True},
        )

    async def test_native_start_reports_codex_readiness_without_issuing_a_key(self):
        self.app.include_router(create_router(lambda: Principal('alice')))
        created_before = len(self.llm.created)
        for enabled in (True, False):
            if not enabled:
                self.service.settings = replace(
                    self.settings,
                    codex_models=(),
                    codex_release='',
                    codex_catalog_path=None,
                )
            response = await self.client.post(
                '/api/company-ai/device/start',
                json={'device_name': 'WIN-READINESS', 'code_challenge': 'A' * 43},
            )
            self.assertEqual(response.status_code, 200)
            self.assertIs(response.json()['codex_enabled'], enabled)
            self.assertNotIn('key', response.json())
            self.assertNotIn('catalog', response.json())
            self.assertEqual(len(self.llm.created), created_before)
            self.assertEqual(
                (await self.store.get('enrollment', response.json()['request_id']))['status'],
                'pending',
            )

    async def test_disabled_codex_does_not_forward(self):
        self.service.settings = replace(self.settings, codex_models=(), codex_release='', codex_catalog_path=None)
        self.assertEqual((await self.post()).status_code, 503)
        self.assertEqual(
            (await self.client.get('/company-ai/v1/codex/config', headers=self.headers)).status_code,
            503,
        )
        self.assertFalse(self.forwarded)

    async def test_existing_claude_device_requires_new_codex_enrollment(self):
        device = await self.store.get('device', self.ready['device_id'])
        device.pop('codex_enabled')
        await self.store.put('device', device)
        self.assertEqual((await self.post()).status_code, 403)
        response = await self.client.get('/company-ai/v1/codex/config', headers=self.headers)
        self.assertEqual(response.json()['detail']['code'], 'ai_access_codex_enrollment_required')
        # Existing Claude access remains valid while a new enrollment is prepared.
        self.assertEqual(
            (await self.client.get('/company-ai/v1/models', headers=self.headers)).status_code,
            200,
        )
        self.assertFalse(self.forwarded)

    async def test_server_state_hosted_tools_and_remote_content_denied(self):
        cases = [
            {'previous_response_id': 'another-users-response'},
            {'conversation': 'other-conversation'},
            {'store': True},
            {'background': True},
            *(
                {'tools': [{'type': kind}]}
                for kind in (
                    'mcp',
                    'web_search',
                    'file_search',
                    'computer_use_preview',
                    'code_interpreter',
                )
            ),
            {'tool_choice': {'type': 'web_search'}},
            {'input': [{'type': 'item_reference', 'id': 'another-users-item'}]},
            {
                'input': [
                    {
                        'role': 'user',
                        'content': [
                            {
                                'type': 'input_image',
                                'image_url': 'http://127.0.0.1/private',
                            }
                        ],
                    }
                ]
            },
            {
                'input': [
                    {
                        'role': 'user',
                        'content': [{'type': 'input_file', 'file_id': 'other-users-file'}],
                    }
                ]
            },
            {'tools': [{'type': 'namespace', 'tools': [{'type': 'mcp'}]}]},
        ]
        for values in cases:
            with self.subTest(values=values):
                self.assertEqual((await self.post(**values)).status_code, 400)
        self.assertFalse(self.forwarded)

    async def test_query_and_management_scope_and_method(self):
        for path in (
            '/company-ai/v1/responses?api_key=x',
            '/company-ai/v1/responses?beta=true',
            '/company-ai/v1/codex/config?client_version=1',
        ):
            response = (
                await self.client.post(
                    path,
                    headers=self.headers,
                    json={'model': COMPANY_MODELS[0], 'input': []},
                )
                if '/responses' in path
                else await self.client.get(path, headers=self.headers)
            )
            self.assertEqual(response.status_code, 400)
        for method, path in [
            ('get', '/company-ai/v1/responses/resp_other'),
            ('delete', '/company-ai/v1/responses/resp_other'),
            ('post', '/company-ai/v1/responses/compact'),
            ('post', '/company-ai/v1/chat/completions'),
        ]:
            self.assertEqual(
                (await self.client.request(method, path, headers=self.headers)).status_code,
                404,
            )
        self.assertFalse(self.forwarded)

    async def test_errors_retain_budget_status_and_hide_provider_details(self):
        for status in (400, 401, 403, 404, 422, 429, 500):
            self.upstream_status = status
            response = await self.post()
            self.assertEqual(response.status_code, 502 if status == 500 else status)
            self.assertEqual(response.json()['detail']['code'], 'ai_access_budget_exceeded')
            self.assertNotIn('private-provider-detail', response.text)

    async def test_revocation_expiry_and_network_apply_to_both_clients(self):
        await self.service.revoke('alice', self.ready['device_id'])
        self.assertEqual((await self.post()).status_code, 401)
        self.assertEqual(
            (await self.client.get('/company-ai/v1/codex/config', headers=self.headers)).status_code,
            401,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=('203.0.113.1', 4)),
            base_url=self.settings.origin,
        ) as client:
            response = await client.post(
                '/company-ai/v1/responses',
                headers={**self.headers, 'x-forwarded-for': '127.0.0.1'},
                json={},
            )
            self.assertEqual(response.status_code, 403)
        self.assertFalse(self.forwarded)

    async def test_launcher_download_is_exact_and_unknown_files_denied(self):
        path = '/company-ai/downloads/Start-CompanyCodex.ps1'
        response = await self.client.get(path)
        self.assertEqual(
            response.content,
            (Path(__file__).resolve().parents[1] / 'examples/company_ai_access/windows/Start-CompanyCodex.ps1').read_bytes(),
        )
        self.assertEqual((await self.client.get('/company-ai/downloads/other.ps1')).status_code, 404)

    async def test_virtual_key_is_union_of_models_and_exact_routes(self):
        calls = []

        def upstream(request):
            calls.append(request)
            return httpx.Response(404, json={}) if request.method == 'GET' else httpx.Response(200, json={})

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            await LiteLLM(self.settings, client).ensure_key(
                {'id': 'device', 'subject': 'alice', 'expires_at': 2000000000},
                'sk-test',
            )
        body = json.loads(calls[-1].content)
        self.assertEqual(body['models'], list(COMPANY_MODELS))
        self.assertEqual(
            body['allowed_routes'],
            [
                '/v1/messages',
                '/v1/messages/count_tokens',
                '/v1/models',
                '/v1/responses',
            ],
        )
        self.assertNotIn('key_type', body)

    async def test_reconciliation_rejects_existing_key_with_wrong_routes(self):
        device = {'id': 'device', 'subject': 'alice', 'expires_at': 2000000000}
        info = {
            'key_alias': 'company-ai-test-device',
            'metadata': {'company_ai_device': 'device', 'company_ai_subject': 'alice'},
            'models': list(self.settings.all_models),
            'allowed_routes': ['/v1/models'],
        }
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'info': info}))
        ) as client:
            with self.assertRaises(HTTPException):
                await LiteLLM(self.settings, client).ensure_key(device, 'sk-test')

    async def test_catalog_configuration_rejects_unqualified_or_mismatched_policy(self):
        for values in (
            {'codex_release': ''},
            {'codex_catalog_path': None},
            {'codex_models': ('missing',)},
            {'codex_models': ('coding-test', 'coding-test')},
            {'codex_release': 'latest'},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                replace(self.settings, **values)
        catalog = candidate(COMPANY_MODELS, 32768)
        catalog['models'][0]['upgrade'] = {'model': 'outside-gateway'}
        self.catalog_path.write_text(json.dumps(catalog))
        with self.assertRaises(ValueError):
            replace(self.settings)

    async def test_codex_inherits_the_complete_claude_policy_without_duplicate_config(
        self,
    ):
        settings = replace(self.settings, codex_models=())
        self.assertEqual(settings.codex_models, settings.models)
        for models in (COMPANY_MODELS[:1], tuple(reversed(COMPANY_MODELS))):
            with self.assertRaisesRegex(ValueError, 'same ordered model list'):
                replace(settings, codex_models=models)

    async def test_environment_uses_the_claude_list_and_rejects_a_legacy_subset(self):
        env = {
            'PYTHON_DOTENV_DISABLED': '1',
            'AI_ACCESS_DATABASE_URL': __import__('os').environ['AI_ACCESS_DATABASE_URL'],
            'AI_ACCESS_ORIGIN': self.settings.origin,
            'AI_ACCESS_STATE_DIR': str(self.settings.state_dir),
            'AI_ACCESS_LITELLM_URL': self.settings.litellm_url,
            'AI_ACCESS_MASTER_KEY': 'synthetic-test-master',
            'AI_ACCESS_MODELS': ','.join(COMPANY_MODELS),
            'AI_ACCESS_CODEX_RELEASE': self.settings.codex_release,
            'AI_ACCESS_CODEX_CATALOG': str(self.catalog_path),
            'AI_ACCESS_ISSUER': 'https://idp.example/realms/test',
            'AI_ACCESS_DIRECTORY_CLIENT': 'readonly',
            'AI_ACCESS_DIRECTORY_SECRET': 'synthetic-test-secret',
            'AI_ACCESS_ROLE_CLIENT_UUID': 'roles',
        }
        with patch.dict('os.environ', env, clear=True):
            self.assertEqual(Settings.from_env().codex_models, COMPANY_MODELS)
            with patch.dict('os.environ', {'AI_ACCESS_CODEX_MODELS': COMPANY_MODELS[1]}):
                with self.assertRaisesRegex(ValueError, 'same ordered model list'):
                    Settings.from_env()

    async def test_shipped_catalog_matches_the_company_model_policy(self):
        root = Path(__file__).resolve().parents[1]
        catalog_path = root / 'examples/company_ai_access/codex-models.example.json'
        self.assertEqual(json.loads(catalog_path.read_text()), candidate(COMPANY_MODELS, 32768))
        settings = replace(
            self.settings,
            codex_models=(),
            models=COMPANY_MODELS,
            codex_catalog_path=catalog_path,
        )
        self.assertEqual(settings.codex_models, settings.models)


class Envelope(unittest.TestCase):
    def test_output_budget_defaults_and_explicit_limit(self):
        for value in ({'input': []}, {'input': [], 'max_output_tokens': None}):
            self.assertEqual(envelope(value)['max_output_tokens'], 8192)
        self.assertEqual(
            envelope({'input': [], 'max_output_tokens': 4096})['max_output_tokens'],
            4096,
        )

    def test_local_namespaces_and_inline_images(self):
        value = {
            'input': [
                {
                    'role': 'user',
                    'content': [
                        {
                            'type': 'input_image',
                            'image_url': 'data:image/png;base64,YQ==',
                        }
                    ],
                }
            ],
            'tools': [
                {
                    'type': 'namespace',
                    'name': 'functions',
                    'tools': [{'type': 'function', 'name': 'shell'}],
                }
            ],
        }
        self.assertEqual(envelope(value)['input'], value['input'])
