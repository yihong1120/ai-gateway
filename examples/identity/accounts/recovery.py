"""PostgreSQL owns both one-time actions and the account mutations they permit."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.orm import selectinload

from examples.identity.accounts.password_policy import validate_password_minimum
from examples.identity.accounts.passwords import hash_password_async
from examples.identity.models import AccountActionToken
from examples.identity.models import User
from examples.identity.models import UserProfile
from examples.shared.runtime.modules import ModuleReference

_identity_login_guard = ModuleReference('examples.identity.sessions.login_guard')

RESET_SENT = 'If the email exists, a reset link has been sent.'
VERIFICATION_SENT = (
    'If the account requires verification, a verification email has been sent.'
)


def token_hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def now():
    return datetime.now(timezone.utc)


class AccountRecovery:
    def __init__(self, settings, public_url, mailer, sessions):
        self.settings, self.public_url, self.mailer = (
            settings,
            public_url.rstrip('/'),
            mailer,
        )
        self.sessions = sessions

    async def user_by_email(self, db, email):
        return await db.scalar(
            select(User)
            .options(selectinload(User.profile))
            .join(UserProfile)
            .where(UserProfile.email == email.strip().lower()),
        )

    async def issue(self, db, user, purpose, email):
        # Every issuer and consumer locks user before token, preventing lock inversion.
        await db.scalar(
            select(User)
            .where(User.id == user.id)
            .with_for_update(of=User)
            .execution_options(populate_existing=True),
        )
        profile = await db.scalar(
            select(UserProfile)
            .where(UserProfile.user_id == user.id)
            .with_for_update(of=UserProfile)
            .execution_options(populate_existing=True),
        )
        if profile is None or profile.email != email:
            raise HTTPException(
                status_code=409, detail='Account email changed; request a new link.',
            )
        instant = now()
        await db.execute(
            update(AccountActionToken)
            .where(
                AccountActionToken.user_id == user.id,
                AccountActionToken.purpose == purpose,
                AccountActionToken.consumed_at.is_(None),
            )
            .values(consumed_at=instant),
        )
        raw = secrets.token_urlsafe(48)
        ttl = (
            self.settings.password_reset_token_ttl_seconds
            if purpose == 'reset_password'
            else self.settings.email_verification_token_ttl_seconds
        )
        db.add(
            AccountActionToken(
                token_hash=token_hash(raw),
                user_id=user.id,
                purpose=purpose,
                email=email,
                expires_at=instant + timedelta(seconds=ttl),
                delivery='prepared',
            ),
        )
        await db.commit()
        return raw

    async def deliver(self, db, raw, payload):
        task = asyncio.create_task(self._deliver(db, raw, payload))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _deliver(self, db, raw, payload):
        result = await self.mailer.send(payload)
        try:
            values = {'delivery': result.state}
            if result.state == 'rejected':
                values['consumed_at'] = now()
            await db.execute(
                update(AccountActionToken)
                .where(AccountActionToken.token_hash == token_hash(raw))
                .values(**values),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            logging.getLogger(__name__).warning(
                'Account email result could not be persisted',
            )
        if result.state == 'rejected':
            raise HTTPException(
                status_code=502, detail='Account email was rejected by the provider.',
            )
        return result

    async def request_reset(self, email, db):
        user = await self.user_by_email(db, email)
        if user is not None:
            self.mailer.require_configured()
            address = email.strip().lower()
            raw = await self.issue(db, user, 'reset_password', address)
            await self.deliver(
                db,
                raw,
                self.mailer.password_reset(
                    address, f'{self.public_url}/reset_password?token={raw}',
                ),
            )
        return {'message': RESET_SENT}

    async def send_verification(self, user, db):
        self.mailer.require_configured()
        email = str(user.profile.email if user.profile else '').strip().lower()
        if not email:
            raise HTTPException(
                status_code=400, detail='User email is required for verification.',
            )
        raw = await self.issue(db, user, 'verify_email', email)
        result = await self.deliver(
            db,
            raw,
            self.mailer.verification(
                email, user.username, f'{self.public_url}/verify-email?token={raw}',
            ),
        )
        return {
            'message': VERIFICATION_SENT,
            'code': 'verification_email_sent'
            if result.state == 'sent'
            else 'verification_email_pending',
            'status': user.status,
        }

    async def resend_verification(self, email, db):
        user = await self.user_by_email(db, email)
        if (
            user is None
            or user.status != 'email_unverified'
            or user.email_verified_at is not None
        ):
            return {'message': VERIFICATION_SENT, 'code': 'verification_email_sent'}
        return await self.send_verification(user, db)

    def invalid(self, purpose, *, used=False):
        if purpose == 'reset_password':
            return HTTPException(
                status_code=400,
                detail={
                    'code': 'reset_token_invalid',
                    'message': 'Reset token is invalid or expired.',
                },
            )
        return HTTPException(
            status_code=400,
            detail={
                'code': 'token_used' if used else 'invalid_or_expired_token',
                'message': 'Token already used.'
                if used
                else 'Token is invalid or expired.',
            },
        )

    async def active(self, db, raw, purpose):
        digest = token_hash((raw or '').strip())
        candidate = await db.get(AccountActionToken, digest)
        if candidate is None or candidate.purpose != purpose:
            raise self.invalid(purpose)
        user = await db.scalar(
            select(User)
            .options(selectinload(User.profile))
            .where(User.id == candidate.user_id)
            .with_for_update(of=User)
            .execution_options(populate_existing=True),
        )
        profile = await db.scalar(
            select(UserProfile)
            .where(UserProfile.user_id == candidate.user_id)
            .with_for_update(of=UserProfile)
            .execution_options(populate_existing=True),
        )
        record = await db.scalar(
            select(AccountActionToken)
            .where(AccountActionToken.token_hash == digest)
            .with_for_update()
            .execution_options(populate_existing=True),
        )
        if (
            record is None
            or record.consumed_at is not None
            or record.delivery == 'rejected'
            or record.expires_at <= now()
            or user is None
            or profile is None
            or profile.email != record.email
        ):
            raise self.invalid(
                purpose, used=record is not None and record.consumed_at is not None,
            )
        return record, user

    async def reset(self, raw, password, db):
        validate_password_minimum(password)
        try:
            record, user = await self.active(db, raw, 'reset_password')
            user.password_hash = await hash_password_async(password)
            record.consumed_at = now()
            record.revocation_pending = True
            await db.commit()
            return None
        except HTTPException:
            await db.rollback()
            raise
        except Exception as error:
            await db.rollback()
            raise HTTPException(
                status_code=503,
                detail={
                    'code': 'database_error',
                    'message': 'Account update could not be confirmed; retry the link.',
                },
            ) from error

    async def verify(self, raw, db):
        try:
            record, user = await self.active(db, raw, 'verify_email')
            if user.email_verified_at is None:
                user.email_verified_at = now()
            if user.status == 'email_unverified':
                user.status = 'pending_admin_approval'
            record.consumed_at = now()
            await db.commit()
            if user.status not in {'active', 'pending_admin_approval'}:
                raise HTTPException(
                    status_code=403,
                    detail={'code': 'account_not_verifiable', 'status': user.status},
                )
            return {
                'message': 'Email verified successfully.',
                'code': 'email_verified',
                'status': user.status,
            }
        except HTTPException:
            await db.rollback()
            raise
        except Exception as error:
            await db.rollback()
            raise HTTPException(
                status_code=503,
                detail={
                    'code': 'database_error',
                    'message': 'Account update could not be confirmed; retry the link.',
                },
            ) from error

    async def drain_revocations(self, db, redis, *, limit=32, action=None):
        rows = (
            await db.execute(
                select(AccountActionToken, User.username)
                .join(User, User.id == AccountActionToken.user_id)
                .where(
                    AccountActionToken.revocation_pending.is_(True),
                    True if action is None else AccountActionToken.token_hash == action,
                )
                .order_by(AccountActionToken.created_at)
                .limit(limit)
                .with_for_update(of=AccountActionToken, skip_locked=True),
            )
        ).all()
        try:
            for record, username in rows:
                async with asyncio.timeout(10):
                    await self.sessions.revoke_once(redis, username, record.token_hash)
                    clear_login_guard_for_identifiers = _identity_login_guard.attribute(
                        'clear_login_guard_for_identifiers',
                    )

                    await clear_login_guard_for_identifiers(
                        redis, [username, record.email],
                    )
                record.revocation_pending = False
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        return len(rows)
