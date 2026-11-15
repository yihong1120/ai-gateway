"""Inspect retention classes and explicitly prune expired, rebuildable document caches."""

from __future__ import annotations

import argparse
import json
import math
import os
import time

from psycopg import sql

from .database import cache_owner_lock_key, connection, shared_content_lock_namespace

POLICY = {
    'jobs': 'user_result',
    'web_jobs': 'user_result',
    'artifacts': 'user_result',
    'tool_receipts': 'execution_receipt',
    'assignment_claims': 'execution_receipt',
    'proposals': 'execution_receipt',
    'audit': 'audit',
    'tasks': 'execution_receipt',
    'writes': 'external_write_receipt',
    'event_receipts': 'external_write_receipt',
    'source_manifests': 'external_write_receipt',
    'processing_chunks': 'rebuildable_cache',
    'shared_content_cache': 'content_cache_and_paid_receipts',
    'document_analysis_results': 'paid_analysis_receipt',
    'document_analysis_uses': 'analysis_delivery_audit',
    'document_analysis_resolutions': 'analysis_recovery_audit',
    'pending_clarifications': 'expiring_continuation',
    'clarification_outputs': 'execution_receipt',
    'clarification_output_reviews': 'audit',
    'checkpoints': 'execution_receipt',
    'checkpoint_blobs': 'execution_receipt',
    'checkpoint_writes': 'execution_receipt',
    'analysis_evidence': 'execution_receipt',
    'artifact_validations': 'execution_receipt',
    'delivery_plans': 'execution_receipt',
    'eip_read_inputs': 'user_source',
    'eip_sources': 'user_source',
    'accounts': 'credentials',
    'nas_bindings': 'credentials',
}


class Retention:
    def __init__(self, dsn, schema):
        self.dsn, self.schema = dsn, schema

    def report(self, *, exact_tables=()):
        """Catalog-only estimates by default; exact scans must name their tables."""
        requested = set(exact_tables)
        result = []
        with connection(self.dsn, self.schema) as db:
            tables = db.execute(
                """SELECT c.relname,c.reltuples,pg_total_relation_size(c.oid)
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=%s AND c.relkind IN ('r','p') ORDER BY c.relname""",
                (self.schema,),
            ).fetchall()
            if requested - {name for name, _, _ in tables}:
                raise ValueError('Exact counts must name existing tables in this schema.')
            for table, estimated, size in tables:
                exact = table in requested
                count = (db.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier(table))).fetchone()[0]
                         if exact else max(0, round(estimated)) if estimated >= 0 else None)
                result.append({
                    'table': table, 'category': POLICY.get(table, 'protected_unclassified'),
                    'rows': count, 'rows_kind': 'exact' if exact else 'estimate' if count is not None else 'unknown',
                    'bytes': size, 'automatic_deletion': False,
                })
        return result

    def expired_cache(self, before, *, limit=100, apply=False):
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
            or not math.isfinite(before)
            or before > time.time() - 86400
        ):
            raise ValueError('Cache cutoff must be at least one day old; batch size must be 1–1000.')
        with connection(self.dsn, self.schema) as db:
            if not apply:
                count, size = db.execute(
                    'SELECT count(*),COALESCE(sum(octet_length(payload)),0) FROM '
                    '(SELECT payload FROM processing_chunks WHERE updated<%s ORDER BY updated,owner,key LIMIT %s) selected',
                    (before, limit),
                ).fetchone()
                return {'mode': 'preview', 'rows': count, 'payload_bytes': size}
            # One bounded statement: lock each owner once, skip active batches,
            # then delete only the selected expired rows in this transaction.
            deleted = db.execute(
                """WITH candidates AS MATERIALIZED (
                    SELECT owner,key FROM processing_chunks WHERE updated<%s
                    ORDER BY updated,owner,key LIMIT %s FOR UPDATE SKIP LOCKED
                ), owners AS MATERIALIZED (SELECT DISTINCT owner FROM candidates),
                available AS MATERIALIZED (
                    SELECT owner FROM owners
                    WHERE pg_try_advisory_xact_lock(hashtextextended(%s || owner,0))
                ) DELETE FROM processing_chunks p USING candidates c,available a
                  WHERE p.owner=c.owner AND p.key=c.key AND a.owner=c.owner AND p.updated<%s""",
                (before, limit, cache_owner_lock_key(self.schema, ''), before),
            ).rowcount
            return {'mode': 'applied', 'rows': deleted}

    def expired_shared_content(self, *, limit=100, apply=False):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Shared content cache batch size must be 1–1000.')
        with connection(self.dsn, self.schema) as db:
            if not apply:
                count, size = db.execute("""SELECT count(*),COALESCE(sum(octet_length(payload)),0)
                    FROM (SELECT payload FROM shared_content_cache WHERE state IN ('ready','failed') AND expires_at<CURRENT_TIMESTAMP
                        ORDER BY expires_at,scope,key LIMIT %s) selected""", (limit,)).fetchone()
                return {'mode': 'preview', 'rows': count, 'payload_bytes': size}
            deleted = db.execute("""WITH candidates AS MATERIALIZED (
                    SELECT scope,key FROM shared_content_cache WHERE state IN ('ready','failed') AND expires_at<CURRENT_TIMESTAMP
                    ORDER BY expires_at,scope,key LIMIT %s FOR UPDATE SKIP LOCKED
                ), available AS MATERIALIZED (
                    SELECT scope,key FROM candidates
                    WHERE pg_try_advisory_xact_lock(hashtextextended(%s || scope || ':' || key,0))
                ) DELETE FROM shared_content_cache p USING available a
                    WHERE p.scope=a.scope AND p.key=a.key AND p.state IN ('ready','failed') AND p.expires_at<CURRENT_TIMESTAMP""",
                (limit, shared_content_lock_namespace(self.schema)),
            ).rowcount
            return {'mode': 'applied', 'rows': deleted}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope', choices=['documents', 'company'], default='documents')
    parser.add_argument('--cache-older-than-days', type=int)
    parser.add_argument('--exact-table', action='append', default=[],
                        help='Explicitly scan this table for an exact count; may be repeated.')
    parser.add_argument('--apply-cache-cleanup', action='store_true')
    parser.add_argument('--shared-content-cache', action='store_true', help='Preview expired shared pure-content results.')
    parser.add_argument('--apply-shared-content-cleanup', action='store_true')
    parser.add_argument('--batch-size', type=int, default=100)
    args = parser.parse_args()
    if args.apply_cache_cleanup and args.cache_older_than_days is None:
        parser.error('Explicit --cache-older-than-days is required for cleanup.')
    if args.apply_shared_content_cleanup and not args.shared_content_cache:
        parser.error('Explicit --shared-content-cache is required for shared content cleanup.')
    if args.cache_older_than_days is not None and args.cache_older_than_days < 1:
        parser.error('Cache age must be at least one day.')
    prefix = 'EIP' if args.scope == 'documents' else 'COMPANY_AGENT'
    dsn = os.getenv(prefix + '_DATABASE_URL') or os.getenv('DATABASE_URL')
    schema = os.getenv(prefix + '_DB_SCHEMA', 'eip' if args.scope == 'documents' else 'company_agent')
    if not dsn:
        parser.error('A database connection must be explicitly configured.')
    if args.cache_older_than_days is not None and args.scope != 'documents':
        parser.error('Cache cleanup applies only to document analysis caches.')
    if args.shared_content_cache and args.scope != 'documents':
        parser.error('Shared content cache cleanup applies only to documents.')
    policy = Retention(dsn, schema)
    result = {
        'storage': policy.report(exact_tables=args.exact_table),
        'protected_data_policy': 'retain; archive using verified database/file backups',
    }
    if args.cache_older_than_days is not None:
        result['cache'] = policy.expired_cache(
            time.time() - args.cache_older_than_days * 86400, limit=args.batch_size, apply=args.apply_cache_cleanup
        )
    if args.shared_content_cache:
        result['shared_content_cache'] = policy.expired_shared_content(
            limit=args.batch_size, apply=args.apply_shared_content_cleanup,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
