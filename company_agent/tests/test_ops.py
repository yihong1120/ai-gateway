"""Deployment uses mocked WebUI stores, never the live configuration database."""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

from company_agent.ops.deploy_webui import deploy
from company_agent.ops.deploy_webui import ENTRIES


class Form(SimpleNamespace):
    def model_dump(self, exclude=()):
        return {key: value for key, value in vars(self).items() if key not in exclude}


class DeploymentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.functions = SimpleNamespace(
            get_function_by_id=AsyncMock(return_value=Form(id='company_work_assistant', content='old')),
            update_function_by_id=AsyncMock(return_value=Form(is_active=True)),
            insert_new_function=AsyncMock(return_value=Form(is_active=False)),
        )
        self.models = SimpleNamespace(
            get_model_by_id=AsyncMock(return_value=Form(id='company_work_assistant', name='old')),
            update_model_by_id=AsyncMock(return_value=Form(id='company_work_assistant')),
            insert_new_model=AsyncMock(return_value=Form(id='company_work_assistant')),
        )
        self.users = SimpleNamespace(get_first_user=AsyncMock(return_value=Form(id='admin', role='admin')))
        self.banners = [{'id': 'other', 'content': 'keep'}, {'id': 'company-agent-workspace', 'content': 'old'}]
        self.config = SimpleNamespace(get=AsyncMock(return_value=self.banners), upsert=AsyncMock())
        modules = {}
        definitions = {
            'open_webui.models.functions': dict(Functions=self.functions, FunctionForm=Form, FunctionMeta=Form),
            'open_webui.models.models': dict(Models=self.models, ModelForm=Form, ModelMeta=Form, ModelParams=Form),
            'open_webui.models.users': dict(Users=self.users),
            'open_webui.models.config': dict(Config=self.config),
            'open_webui.env': dict(DATA_DIR=self.directory),
        }
        for name, attributes in definitions.items():
            module = ModuleType(name)
            vars(module).update(attributes)
            modules[name] = module
        self.enterContext(patch.dict('sys.modules', modules))
        self.enterContext(patch.dict('os.environ', {}))
        self.enterContext(patch('company_agent.ops.deploy_webui.function_source', return_value='canonical source'))
        self.enterContext(redirect_stdout(StringIO()))

    def backup(self):
        paths = list((self.directory / 'company_agent_deploy_backups').glob('*.json'))
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0].stat().st_mode & 0o777, 0o600)
        return json.loads(paths[0].read_text())

    async def test_update_backs_up_all_components_and_preserves_other_banners(self):
        await deploy()
        saved = self.backup()
        self.assertEqual(saved['function']['content'], 'old')
        self.assertEqual(saved['model']['name'], 'old')
        self.assertEqual(saved['ui.banners'], self.banners)
        updated = self.config.upsert.call_args.args[0]['ui.banners']
        self.assertEqual(updated[0], self.banners[0])
        self.assertEqual({b['id'] for b in updated[1:]}, set(ENTRIES))
        self.assertEqual(len(updated), 4)
        self.functions.insert_new_function.assert_not_awaited()
        self.models.insert_new_model.assert_not_awaited()
        self.functions.update_function_by_id.assert_awaited_with('company_work_assistant', {'is_active': True})

    async def test_first_install_uses_same_single_deployment_path(self):
        self.functions.get_function_by_id.return_value = None
        self.models.get_model_by_id.return_value = None
        await deploy()
        self.functions.insert_new_function.assert_awaited_once()
        self.models.insert_new_model.assert_awaited_once()
        self.assertIsNone(self.backup()['function'])
        form = self.models.insert_new_model.call_args.args[0]
        self.assertEqual(form.access_grants, [{'principal_type': 'user', 'principal_id': '*', 'permission': 'read'}])
        self.config.upsert.assert_awaited_once()

    async def test_backup_failure_prevents_configuration_writes(self):
        with patch('builtins.open', side_effect=OSError('backup failed')):
            with self.assertRaises(OSError):
                await deploy()
        self.functions.update_function_by_id.assert_not_awaited()
        self.models.update_model_by_id.assert_not_awaited()
        self.config.upsert.assert_not_awaited()

    async def test_banner_failure_preserves_backup_and_does_not_activate(self):
        self.config.upsert.side_effect = RuntimeError('banner failed')
        with self.assertRaisesRegex(RuntimeError, 'banner failed'):
            await deploy()
        self.assertEqual(self.backup()['ui.banners'], self.banners)
        self.assertFalse(
            any(
                c.args[1].get('is_active') is True
                for c in self.functions.update_function_by_id.call_args_list
            ),
        )

    async def test_non_admin_does_not_write(self):
        self.users.get_first_user.return_value = Form(id='user', role='user')
        with self.assertRaisesRegex(RuntimeError, '管理員'):
            await deploy()
        self.assertFalse(list(self.directory.iterdir()))
        self.functions.update_function_by_id.assert_not_awaited()
        self.models.update_model_by_id.assert_not_awaited()
        self.config.upsert.assert_not_awaited()
