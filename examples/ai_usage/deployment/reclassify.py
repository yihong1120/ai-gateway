"""Prepare/apply/roll back audited metadata-only usage reclassification.

Run prepare with the WebUI Python environment: company response IDs are read
from authenticated encrypted checkpoints, never inferred from prompt text,
names, or nearby timestamps. Apply/rollback preserve every accounting field.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from examples.ai_usage.config import AiUsageSettings
from examples.ai_usage.services.accounting import (
    accounting_owner,
    is_document_key,
    log_metadata,
    unattributed_category,
)
from examples.shared.config import config_path, read_services

FINANCIAL_FIELDS = (
    'request_id', 'api_key', 'end_user', 'spend', 'prompt_tokens', 'completion_tokens',
    'total_tokens', 'startTime', 'endTime', 'call_type', 'status',
)


def fingerprint(row: dict[str, Any]) -> str:
    """Protect values, billing identity and key, not just aggregate money."""
    data = {name: row.get(name) for name in FINANCIAL_FIELDS}
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def proposed_metadata(
    row: dict[str, Any], settings: AiUsageSettings, linked: set[str],
    company_evidence: dict[str, list[tuple[str, str]]], run_id: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Reclassify using positive evidence and keep the accounting owner fixed."""
    original = row.get('metadata')
    metadata = log_metadata(row)
    if original is not None and not metadata and original not in ({}, '{}'):
        return None, ['unsupported_metadata']
    metadata = json.loads(json.dumps(metadata))
    tags = metadata.get('spend_logs_metadata')
    if tags is not None and not isinstance(tags, dict):
        return None, ['unsupported_custom_metadata']
    tags = dict(tags or {})
    bases = []
    evidence = company_evidence.get(row['request_id'])
    if evidence:
        subjects = {subject for _, subject in evidence}
        if len(subjects) != 1 or row.get('end_user') not in subjects or not subjects <= linked:
            return None, ['identity_conflict']
        if is_document_key(row, settings) or tags.get('ai_usage_source') not in (None, 'company_agent', 'openwebui', 'unknown'):
            return None, ['source_conflict']
        if tags.get('ai_usage_source') != 'company_agent':
            tags['ai_usage_source'] = 'company_agent'
            bases.append('encrypted_checkpoint_response_id')
    if is_document_key(row, settings) and tags.get('ai_usage_source') != 'official_document':
        tags['ai_usage_source'] = 'official_document'
        bases.append('dedicated_document_key')
    if accounting_owner(row, settings) not in linked:
        category = unattributed_category(row, settings)
        if tags.get('ai_usage_classification') != category:
            tags['ai_usage_classification'] = category
            bases.append('recorded_accounting_fields')
    if not bases:
        return None, []
    tags['ai_usage_reclassification_run'] = run_id
    tags['ai_usage_reclassification_basis'] = ','.join(bases)
    metadata['spend_logs_metadata'] = tags
    if accounting_owner({**row, 'metadata': metadata}, settings) != accounting_owner(row, settings):
        return None, ['would_change_owner']
    return metadata, bases


def company_response_evidence(web: dict[str, Any]) -> dict[str, list[tuple[str, str]]]:
    """Match server-owned jobs, checkpoint billing identity and response IDs."""
    from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    config = json.loads(Path(web['COMPANY_AGENT_CONFIG']).read_text())
    state = Path(config.get('settings', config)['state_dir'])
    serde = EncryptedSerializer.from_pycryptodome_aes(
        serde=JsonPlusSerializer(pickle_fallback=False, allowed_json_modules=[]),
        key=(state / 'checkpoint.key').read_bytes(),
    )
    references: dict[str, set[tuple[str, str]]] = defaultdict(set)
    with psycopg.connect(web['DATABASE_URL']) as db, db.cursor() as cursor:
        cursor.execute('SELECT id,owner FROM company_agent.jobs')
        jobs = dict(cursor.fetchall())
        cursor.execute(
            "SELECT engine_job,owner,subject FROM company_agent.web_jobs WHERE kind='company' AND engine_job IS NOT NULL",
        )
        owned: dict[str, set[tuple[str, str]]] = defaultdict(set)
        for job, owner, subject in cursor.fetchall():
            owned[job].add((owner, subject))
        billing = {}
        cursor.execute("SELECT thread_id,type,blob FROM company_agent.checkpoint_blobs WHERE channel='billing_identity'")
        for job, kind, blob in cursor.fetchall():
            identity = serde.loads_typed((kind, bytes(blob)))
            if isinstance(identity, dict) and identity.get('owner') == jobs.get(job):
                pair = (identity.get('owner'), identity.get('subject'))
                if pair[1] and (job not in owned or owned[job] == {pair}):
                    billing[job] = pair
        for table in ('checkpoint_blobs', 'checkpoint_writes'):
            cursor.execute(f"SELECT thread_id,type,blob FROM company_agent.{table} WHERE channel='messages'")
            for job, kind, blob in cursor.fetchall():
                if job not in billing:
                    continue
                messages = serde.loads_typed((kind, bytes(blob)))
                if not isinstance(messages, list):
                    messages = [messages]
                for message in messages:
                    if getattr(message, 'type', None) != 'ai':
                        continue
                    identity = (getattr(message, 'response_metadata', {}) or {}).get('id')
                    if isinstance(identity, str) and identity:
                        references[identity].add((job, billing[job][1]))
    return {key: sorted(value) for key, value in references.items()}


def save_json(path: Path, value: Any) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
        stream.write('\n')


def context(root: Path):
    settings = AiUsageSettings.from_environment()
    if not settings.gateway_tenant_id or not settings.admin_database_url:
        raise RuntimeError('An explicit gateway tenant and read-only directory database are required')
    source = config_path(os.getenv('AI_GATEWAY_CONFIG', root / 'config.local.toml'))
    sections = read_services('webui', 'litellm', path=source)
    web, llm = sections['webui'], sections['litellm']
    linked, foreign = set(), set()
    with psycopg.connect(settings.admin_database_url.replace('postgresql+asyncpg://', 'postgresql://'),
                         options='-c default_transaction_read_only=on') as db, db.cursor() as cursor:
        cursor.execute(
            'SELECT i.provider_user_id,u.tenant_id FROM user_identities i JOIN users u ON u.id=i.user_id WHERE i.provider=%s',
            (settings.oidc_identity_provider,),
        )
        for subject, tenant in cursor.fetchall():
            (linked if str(tenant) == settings.gateway_tenant_id else foreign).add(subject)
    return settings, web, llm, linked, foreign


def prepare(root: Path, run: Path) -> None:
    settings, web, llm, linked, foreign = context(root)
    run.mkdir(mode=0o700, parents=True, exist_ok=False)
    evidence = company_response_evidence(web)
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    with psycopg.connect(llm['LITELLM_DATABASE_URL'], row_factory=dict_row) as db:
        rows = db.execute(
            'SELECT *, metadata IS NULL AS metadata_sql_null FROM "LiteLLM_SpendLogs" '
            'WHERE COALESCE("endTime","startTime")<=%s ORDER BY request_id', (cutoff,),
        ).fetchall()
    plan = []
    issues: dict[str, int] = defaultdict(int)
    bases_count: dict[str, int] = defaultdict(int)
    category_count: dict[str, int] = defaultdict(int)
    thirty_company = 0
    thirty_company_spend = 0.0
    for row in rows:
        if accounting_owner(row, settings) in foreign:
            continue
        updated, bases = proposed_metadata(row, settings, linked, evidence, run.name)
        if updated is None:
            for basis in bases:
                issues[basis] += 1
            continue
        if 'encrypted_checkpoint_response_id' in bases and datetime(2026, 9, 28, 16) <= row['startTime'] < datetime(2026, 10, 28, 16):
            thirty_company += 1
            thirty_company_spend += float(row['spend'] or 0)
        for basis in bases:
            bases_count[basis] += 1
        if accounting_owner(row, settings) not in linked:
            category_count[unattributed_category(row, settings)] += 1
        plan.append({
            'request_id': row['request_id'], 'original_metadata': row['metadata'],
            'new_metadata': updated, 'financial_sha256': fingerprint(row), 'bases': bases,
            'original_metadata_sql_null': row['metadata_sql_null'],
        })
    save_json(run / 'plan.private.json', plan)
    planned_ids = {item['request_id'] for item in plan}
    save_json(run / 'evidence.private.json', {key: evidence[key] for key in evidence if key in planned_ids})
    report = {
        'run_id': run.name, 'host': socket.gethostname(), 'cutoff': cutoff.isoformat(),
        'scanned_records': len(rows), 'planned_updates': len(plan), 'basis_counts': dict(bases_count),
        'unattributed_categories': dict(category_count), 'issues': dict(issues),
        'company_records_reclassified_in_30_day_window': thirty_company,
        'company_spend_reclassified_in_30_day_window': thirty_company_spend,
        'owner_changes': 0, 'financial_changes': 0, 'state': 'prepared',
        'plan_sha256': hashlib.sha256((run / 'plan.private.json').read_bytes()).hexdigest(),
        'database_sha256': hashlib.sha256(llm['LITELLM_DATABASE_URL'].encode()).hexdigest(),
    }
    save_json(run / 'prepared.json', report)
    print(json.dumps(report))


def apply_or_restore(root: Path, run: Path, *, rollback: bool) -> None:
    if run.is_symlink() or run.stat().st_mode & 0o077:
        raise RuntimeError('The run directory must be private and not a symlink')
    prepared = json.loads((run / 'prepared.json').read_text())
    if prepared['host'] != socket.gethostname():
        raise RuntimeError('Prepared metadata belongs to a different host')
    _, _, llm, _, _ = context(root)
    if hashlib.sha256(llm['LITELLM_DATABASE_URL'].encode()).hexdigest() != prepared['database_sha256']:
        raise RuntimeError('Prepared metadata belongs to a different database')
    if hashlib.sha256((run / 'plan.private.json').read_bytes()).hexdigest() != prepared['plan_sha256']:
        raise RuntimeError('The prepared plan has changed')
    marker = run / ('rolled-back.json' if rollback else 'applied.json')
    if marker.exists() or (not rollback and (run / 'rolled-back.json').exists()):
        raise RuntimeError('This action has already been recorded')
    if rollback and not (run / 'applied.json').exists():
        raise RuntimeError('No successful apply exists to roll back')
    plan = json.loads((run / 'plan.private.json').read_text())
    with psycopg.connect(llm['LITELLM_DATABASE_URL'], row_factory=dict_row) as db:
        for item in plan:
            row = db.execute(
                'SELECT *, metadata IS NULL AS metadata_sql_null FROM "LiteLLM_SpendLogs" '
                'WHERE request_id=%s FOR UPDATE',
                (item['request_id'],),
            ).fetchone()
            if row is None or fingerprint(row) != item['financial_sha256']:
                raise RuntimeError('An accounting field changed after preparation')
            expected = item['new_metadata'] if rollback else item['original_metadata']
            target = item['original_metadata'] if rollback else item['new_metadata']
            original_sql_null = item.get('original_metadata_sql_null', False)
            if item['original_metadata'] is None and 'original_metadata_sql_null' not in item:
                raise RuntimeError('Legacy plan lacks SQL NULL provenance')
            expected_sql_null = False if rollback else original_sql_null
            if row['metadata'] != expected or row['metadata_sql_null'] != expected_sql_null:
                raise RuntimeError('Metadata changed after preparation; the transaction was not applied')
            db.execute(
                'UPDATE "LiteLLM_SpendLogs" SET metadata=%s WHERE request_id=%s',
                (None if rollback and original_sql_null else Jsonb(target), item['request_id']),
            )
        for item in plan:
            row = db.execute('SELECT * FROM "LiteLLM_SpendLogs" WHERE request_id=%s', (item['request_id'],)).fetchone()
            if row is None or fingerprint(row) != item['financial_sha256']:
                raise RuntimeError('Financial invariant failed; transaction rolled back')
    report = {
        'run_id': run.name, 'state': 'rolled_back' if rollback else 'applied',
        'records_updated': len(plan), 'owner_changes': 0, 'financial_changes': 0,
        'verified_at': datetime.now(timezone.utc).isoformat(),
    }
    save_json(marker, report)
    print(json.dumps(report))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'apply', 'rollback'))
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument('--run-dir', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    os.environ.setdefault('AI_GATEWAY_CONFIG', str(args.root / 'config.local.toml'))
    if args.action == 'prepare':
        prepare(args.root, args.run_dir)
    else:
        apply_or_restore(args.root, args.run_dir, rollback=args.action == 'rollback')


if __name__ == '__main__':
    main()
