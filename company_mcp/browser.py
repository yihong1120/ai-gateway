"""Same-origin, browser-only resource permissions and paid-operation approval."""
from __future__ import annotations

import re
from importlib.resources import files

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import Response
from open_webui.utils.auth import get_verified_user as active

from .authentication import Principal
from .storage import ResourceError
from examples.shared.runtime.browser import data
from examples.shared.runtime.browser import PrivateRoute
from examples.shared.runtime.io import run_sync
from examples.shared.runtime.usage import verified_subject


router = APIRouter(route_class=PrivateRoute)


def configured(request):
    state = getattr(request.app.state, 'company_mcp', None)
    if state is None:
        raise HTTPException(503, '公司 MCP 尚未配置。')
    return state[0], state[1]()


def owner(user):
    if user.role not in {'user', 'admin'}:
        raise HTTPException(403, '需要已啟用的本人帳號。')
    try:
        subject = verified_subject({'id': user.id, 'role': user.role, 'oauth': getattr(user, 'oauth', None)})
    except ValueError:
        raise HTTPException(403, '請先使用公司 OIDC 帳號登入。') from None
    return user.id, subject


def identifier(value):
    if not re.fullmatch(r'[a-f0-9]{32}', value):
        raise HTTPException(404, '操作不存在。')
    return value


async def browser_data(request):
    # Resource bearer tokens and model/API keys cannot approve their own work.
    # These operations use the host's same-origin browser session exclusively.
    if request.headers.get('authorization'):
        raise HTTPException(403, '請以本人網站登入狀態確認操作。')
    return await data(request)


@router.get('/agents/mcp/status')
async def status(request: Request, user=Depends(active)):
    owner(user)
    if not getattr(request.app.state, 'company_mcp', None):
        return {'enabled': False, 'clients': []}
    configuration, resources = configured(request)
    return {
        'enabled': True, 'resource_mode': 'all_owned_bindings',
        'clients': await run_sync(resources.repository.grants, user.id, configuration.clients),
    }


@router.post('/agents/mcp/access')
async def access(request: Request, user=Depends(active)):
    owner(user)
    configuration, resources = configured(request)
    value = await browser_data(request)
    if (
        set(value) != {'client_id', 'enabled'} or value['client_id'] not in configuration.clients
        or type(value['enabled']) is not bool
    ):
        raise HTTPException(422, '請選擇有效的公司工具及存取狀態。')
    await run_sync(resources.repository.set_grant, user.id, value['client_id'], value['enabled'])
    return {'updated': True}


@router.get('/agents/mcp')
@router.get('/agents/mcp/app.js')
async def page(request: Request, user=Depends(active)):
    owner(user)
    script = request.url.path.endswith('.js')
    response = Response(
        files('company_mcp.ui').joinpath('mcp.js' if script else 'mcp.html').read_bytes(),
        media_type='application/javascript' if script else 'text/html',
    )
    if not script:
        response.headers['Content-Security-Policy'] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        )
    return response


@router.get('/agents/mcp/operations/{key}')
async def operation(key: str, request: Request, user=Depends(active)):
    owner(user)
    configuration, resources = configured(request)
    try:
        value = await run_sync(resources.repository.operation, user.id, identifier(key))
    except ResourceError:
        raise HTTPException(404, '操作不存在或沒有權限。') from None
    return {
        'operation': key, 'client_id': value['client'], 'instruction': value['payload']['instruction'],
        'attachments': len(value['payload']['handles']), 'model_profile': value['payload']['model_profile'],
        'status': 'submitted' if value['web_job'] else 'awaiting_personal_confirmation',
        'notice': '確認後會使用本人既有模型額度；相同文件與判讀條件沿用持久分析。尚未寫入行事曆。',
    }


@router.post('/agents/mcp/operations/{key}/start')
async def start(key: str, request: Request, user=Depends(active)):
    user_id, subject = owner(user)
    _configuration, resources = configured(request)
    value = await browser_data(request)
    if value != {'confirm': True} or type(value.get('confirm')) is not bool:
        raise HTTPException(422, '請本人確認此分析。')
    try:
        operation = await run_sync(resources.repository.operation, user_id, identifier(key))
        if operation['web_job']:
            return {'job': operation['web_job'], 'status': 'submitted', 'workspace_url': '/agents'}
        person = Principal(user_id, subject, operation['client'])
        job = await resources.confirm_analysis(person, key)
    except ResourceError as failure:
        raise HTTPException(409, {'code': failure.code, 'message': str(failure)}) from None
    except ValueError:
        raise HTTPException(409, '來源或綁定已變更，請建立新工作。') from None
    return {'job': job, 'status': 'submitted', 'workspace_url': '/agents'}
