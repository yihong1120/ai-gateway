"""Immutable mail transport; ambiguous delivery never invalidates a live link."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from typing import Literal

import httpx
from fastapi import HTTPException


@dataclass(frozen=True)
class MailResult:
    state: Literal['sent', 'rejected', 'unknown']
    reason: str = ''


class Mailer:
    def __init__(self, settings, clients):
        self.settings, self.clients = settings, clients

    def require_configured(self):
        if not self.settings.brevo_api_key or not self.settings.mail_from:

            raise HTTPException(
                status_code=500, detail='Account email is not configured.',
            )

    async def send(self, payload):
        self.require_configured()
        client = await self.clients.get('brevo-email')
        try:
            response = await client.post(
                'https://api.brevo.com/v3/smtp/email',
                json=payload,
                headers={
                    'accept': 'application/json',
                    'api-key': self.settings.brevo_api_key,
                    'content-type': 'application/json',
                },
            )
        except httpx.HTTPError:
            return MailResult('unknown', 'transport_unavailable')
        if 200 <= response.status_code < 300:
            return MailResult('sent')
        if 400 <= response.status_code < 500 and response.status_code != 408:
            return MailResult('rejected', 'provider_rejected')
        return MailResult('unknown', 'provider_unavailable')

    def password_reset(self, email, reset_url):
        settings = self.settings
        payload: dict[str, Any] = {
            'sender': {
                'email': settings.mail_from,
                'name': settings.mail_from_name,
            },
            'to': [{'email': email}],
            'subject': 'Reset your password',
            'htmlContent': (
                '<p>Use the link below to reset your password.</p>'
                f'<p><a href="{reset_url}">Reset password</a></p>'
                '<p>This link expires soon. If you did not request this, '
                'you can ignore this email.</p>'
            ),
            'textContent': (
                'Use the following link to reset your password:\n'
                f'{reset_url}\n\n'
                'This link expires soon. If you did not request this, '
                'you can ignore this email.'
            ),
        }

        return payload

    def verification(self, email, username, verify_url):
        settings = self.settings
        expires_hours = max(
            settings.email_verification_token_ttl_seconds // 3600,
            1,
        )
        payload: dict[str, Any] = {
            'sender': {
                'email': settings.mail_from,
                'name': settings.mail_from_name,
            },
            'to': [{'email': email}],
            'params': {
                'username': username,
                'verify_url': verify_url,
                'VERIFY_URL': verify_url,
                'expires_hours': expires_hours,
            },
        }

        if settings.brevo_email_verification_template_id > 0:
            payload['templateId'] = settings.brevo_email_verification_template_id
        else:
            payload.update(
                {
                    'subject': 'Verify your Visionnaire account',
                    'htmlContent': (
                        '<p>請點擊下方連結完成信箱驗證。</p>'
                        f'<p><a href="{verify_url}">驗證信箱</a></p>'
                        f'<p>此連結將於 {expires_hours} 小時後失效。</p>'
                    ),
                    'textContent': (
                        '請使用以下連結完成信箱驗證：\n'
                        f'{verify_url}\n\n'
                        f'此連結將於 {expires_hours} 小時後失效。'
                    ),
                },
            )

        return payload
