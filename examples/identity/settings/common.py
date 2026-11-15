from __future__ import annotations

import os
import re


def _join_env_values(*names: str) -> str:
    """Return comma-separated non-empty environment values."""
    return ','.join(value for name in names if (value := os.getenv(name, '').strip()))


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse common boolean environment variable values."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


DEFAULT_GOOGLE_CLIENT_IDS = (
    '860473757501-c1gtkrqr4lsa52vgoq7vclprm8atjvtv.apps.googleusercontent.com,'
    '860473757501-s53qldp7i294qbg1ia8aq822oa0rudj2.apps.googleusercontent.com,'
    '860473757501-088t4flpgv0kdds6pu4a5m1fntamf1ht.apps.googleusercontent.com'
)

DEFAULT_APPLE_BUNDLE_ID = 'com.changdar.visionnaire'
DEFAULT_APPLE_SERVICE_ID = 'com.changdar.visionnaire.signin'
DEFAULT_NATIVE_SOCIAL_ALLOWED_CLIENTS_JSON = (
    '{"visionnaire-mobile":["com.changdar.visionnaire:/oauthredirect"]}'
)
_OIDC_CLIENT_ID_RE = re.compile(r'^[A-Za-z0-9._-]{1,128}$')
