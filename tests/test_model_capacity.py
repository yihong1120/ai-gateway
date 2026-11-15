"""Capacity workloads select their own model without real network access."""
from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

from examples.deployment.diagnostics import model_capacity


class Response(io.BytesIO):
    status = 200


class ModelCapacityTests(unittest.TestCase):
    def test_each_workload_model_is_forwarded_and_recorded(self):
        values = {
            'OPENAI_API_BASE_URLS': 'http://gateway.test/v1',
            'OPENAI_API_KEYS': 'synthetic-restricted-key',
        }
        for model in ('first-local-model', 'another-local-model'):
            with self.subTest(model=model):
                opener = Mock()
                event = {
                    'choices': [{'delta': {'content': 'result'}, 'finish_reason': 'stop'}],
                    'usage': {'completion_tokens': 2},
                }
                opener.open.return_value = Response(b'data: ' + json.dumps(event).encode() + b'\n\ndata: [DONE]\n')
                workload = {
                    'profile': 'synthetic', 'model': model, 'requests': [{
                        'label': 'short', 'messages': [{'role': 'user', 'content': 'synthetic'}],
                        'max_tokens': 2, 'input_tokens_tokenizer': 1,
                    }],
                }
                with patch.object(model_capacity, 'read_service', return_value=values), \
                        patch.object(model_capacity.urllib.request, 'build_opener', return_value=opener):
                    report = model_capacity.run(workload, Path('/unused-model-capacity-root'))
                request = opener.open.call_args.args[0]
                body = json.loads(request.data)
                self.assertEqual(request.full_url, 'http://gateway.test/v1/chat/completions')
                self.assertEqual(body['model'], model)
                self.assertTrue(body['user'].startswith('capacity-'))
                self.assertEqual(request.get_header('Authorization'), 'Bearer synthetic-restricted-key')
                self.assertEqual((report['model'], report['successes'], report['failures']), (model, 1, 0))
                self.assertEqual(report['output_tokens_total'], 2)
                self.assertNotIn('synthetic-restricted-key', json.dumps(report))

    def test_missing_model_stops_before_configuration_or_network(self):
        with (
            patch.object(model_capacity, 'read_service') as configuration,
            patch.object(model_capacity.urllib.request, 'build_opener') as transport,
            self.assertRaises(KeyError),
        ):
            model_capacity.run({'profile': 'synthetic', 'requests': []}, Path('/unused'))
        configuration.assert_not_called()
        transport.assert_not_called()
