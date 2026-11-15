"""One configured local token issuer; Redis performs rotation and revocation atomically."""

from __future__ import annotations
import datetime
from uuid import uuid4
import jwt
from fastapi import HTTPException
from examples.identity.sessions.jwt import access_token_subject_from_payload, refresh_token_subject_from_payload
from examples.identity.sessions.revocation import revoke_access_token
from examples.identity.sessions.issuer import issue_access_token, issue_refresh_token

ACCESS_TTL = datetime.timedelta(minutes=15)
REFRESH_TTL = datetime.timedelta(days=30)


class LocalTokens:
    def __init__(self, access, refresh, sessions, cache):
        self.access, self.refresh, self.sessions, self.cache = (
            access,
            refresh,
            sessions,
            cache,
        )

    def decoded_refresh(self, token, deployment=None):
        try:
            data = self.refresh.decode_token(
                token,
                expected_issuer=deployment.issuer if deployment else None,
                expected_audience=deployment.audience if deployment else None,
            )
            subject = refresh_token_subject_from_payload(data)
        except jwt.PyJWTError as error:
            raise HTTPException(
                status_code=401, detail='Invalid refresh token'
            ) from error
        if deployment is not None and (
            subject.get('tenant_id') != str(deployment.tenant_id)
            or subject.get('deployment_id') != str(deployment.deployment_id)
            or subject.get('config_revision') != deployment.config_revision
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    'code': 'deployment_configuration_changed',
                    'message': 'Deployment configuration changed; sign in again.',
                },
            )
        return data

    async def verify(self, redis, token, deployment=None):
        data = self.decoded_refresh(token, deployment)
        subject = data['subject']
        await self.sessions.validate(
            redis, token, subject['username'], subject['family_id']
        )
        return data

    async def pair(
        self,
        redis,
        user,
        features,
        epochs,
        deployment=None,
        *,
        previous=None,
        family=None,
    ):
        family = family or str(uuid4())
        jti = str(uuid4())
        access = issue_access_token(
            self.access,
            username=user['username'],
            user_id=user['id'],
            role=user['role'],
            jti=jti,
            feature_names=features,
            expires_delta=ACCESS_TTL,
            deployment=deployment,
        )
        refresh = issue_refresh_token(
            self.refresh,
            username=user['username'],
            family_id=family,
            token_id=str(uuid4()),
            expires_delta=REFRESH_TTL,
            deployment=deployment,
        )
        claims = self.access.decode_token_for_lifecycle(access)
        await self.sessions.register(
            redis,
            user['username'],
            family,
            refresh,
            jti,
            int(claims['exp']),
            epochs,
            previous=previous,
        )
        return {
            'access_token': access,
            'refresh_token': refresh,
            'feature_names': features,
            **({'deployment': deployment.as_response()} if deployment else {}),
        }

    async def issue(self, redis, user, features, deployment=None, *, epochs=None):
        epochs = epochs or await self.sessions.epochs(redis, user.username)
        if deployment is not None and user.tenant_id != deployment.tenant_id:
            raise HTTPException(
                status_code=403,
                detail={
                    'code': 'tenant_access_denied',
                    'message': 'This account does not belong to this deployment.',
                },
            )
        snapshot = {
            'db_user': {
                'id': user.id,
                'username': user.username,
                'role': user.role,
                'group_id': user.group_id,
                'status': user.status,
                'tenant_id': str(user.tenant_id) if deployment else '',
            },
            'feature_names': features,
        }
        await self.cache.set_user_data(redis, user.username, snapshot)
        result = await self.pair(
            redis, snapshot['db_user'], features, epochs, deployment
        )
        return {
            **result,
            **{
                key: snapshot['db_user'][key]
                for key in ('username', 'role', 'group_id')
            },
            'user_id': user.id,
        }

    async def rotate(self, redis, token, deployment=None):
        data = await self.verify(redis, token, deployment)
        subject = data['subject']
        username = subject['username']
        epochs = await self.sessions.epochs(redis, username)
        snapshot = await self.cache.get_user_data(redis, username)
        if not snapshot or snapshot['db_user']['status'] != 'active':
            raise HTTPException(status_code=401, detail='Refresh token not recognised')
        if deployment is not None and snapshot['db_user'].get('tenant_id') != str(
            deployment.tenant_id
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    'code': 'deployment_configuration_changed',
                    'message': 'Deployment configuration changed; sign in again.',
                },
            )
        return await self.pair(
            redis,
            snapshot['db_user'],
            snapshot['feature_names'],
            epochs,
            deployment,
            previous=token,
            family=subject['family_id'],
        )

    async def logout(self, redis, refresh, authorization):
        access_payload, subject = None, None
        if authorization:
            scheme, _, token = authorization.partition(' ')
            if scheme.lower() == 'bearer':
                try:
                    data = self.access.decode_token_for_lifecycle(
                        token, verify_exp=False
                    )
                    subject = access_token_subject_from_payload(data)
                    access_payload = {'jti': subject['jti'], 'exp': data['exp']}
                except jwt.PyJWTError:
                    pass
        if access_payload is not None:
            await revoke_access_token(redis, access_payload)
        if refresh:
            try:
                payload = self.refresh.decode_token_for_lifecycle(
                    refresh, verify_exp=False
                )
                owner = refresh_token_subject_from_payload(payload)
            except jwt.PyJWTError:
                return
            await self.sessions.revoke_family(
                redis,
                owner['username'],
                owner['family_id'],
                all_access=access_payload is None,
            )
