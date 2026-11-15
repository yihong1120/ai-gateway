"""Resolve a user's current OIDC token through the WebUI host at tool time."""
from __future__ import annotations

import jwt

from examples.identity.adapters import webui
from examples.shared.runtime.modules import ModuleReference

_webui_oauth_sessions = ModuleReference('open_webui.models.oauth_sessions')

AUTHORIZATION_REQUIRED = '外部工具需要本人有效的 OIDC 登入，請重新登入後再試。'


class WebUIUserTokens:
    """Runtime-only adapter; credentials never enter job settings or checkpoints.

    The newest OIDC session is selected by creation time, then ID for ties.
    A failed or revoked session never falls back to an older login. The host
    manager owns token refresh and checks the selected session's ownership again.
    """

    def __init__(self, oauth_manager):
        self._oauth_manager = oauth_manager

    async def __call__(self, owner: str, subject: str) -> str:
        try:
            if any(
                not isinstance(value, str) or not value or value != value.strip()
                for value in (owner, subject)
            ):
                raise ValueError(AUTHORIZATION_REQUIRED)
            user = await webui.verified_user(owner)
            if (
                user.get('id') != owner
                or user.get('role') not in {'user', 'admin'}
                or (user.get('oauth') or {}).get('oidc', {}).get('sub') != subject
            ):
                raise ValueError(AUTHORIZATION_REQUIRED)

            # Loading this optional host module may initialize its database;
            # never import it while loading gateway libraries or CLI tools.
            OAuthSessions = _webui_oauth_sessions.attribute('OAuthSessions')

            sessions = await OAuthSessions.get_sessions_by_user_id(owner)
            candidates = [
                session for session in sessions
                if session.provider == 'oidc' and session.user_id == owner
            ]
            if not candidates:
                raise ValueError(AUTHORIZATION_REQUIRED)
            session = max(candidates, key=_session_order)
            token = await self._oauth_manager.get_oauth_token(owner, session.id)
            access_token = token.get('access_token') if isinstance(token, dict) else None
            if (
                not isinstance(access_token, str)
                or not access_token
                or any(character.isspace() or ord(character) < 32 for character in access_token)
            ):
                raise ValueError(AUTHORIZATION_REQUIRED)
            # This decode only rejects stale sessions after an account binding
            # changes. It does not authorize the request: the remote service
            # still verifies signature, issuer, audience, expiry and its ACLs.
            claims = jwt.decode(access_token, options={'verify_signature': False})
            if claims.get('sub') != subject:
                raise ValueError(AUTHORIZATION_REQUIRED)
            return access_token
        except Exception:
            # Identity/database/provider exceptions may contain credentials or
            # upstream response bodies. Never expose those through tool errors.
            raise ValueError(AUTHORIZATION_REQUIRED) from None


def _session_order(session):
    if (
        not isinstance(session.created_at, int)
        or not isinstance(session.id, str)
        or not session.id
    ):
        raise ValueError(AUTHORIZATION_REQUIRED)
    return session.created_at, session.id
