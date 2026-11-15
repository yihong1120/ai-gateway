"""Resolve accounting ownership without treating service costs as user budgets."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from examples.ai_usage.config import UsageQuerySettings
from examples.ai_usage.schemas.usage import UnattributedCategory
from examples.ai_usage.schemas.usage import UsageSource


def log_metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    """LiteLLM may return metadata as a JSON object or encoded JSON."""
    value = row.get('metadata')
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def usage_metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    """Read only LiteLLM's explicitly persisted custom metadata container."""
    value = log_metadata(row).get('spend_logs_metadata')
    return value if isinstance(value, dict) else {}


def is_document_key(row: Mapping[str, Any], settings: UsageQuerySettings) -> bool:
    """A caller-controlled source tag alone must never establish ownership."""
    return bool(settings.official_document_key_hash) and row.get('api_key') == settings.official_document_key_hash


def accounting_owner(row: Mapping[str, Any], settings: UsageQuerySettings) -> str | None:
    """Prefer actual end-user billing; allow metadata only for the pinned key."""
    owner = row.get('end_user')
    if isinstance(owner, str) and owner.strip():
        return owner
    metadata = usage_metadata(row)
    if is_document_key(row, settings) and metadata.get('ai_usage_source') == 'official_document':
        subject = metadata.get('ai_usage_subject')
        if isinstance(subject, str) and subject.strip() and subject == subject.strip() and len(subject) <= 255:
            return subject
    return None


def usage_source(row: Mapping[str, Any], settings: UsageQuerySettings) -> UsageSource:
    """Expose a fixed source enum, never raw key aliases or arbitrary tags."""
    if is_document_key(row, settings):
        return 'official_document'
    if usage_metadata(row).get('ai_usage_source') == 'company_agent':
        return 'company_agent'
    alias = log_metadata(row).get('user_api_key_alias')
    if alias == 'openwebui-backend':
        return 'openwebui'
    if isinstance(alias, str) and alias.startswith('company-ai-production-'):
        return 'company_ai'
    return 'unknown'


def is_user_billed(row: Mapping[str, Any]) -> bool:
    """Only LiteLLM's actual end_user participates in its daily user budget."""
    return isinstance(row.get('end_user'), str) and bool(row['end_user'].strip())


def unattributed_category(row: Mapping[str, Any], settings: UsageQuerySettings) -> UnattributedCategory:
    """Classify unresolved rows using recorded facts, never names or timing."""
    if row.get('call_type') in {
        '/health', '/health/liveliness', '/health/readiness', '/v1/models', '/models', '/spend/logs/v2',
    }:
        return 'system_operation'
    if is_document_key(row, settings):
        return 'document_service'
    if accounting_owner(row, settings):
        return 'identity_not_linked'
    status = row.get('status') or log_metadata(row).get('status')
    if status in {'failure', 'failed', 'error'}:
        return 'failed_request'
    return 'missing_identity'
