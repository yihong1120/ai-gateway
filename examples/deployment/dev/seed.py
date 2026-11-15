"""Install repository Functions into the empty dev database, without prod content."""
import asyncio
import json
import secrets
import sys
from pathlib import Path
from importlib.resources import files
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from examples.deployment.dev.run import load_environment, CONFIG


async def main():
    load_environment('webui')
    from open_webui.models.auths import Auths
    from open_webui.models.users import Users
    from open_webui.models.functions import Functions, FunctionForm, FunctionMeta
    from open_webui.models.models import Models, ModelForm, ModelMeta, ModelParams
    from open_webui.utils.auth import get_password_hash
    operator = json.loads((CONFIG / 'operator.json').read_text())
    user = await Users.get_user_by_email(operator['email'])
    if not user:
        user = await Auths.insert_new_auth(email=operator['email'], name=operator['name'],
            password=await get_password_hash(secrets.token_hex(32)), role='admin',
            oauth={'oidc': {'sub': operator['id']}})
    if not user or user.oauth.get('oidc', {}).get('sub') != operator['id'] or user.role != 'admin':
        raise RuntimeError('Unexpected dev operator identity.')
    sources = [
        ('company_work_assistant', 'company_agent_pipe.py', '公司工作助理 DEV', 'pipe'),
        ('official_document_processor', 'official_document_processor_pipe.py', '公文處理 DEV', 'pipe'),
        ('my_daily_usage', 'my_daily_usage_pipe.py', '我的今日額度 DEV', 'pipe'),
        ('keycloak_subject_budget', 'keycloak_subject_budget_filter.py', 'DEV 身分與額度', 'filter'),
    ]
    for key, filename, name, kind in sources:
        form = FunctionForm(id=key, name=name, content=files('examples.openwebui.functions').joinpath(filename).read_text(encoding='utf-8'),
                            meta=FunctionMeta(description='獨立 DEV 資料與額度'))
        old = await Functions.get_function_by_id(key)
        if old:
            await Functions.update_function_by_id(key, form.model_dump(exclude={'id'}))
        else:
            await Functions.insert_new_function(user.id, kind, form)
        await Functions.update_function_by_id(key, {'is_active': True, 'is_global': kind == 'filter'})
        if kind == 'pipe':
            model = ModelForm(id=key, name=name, params=ModelParams(),
                meta=ModelMeta(capabilities={'file_context': False, 'vision': True}),
                access_grants=[{'principal_type': 'user', 'principal_id': '*', 'permission': 'read'}])
            if await Models.get_model_by_id(key):
                await Models.update_model_by_id(key, model)
            else:
                await Models.insert_new_model(model, user.id)
    from examples.deployment.dev.persona import install_personas
    await install_personas()
    print('Dev operator and four repository Functions installed; production chats/files were not copied.')


if __name__ == '__main__':
    asyncio.run(main())
