"""Install the local Gemma friend model into isolated dev only."""
from __future__ import annotations

import asyncio
import json
import secrets
import sys
import uuid
from pathlib import Path

import yaml

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from examples.deployment.dev.run import CONFIG, load_environment

PERSONAS = Path(__file__).resolve().parent / 'personas'


async def install_personas():
    load_environment('webui')  # Validates the independent dev DB/data paths.
    definition = json.loads((PERSONAS / 'love_brain_friend.json').read_text())
    gateway = yaml.safe_load((CONFIG / 'litellm.yaml').read_text())
    route = next((entry for entry in gateway['model_list']
                  if entry['model_name'] == definition['base_model_id']), None)
    if route is None or any(route['litellm_params'].get(name) != 0
                            for name in ('input_cost_per_token', 'output_cost_per_token')):
        raise RuntimeError('Configure the zero-cost local base model with dev setup before installing its persona.')
    from open_webui.models.auths import Auth, Auths
    from open_webui.models.models import ModelForm, ModelMeta, ModelParams, Models
    from open_webui.models.users import Users
    user = await Users.get_first_user()
    if not user or user.role != 'admin':
        raise RuntimeError('An existing dev administrator is required.')
    # The current WebUI checks credential/session revocation even for OIDC.
    # Backfill only the already-provisioned dev operator's missing legacy link;
    # an explicitly disabled credential is never enabled here.
    auth = await Auths.get_auth_by_id(user.id)
    if auth is None:
        operator = json.loads((CONFIG / 'operator.json').read_text())
        if user.email != operator['email'] or user.oauth.get('oidc', {}).get('sub') != operator['id']:
            raise RuntimeError('Refusing to provision a different account.')
        from open_webui.internal.db import get_async_db
        from open_webui.utils.auth import get_password_hash
        password = await get_password_hash(secrets.token_urlsafe(48))
        async with get_async_db() as db:
            db.add(Auth(id=user.id, email=user.email, password=password, active=True,
                        session_stamp=str(uuid.uuid4())))
            await db.commit()
    params = {**definition['params'], 'system': (PERSONAS / definition['prompt_file']).read_text()}
    form = ModelForm(id=definition['id'], name=definition['name'], base_model_id=definition['base_model_id'],
        params=ModelParams(**params), meta=ModelMeta(**definition['meta']), is_active=True,
        access_grants=[{'principal_type': 'user', 'principal_id': '*', 'permission': 'read'}],
        commit_message='DEV local Gemma love-brain friend persona')
    old = await Models.get_model_by_id(form.id)
    if old:
        item = await Models.update_model_by_id(form.id, form)
    else:
        item = await Models.insert_new_model(form, user.id)
    if not item:
        raise RuntimeError('Could not save the dev friend model.')
    print(json.dumps({'model_id': item.id, 'name': item.name, 'base_model_id': item.base_model_id,
                      'environment': 'dev', 'active': item.is_active}, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(install_personas())
