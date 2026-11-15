"""Persona installation consumes deployed routes without rewriting snapshots."""

import json
import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import yaml

from examples.deployment.dev import persona


class DevPersonaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.config = Path(temporary)
        self.enterContext(patch.object(persona, 'CONFIG', self.config))
        self.load = self.enterContext(patch.object(persona, 'load_environment'))
        self.definition = json.loads((persona.PERSONAS / 'love_brain_friend.json').read_text())
        self.route = {
            'model_name': self.definition['base_model_id'],
            'litellm_params': {
                'api_base': 'os.environ/LOCAL_MODEL_DEV_API_BASE',
                'input_cost_per_token': 0, 'output_cost_per_token': 0,
                'temperature': 0.19,
            },
        }
        self.snapshot = self.config / 'litellm.yaml'
        self.private = self.config / 'litellm.json'
        self.private.write_text(json.dumps({'LITELLM_MASTER_KEY': 'synthetic-never-load'}))
        self.models = SimpleNamespace(
            get_model_by_id=AsyncMock(return_value=None),
            insert_new_model=AsyncMock(side_effect=lambda form, owner: form),
            update_model_by_id=AsyncMock(),
        )
        self.users = SimpleNamespace(get_first_user=AsyncMock(return_value=SimpleNamespace(id=7, role='admin')))
        self.auths = SimpleNamespace(get_auth_by_id=AsyncMock(return_value=object()))
        model_module = ModuleType('open_webui.models.models')
        model_module.Models = self.models
        model_module.ModelForm = model_module.ModelParams = model_module.ModelMeta = SimpleNamespace
        user_module = ModuleType('open_webui.models.users')
        user_module.Users = self.users
        auth_module = ModuleType('open_webui.models.auths')
        auth_module.Auths, auth_module.Auth = self.auths, SimpleNamespace
        self.enterContext(patch.dict('sys.modules', {
            'open_webui.models.models': model_module,
            'open_webui.models.users': user_module,
            'open_webui.models.auths': auth_module,
        }))
        self.enterContext(patch('builtins.print'))

    async def test_install_preserves_deployed_yaml_and_private_environment(self):
        self.snapshot.write_text(yaml.safe_dump({'model_list': [self.route], 'custom_policy': 'keep'}))
        before = {path.name: path.read_bytes() for path in self.config.iterdir()}
        with patch.dict(os.environ, {}, clear=True):
            await persona.install_personas()
            self.assertNotIn('LITELLM_MASTER_KEY', os.environ)
        self.assertEqual({path.name: path.read_bytes() for path in self.config.iterdir()}, before)
        self.load.assert_called_once_with('webui')
        form, owner = self.models.insert_new_model.call_args.args
        self.assertEqual((form.id, form.base_model_id, owner),
                         (self.definition['id'], self.definition['base_model_id'], 7))

    def test_script_entry_point_does_not_patch_deployment_routes(self):
        from examples.deployment.dev import run

        self.snapshot.write_text(yaml.safe_dump({'model_list': [self.route]}))
        before = {path.name: path.read_bytes() for path in self.config.iterdir()}
        with patch.object(run, 'CONFIG', self.config), patch.object(run, 'load_environment', self.load), \
                patch.object(sys, 'path', list(sys.path)), patch.dict(os.environ, {}, clear=True):
            runpy.run_path(persona.__file__, run_name='__main__')
        self.assertEqual({path.name: path.read_bytes() for path in self.config.iterdir()}, before)
        self.load.assert_called_once_with('webui')
        self.models.insert_new_model.assert_awaited_once()

    async def test_missing_or_billed_route_stops_before_host_changes(self):
        for route in (None, {**self.route, 'litellm_params': {'input_cost_per_token': 1, 'output_cost_per_token': 0}}):
            with self.subTest(route=route):
                self.snapshot.write_text(yaml.safe_dump({'model_list': [route] if route else []}))
                before = self.snapshot.read_bytes()
                with self.assertRaisesRegex(RuntimeError, 'dev setup'):
                    await persona.install_personas()
                self.assertEqual(self.snapshot.read_bytes(), before)
        self.users.get_first_user.assert_not_awaited()
        self.models.insert_new_model.assert_not_awaited()
