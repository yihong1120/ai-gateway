"""Inspect paid-analysis metadata; release only a proven not-started reservation."""
from __future__ import annotations

import argparse
import json

from examples.shared.config import load_service
from examples.official_document_workflow.cli.output_receipts import existing_store
from examples.official_document_workflow.infrastructure.storage.analysis_results import AnalysisResults


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    listing = commands.add_parser('list', help='Only metadata; never source text or model responses.')
    listing.add_argument('--state', choices=['ready', 'failed', 'unknown'])
    listing.add_argument('--limit', type=int, default=20, choices=range(1, 101))
    resolve = commands.add_parser('confirm-not-started', help='Only after proving the provider call never started; does not run a model.')
    for key in ['scope', 'key', 'actor', 'reason']:
        resolve.add_argument('--' + key, required=True)
    args = parser.parse_args(argv)
    load_service('webui')
    store = existing_store()  # Never initialize keys or migrate an operator read.
    if args.command == 'list':
        with store.db() as db:
            rows = db.execute("""SELECT scope,key,state,created_at FROM document_analysis_results
                WHERE (%s::text IS NULL OR state=%s) ORDER BY created_at DESC,scope,key LIMIT %s""",
                              (args.state, args.state, args.limit)).fetchall()
        result = [dict(row) for row in rows]
    else:
        AnalysisResults(store, args.scope).confirm_not_started(args.key, actor=args.actor, reason=args.reason)
        result = {'reconciled': True, 'model_called': False}
    print(json.dumps(result, default=str, ensure_ascii=False))


if __name__ == '__main__':
    main()
