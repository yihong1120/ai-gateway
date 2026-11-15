"""Verify producer attribution without charging document users or mixing tasks."""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock
from unittest.mock import patch

import httpx

from company_agent.runtime.models import close_model
from company_agent.runtime.models import gateway_for_verified_user
from examples.shared.runtime.concurrency import model_request
from examples.shared.runtime.concurrency import ModelNotStarted
from examples.shared.runtime.modules import ModuleReference
from examples.shared.runtime.usage import document_subject
from examples.shared.runtime.usage import request_metadata

_fixture_test_official_document_processor_pipe = ModuleReference('tests.test_official_document_processor_pipe')


class UsageAttributionTests(unittest.IsolatedAsyncioTestCase):
    async def test_document_responses_uses_gateway_metadata_without_user_budget(self):
        PIPE_MODULE = _fixture_test_official_document_processor_pipe.attribute('PIPE_MODULE')

        pipe = PIPE_MODULE.Pipe()
        captured = []

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            async def post(self, url, *, json, **kwargs):
                captured.append(json)
                return httpx.Response(200, json={'output_text': '{}'})

        token = document_subject.set('verified-subject')
        try:
            with patch.object(pipe, '_official_document_litellm_connection', return_value=('http://127.0.0.1:4000/v1', 'key')), \
                    patch.object(PIPE_MODULE.httpx, 'AsyncClient', side_effect=lambda **kwargs: Client()):
                await pipe.analyze_text(PIPE_MODULE.CloudSemanticSource('source', 'native_pdf_text'))
        finally:
            document_subject.reset(token)
        self.assertEqual(captured[0]['litellm_metadata']['spend_logs_metadata']['ai_usage_subject'], 'verified-subject')
        self.assertNotIn('user', captured[0])
        self.assertNotIn('metadata', captured[0])

    async def test_concurrent_documents_keep_verified_subjects_and_reset_context(self):
        class Pipe:
            def __init__(self, owner):
                self._usage_owner = owner

            @model_request
            async def request(self):
                await asyncio.sleep(0)
                return request_metadata()

        async def identity(owner):
            return {'id': owner, 'role': 'user', 'oauth': {'oidc': {'sub': 'subject-' + owner}}}

        with patch('examples.identity.adapters.webui.verified_user', new=AsyncMock(side_effect=identity)):
            results = await asyncio.gather(Pipe('alice').request(), Pipe('bob').request())
        self.assertEqual(
            [result['spend_logs_metadata']['ai_usage_subject'] for result in results],
            ['subject-alice', 'subject-bob'],
        )
        self.assertTrue(
            all(
                result['spend_logs_metadata']['ai_usage_source']
                == 'official_document' for result in results
            ),
        )
        self.assertTrue(all('user' not in result for result in results))
        self.assertIsNone(document_subject.get())

    async def test_document_stops_before_model_if_verified_identity_is_invalid(self):
        calls = []

        class Pipe:
            _usage_owner = 'alice'

            @model_request
            async def request(self):
                calls.append('sent')

        for user in ({'id': 'bob'}, {'id': 'alice', 'role': 'user', 'oauth': {}}):
            with patch('examples.identity.adapters.webui.verified_user', new=AsyncMock(return_value=user)):
                with self.assertRaises(ModelNotStarted):
                    await Pipe().request()
        self.assertEqual(calls, [])
        self.assertIsNone(document_subject.get())

    async def test_provider_failure_is_not_misclassified_as_rejected_preflight(self):
        error = ValueError('response rejected after provider execution')

        class Pipe:
            _usage_owner = 'alice'

            @model_request
            async def request(self):
                raise error

        user = {'id': 'alice', 'role': 'user', 'oauth': {'oidc': {'sub': 'alice-subject'}}}
        with patch('examples.identity.adapters.webui.verified_user', AsyncMock(return_value=user)):
            with self.assertRaises(ValueError) as raised:
                await Pipe().request()
        self.assertIs(raised.exception, error)
        self.assertNotIsInstance(raised.exception, ModelNotStarted)
        self.assertIsNone(document_subject.get())

    async def test_company_all_model_paths_keep_user_and_persist_source(self):
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(
                200, json={
                    'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': '{}'}}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12},
                },
            )

        model = gateway_for_verified_user(
            {'id': 'alice', 'role': 'user', 'oauth': {'oidc': {'sub': 'subject-alice'}}},
            'https://gateway.example/v1', 'test-key', 'test-model', httpx.MockTransport(handler),
        )
        self.addAsyncCleanup(close_model, model)
        await model.analyze([('human', 'analysis')])
        await model.summarize('summary')
        await model.bind_tools([]).ainvoke([('human', 'tools')])
        self.assertEqual(len(requests), 3)
        for body in requests:
            self.assertEqual(body['user'], 'subject-alice')
            self.assertEqual(body['metadata']['spend_logs_metadata']['ai_usage_source'], 'company_agent')
            self.assertEqual(body['num_retries'], 0)
