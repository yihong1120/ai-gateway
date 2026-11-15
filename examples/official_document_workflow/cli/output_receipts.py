"""Inspect and reconcile output receipts even after a conversation expires."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit

import httpx
from examples.shared.runtime.database import application_database_pools
from examples.official_document_workflow.application.output_review import WebUIOutputVerifier
import psycopg
from psycopg import sql
from examples.official_document_workflow.infrastructure.storage.automation_store import AutomationStore, key_file
from examples.official_document_workflow.infrastructure.storage.database import configuration
from examples.official_document_workflow.infrastructure.storage.migrations import VERSION
from examples.official_document_workflow.infrastructure.storage.output_receipts import PostgresOutputReceipts


def _token(path):
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError('The token file must be a private 0600 regular file.')
        token = stream.read(32769).strip()
    if not token or len(token) > 32768 or any(c.isspace() for c in token):
        raise ValueError('The token file must contain one bearer token.')
    return token


def _origin(value):
    parsed = urlsplit(value)
    canonical = 'http://127.0.0.1' + (f':{parsed.port}' if parsed.port is not None else '')
    if value != canonical or (parsed.port is not None and not 1 <= parsed.port <= 65535):
        raise ValueError('Use the local loopback WebUI origin without a path.')
    return value


def existing_store():
    key = key_file()
    if not key.is_file() or key.is_symlink():
        raise ValueError('An existing private automation key is required.')
    dsn, schema = configuration()
    with psycopg.connect(dsn, connect_timeout=5) as db:
        exists = db.execute('SELECT to_regclass(%s)', (schema + '.schema_migrations',)).fetchone()[0]
        if not exists:
            raise ValueError('Initialize the document service before using receipt tools.')
        version = db.execute(sql.SQL('SELECT max(version) FROM {}.schema_migrations').format(sql.Identifier(schema))).fetchone()[0]
        if version != VERSION:
            raise ValueError('Upgrade the document service before using receipt tools.')
    return AutomationStore(key)


async def execute(args):
    async with application_database_pools():
        backend = await asyncio.to_thread(existing_store)
        repository = PostgresOutputReceipts(lambda: backend)
        if args.command == 'list':
            return await asyncio.to_thread(repository.list, args.owner, state=args.state, limit=args.limit)
        record = await asyncio.to_thread(repository.get, args.output_id, args.owner)
        if record is None:
            raise ValueError('No receipt exists for this owner and output ID.')
        if args.command == 'inspect':
            return {'receipt': asdict(record),
                    'verification': await asyncio.to_thread(repository.review_status, record), 'reviews': await asyncio.to_thread(repository.reviews, record.id, record.owner)}
        if args.command == 'annotate':
            review = await asyncio.to_thread(repository.record_review, record, actor=args.actor, method='manual',
                                             outcome=args.outcome, note=args.note)
            return {'output_id': record.id, 'review_id': review, 'state': record.state, 'write_replayed': False}
        origin, token = _origin(args.webui_url), _token(args.token_file)
        async with httpx.AsyncClient(base_url=origin, headers={'Authorization': 'Bearer ' + token},
                                    trust_env=False, follow_redirects=False, timeout=10) as client:
            verification = await WebUIOutputVerifier(client).verify(record)
        result = {'output_id': record.id, **asdict(verification), 'applied': False, 'write_replayed': False}
        if args.command == 'reconcile':
            result['review_id'] = await asyncio.to_thread(repository.record_review, record,
                actor=verification.actor, method='webui_sha256', outcome=verification.outcome, proof=verification.proof)
            result['applied'] = verification.outcome == 'verified'
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('list','inspect','verify','reconcile','annotate'):
        item = commands.add_parser(command)
        item.add_argument('--owner', required=True)
        if command == 'list':
            item.add_argument('--state', choices=('writing','unknown','not_written','stored','deleting','delete_unknown','deleted'))
            item.add_argument('--limit', type=int, default=50, choices=range(1,101))
        else:
            item.add_argument('--output-id', required=True)
        if command in {'verify','reconcile'}:
            item.add_argument('--webui-url', required=True)
            item.add_argument('--token-file', type=Path, required=True)
        if command == 'annotate':
            item.add_argument('--actor', required=True)
            item.add_argument('--outcome', choices=('needs_review','reported_missing','investigating'), default='needs_review')
            item.add_argument('--note', required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(execute(args))
    except Exception as failure:
        # Do not echo HTTP error bodies, bearer tokens or database credentials.
        parser.exit(1, f'Output receipt operation failed ({type(failure).__name__}).\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
