from __future__ import annotations

import os

from pydantic import Field
from pydantic_settings import BaseSettings


class MailSettings(BaseSettings):
    model_config = {'frozen': True, 'populate_by_name': True}

    brevo_api_key: str = Field(
        validation_alias='BREVO_API_KEY', repr=False,
        default_factory=lambda: os.getenv('BREVO_API_KEY', ''),
    )

    mail_from: str = Field(
        validation_alias='MAIL_FROM', default_factory=lambda: os.getenv('MAIL_FROM', ''),
    )

    mail_from_name: str = Field(
        validation_alias='MAIL_FROM_NAME',
        default_factory=lambda: os.getenv('MAIL_FROM_NAME', 'Visionnaire'),
    )

    password_reset_token_ttl_seconds: int = Field(
        validation_alias='PASSWORD_RESET_TOKEN_TTL_SECONDS', ge=1,
        default_factory=lambda: int(
            os.getenv('PASSWORD_RESET_TOKEN_TTL_SECONDS', '1800'),
        ),
    )

    password_reset_email_rate_limit_seconds: int = Field(
        validation_alias='PASSWORD_RESET_EMAIL_RATE_LIMIT_SECONDS', ge=1,
        default_factory=lambda: int(
            os.getenv('PASSWORD_RESET_EMAIL_RATE_LIMIT_SECONDS', '60'),
        ),
    )

    password_reset_ip_rate_limit_window_seconds: int = Field(
        validation_alias='PASSWORD_RESET_IP_RATE_LIMIT_WINDOW_SECONDS', ge=1,
        default_factory=lambda: int(
            os.getenv('PASSWORD_RESET_IP_RATE_LIMIT_WINDOW_SECONDS', '600'),
        ),
    )

    password_reset_ip_rate_limit_max: int = Field(
        validation_alias='PASSWORD_RESET_IP_RATE_LIMIT_MAX', ge=1,
        default_factory=lambda: int(
            os.getenv('PASSWORD_RESET_IP_RATE_LIMIT_MAX', '5'),
        ),
    )

    email_verification_token_ttl_seconds: int = Field(
        validation_alias='EMAIL_VERIFICATION_TOKEN_TTL_SECONDS', ge=1,
        default_factory=lambda: int(
            os.getenv('EMAIL_VERIFICATION_TOKEN_TTL_SECONDS', '86400'),
        ),
    )

    email_verification_resend_rate_limit_seconds: int = Field(
        validation_alias='EMAIL_VERIFICATION_RESEND_RATE_LIMIT_SECONDS', ge=1,
        default_factory=lambda: int(
            os.getenv('EMAIL_VERIFICATION_RESEND_RATE_LIMIT_SECONDS', '60'),
        ),
    )

    email_verification_daily_limit: int = Field(
        validation_alias='EMAIL_VERIFICATION_DAILY_LIMIT', ge=1,
        default_factory=lambda: int(
            os.getenv('EMAIL_VERIFICATION_DAILY_LIMIT', '5'),
        ),
    )

    email_verification_daily_limit_window_seconds: int = Field(
        validation_alias='EMAIL_VERIFICATION_DAILY_LIMIT_WINDOW_SECONDS', ge=1,
        default_factory=lambda: int(
            os.getenv('EMAIL_VERIFICATION_DAILY_LIMIT_WINDOW_SECONDS', '86400'),
        ),
    )

    brevo_email_verification_template_id: int = Field(
        validation_alias='BREVO_EMAIL_VERIFICATION_TEMPLATE_ID',
        default_factory=lambda: int(
            os.getenv('BREVO_EMAIL_VERIFICATION_TEMPLATE_ID', '0') or '0',
        ),
    )
