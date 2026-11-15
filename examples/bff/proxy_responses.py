"""Pure response rewriting for browser-reachable, BFF-protected capabilities."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import Request

from examples.identity.deployment_context import DeploymentBinding
from examples.bff.proxy_config import _VIOLATION_MEDIA_ENDPOINTS, _VIOLATION_MEDIA_FIELDS


def _bff_service_path(request: Request, service: str) -> str:
    """Retain a deployment-specific public prefix from the browser request."""
    marker = f'/{service}'
    request_path = request.url.path.rstrip('/')
    offset = request_path.find(marker)
    if offset >= 0:
        marker_end = offset + len(marker)
        if marker_end == len(request_path) or request_path[marker_end] == '/':
            return request_path[:marker_end]
    return f'/bff/{service}'


def _rewrite_media(payload, prefix):
    changed = False
    if isinstance(payload, list):
        for item in payload:
            changed |= _rewrite_media(item, prefix)
    elif isinstance(payload, dict):
        for key, value in payload.items():
            if key in _VIOLATION_MEDIA_FIELDS:
                if isinstance(value, str):
                    source = urlsplit(value)
                    if source.path in _VIOLATION_MEDIA_ENDPOINTS:
                        rewritten = prefix + source.path + ('?' + source.query if source.query else '')
                        if rewritten != value:
                            payload[key], changed = rewritten, True
            else:
                changed |= _rewrite_media(value, prefix)
    return changed


def _rewrite_download(payload, prefix, api_path):
    if not isinstance(payload, dict) or not isinstance(payload.get('temp_url'), str):
        return False
    source = urlsplit(payload['temp_url'])
    if source.scheme or source.netloc or source.query or source.fragment:
        return False
    prefixes = ('/download/temp/', f'{api_path}/file_manage/download/temp/')
    token = next((source.path[len(p):] for p in prefixes if source.path.startswith(p)), '')
    if not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', token):
        return False
    payload['temp_url'] = prefix + '/download/temp/' + token
    return True


def rewrite_response(
    upstream: httpx.Response,
    request: Request,
    deployment: DeploymentBinding | None,
    service: str,
    suffix: str,
) -> tuple[bytes, bool]:
    """Rewrite recognised JSON capabilities, preserving every other body byte."""
    content = upstream.content
    is_media = service == 'violations'
    is_download = (
        service in {'files', 'file_manage'} and upstream.status_code == 200
        and re.fullmatch(r'(?:versions|exports)/[0-9]+/generate_temp_url', suffix)
    )
    if not (is_media or is_download):
        return content, False
    if upstream.headers.get('content-type', '').split(';', 1)[0].lower() != 'application/json':
        return content, False
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return content, False

    public = urlsplit(deployment.api_base_url) if deployment else None
    prefix = urlunsplit((
        public.scheme if public else '', public.netloc if public else '',
        _bff_service_path(request, service), '', '',
    ))
    changed = (
        _rewrite_media(payload, prefix) if is_media
        else _rewrite_download(payload, prefix, public.path.rstrip('/') if public else '/hazard/api')
    )
    if not changed:
        return content, False
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8'), True
