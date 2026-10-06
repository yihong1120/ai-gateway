"""Keycloak/Visionnaire user synchronization, separate from agent execution."""
from __future__ import annotations

import os
import uuid

import httpx
from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from company_agent.api.http import active
from company_agent.api.http import PrivateRoute

router = APIRouter(route_class=PrivateRoute)


class ManagedUserForm(BaseModel):
    name: str
    email: str
    password: str
    profile_image_url: str | None = '/user.png'
    role: str | None = 'pending'
    username: str | None = None
    group_id: int | None = None
    family_name: str | None = None
    given_name: str | None = None


class ManagedUserUpdate(BaseModel):
    name: str | None = None
    email: str | None = None
    password: str | None = None
    profile_image_url: str | None = None
    role: str | None = None
    group_id: int | None = None


class ManagedPasswordChange(BaseModel):
    password: str
    new_password: str


async def oidc_access_token(request: Request, user) -> str:
    session_id = request.cookies.get('oauth_session_id')
    if not session_id:
        raise HTTPException(401, '請重新使用 Keycloak 登入後再管理使用者。')
    token = await request.app.state.oauth_manager.get_oauth_token(
        user.id,
        session_id,
    )
    if not token or not token.get('access_token'):
        raise HTTPException(401, 'Keycloak Session 已失效，請重新登入。')
    return token['access_token']


@router.post('/api/v1/auths/update/password', include_in_schema=False)
async def update_openwebui_password(
    form: ManagedPasswordChange,
    request: Request,
    user=Depends(active),
) -> bool:
    """Verify the current Keycloak password and replace it at the identity source."""
    from open_webui.utils.auth import revoke_user_tokens

    subject = user.oauth['oidc']['sub']
    access_token = await oidc_access_token(request, user)
    issuer = os.environ['OPENID_PROVIDER_URL'].split('/.well-known/', 1)[0]
    headers = {'Authorization': 'Bearer ' + access_token}
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        identity = await client.get(issuer + '/protocol/openid-connect/userinfo', headers=headers)
        if identity.status_code != 200 or identity.json().get('sub') != subject:
            raise HTTPException(401, 'Keycloak Session 已失效，請重新登入。')
        verified = await client.post(
            issuer + '/protocol/openid-connect/token',
            data={
                'grant_type': 'password',
                'client_id': os.environ['OAUTH_CLIENT_ID'],
                'client_secret': os.environ['OAUTH_CLIENT_SECRET'],
                'username': identity.json()['preferred_username'],
                'password': form.password,
                'scope': 'openid',
            },
        )
        if verified.status_code in (400, 401):
            raise HTTPException(400, '目前密碼不正確。')
        if verified.status_code != 200:
            raise HTTPException(503, 'Keycloak 密碼驗證暫時無法使用。')
        verified_identity = await client.get(
            issuer + '/protocol/openid-connect/userinfo',
            headers={'Authorization': 'Bearer ' + verified.json()['access_token']},
        )
        if verified_identity.status_code != 200 or verified_identity.json().get('sub') != subject:
            raise HTTPException(403, '目前密碼不屬於登入中的帳號。')
        synchronized = await client.put(
            'http://127.0.0.1:8005/internal/openwebui/users/me/password',
            headers=headers,
            json={'new_password': form.new_password},
        )
    if synchronized.status_code != 200:
        detail = synchronized.json().get('detail', 'Keycloak 密碼更新失敗。')
        if detail == 'keycloak_password_policy_rejected':
            detail = '新密碼不符合密碼政策，且不可重複使用最近 5 組密碼。'
        raise HTTPException(
            synchronized.status_code,
            detail,
        )
    await revoke_user_tokens(request, user.id)
    return True


@router.get('/connections/identity')
async def identity_management(user=Depends(active)):
    keycloak = os.environ['OPENID_PROVIDER_URL'].split('/realms/', 1)[0].rstrip('/')
    return {
        'provider': 'Visionnaire Keycloak',
        'admin': user.role == 'admin',
        'source_of_truth': 'visionnaire',
        'conflict_policy': 'reject_email_without_matching_oidc_sub',
        'account_url': keycloak + '/realms/visionnaire/account/#/security/signingin',
        'visionnaire_users_url': '/connections/users' if user.role == 'admin' else None,
        'openwebui_users_url': '/admin/users' if user.role == 'admin' else None,
    }


@router.get('/connections/users', include_in_schema=False)
async def visionnaire_user_management(user=Depends(active)):
    if user.role != 'admin':
        raise HTTPException(403, '只有管理員可以管理使用者。')
    keycloak = os.environ['OPENID_PROVIDER_URL'].split('/realms/', 1)[0].rstrip('/')
    return RedirectResponse(keycloak.removesuffix('/keycloak') + '/users', status_code=303)


@router.post('/api/v1/auths/add', include_in_schema=False)
async def create_openwebui_user(
    form: ManagedUserForm,
    request: Request,
    user=Depends(active),
):
    from open_webui.models.auths import Auths
    from open_webui.models.users import Users
    from open_webui.utils.auth import get_password_hash

    if user.role != 'admin':
        raise HTTPException(403, '只有管理員可以管理使用者。')
    email = form.email.strip().lower()
    if await Users.get_user_by_email(email):
        raise HTTPException(409, '電子郵件已存在。')
    access_token = await oidc_access_token(request, user)
    payload = {
        'username': form.username or email,
        'password': form.password,
        'role': form.role if form.role in {'user', 'admin'} else 'user',
        'openwebui_role': form.role or 'pending',
        'group_id': form.group_id,
        'profile': {
            'family_name': form.family_name or '',
            'given_name': form.given_name or form.name.strip(),
            'email': email,
            'middle_name': None,
            'mobile_number': None,
        },
        'force_password_change': False,
    }
    headers = {'Authorization': 'Bearer ' + access_token}
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        provisioned = await client.post(
            'http://127.0.0.1:8005/internal/openwebui/users',
            headers=headers,
            json=payload,
        )
        if provisioned.status_code != 201:
            detail = provisioned.json().get('detail', 'Visionnaire 新增使用者失敗。')
            raise HTTPException(provisioned.status_code, detail)
        canonical = provisioned.json()
        created = await Auths.insert_new_auth(
            email=email,
            password=await get_password_hash(str(uuid.uuid4())),
            name=form.name.strip(),
            profile_image_url=form.profile_image_url,
            role=form.role if form.role in {'user', 'admin'} else 'user',
            oauth={'oidc': {'sub': canonical['keycloak_subject']}},
        )
        if created is None:
            await client.delete(
                'http://127.0.0.1:8005/internal/openwebui/users/' + str(canonical['user']['id']),
                headers=headers,
            )
            raise HTTPException(500, 'Open WebUI 使用者同步失敗，已撤銷新增。')
    return created


@router.post('/api/v1/users/{target_id}/update', include_in_schema=False)
async def update_openwebui_user(
    target_id: str,
    form: ManagedUserUpdate,
    request: Request,
    user=Depends(active),
):
    from open_webui.models.auths import Auths
    from open_webui.models.users import Users

    if user.role != 'admin':
        raise HTTPException(403, '只有管理員可以管理使用者。')
    target = await Users.get_user_by_id(target_id)
    if target is None:
        raise HTTPException(404, 'Open WebUI 使用者不存在。')
    subject = target.oauth['oidc']['sub']
    access_token = await oidc_access_token(request, user)
    payload = {
        key: value
        for key, value in {
            'role': form.role,
            'password': form.password,
            'group_id': form.group_id,
            'name': form.name,
            'email': form.email.lower() if form.email else None,
        }.items()
        if value is not None
    }
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        synchronized = await client.patch(
            'http://127.0.0.1:8005/internal/openwebui/users/by-sub/' + subject,
            headers={'Authorization': 'Bearer ' + access_token},
            json=payload,
        )
    if synchronized.status_code != 200:
        raise HTTPException(
            synchronized.status_code,
            synchronized.json().get('detail', 'Visionnaire 使用者同步失敗。'),
        )
    if form.email and form.email.lower() != target.email:
        await Auths.update_email_by_id(target_id, form.email.lower())
    update = {
        key: value
        for key, value in {
            'name': form.name,
            'email': form.email.lower() if form.email else None,
            'profile_image_url': form.profile_image_url,
            'role': form.role,
        }.items()
        if value is not None
    }
    return await Users.update_user_by_id(target_id, update) if update else target


@router.delete('/api/v1/users/{target_id}', include_in_schema=False)
async def delete_openwebui_user(
    target_id: str,
    request: Request,
    user=Depends(active),
):
    from open_webui.models.auths import Auths
    from open_webui.models.users import Users

    if user.role != 'admin':
        raise HTTPException(403, '只有管理員可以管理使用者。')
    if target_id == user.id:
        raise HTTPException(403, '不能刪除目前登入的管理員。')
    target = await Users.get_user_by_id(target_id)
    if target is None:
        raise HTTPException(404, 'Open WebUI 使用者不存在。')
    subject = target.oauth['oidc']['sub']
    access_token = await oidc_access_token(request, user)
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        synchronized = await client.delete(
            'http://127.0.0.1:8005/internal/openwebui/users/by-sub/' + subject,
            headers={'Authorization': 'Bearer ' + access_token},
        )
    if synchronized.status_code != 200:
        raise HTTPException(
            synchronized.status_code,
            synchronized.json().get('detail', 'Visionnaire 使用者刪除失敗。'),
        )
    return await Auths.delete_auth_by_id(target_id)
