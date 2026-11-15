"""Opt-in real owned-PDF evaluation. Local artifacts only; never commit to EIP.

Use the service environment, then run with --inventory PRIVATE_JSON --output
NEW_PRIVATE_DIRECTORY. Inventory entries contain document_id, owner and
payload.source_ids. A temporary provider failure stops the whole batch.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from dataclasses import asdict
from functools import partial
from pathlib import Path

from langsmith import tracing_context

from examples.official_document_workflow.application.multi_agent import DocumentAgent
from examples.shared.runtime.modules import ModuleReference

_workflow_integration = ModuleReference('examples.official_document_workflow.application.integration')


class RequestCapture:
    def __init__(self, pipe, folder):
        self.original = pipe.analyze_text
        self.original_text = pipe._responses_output_text
        self.original_json = pipe._cloud_json_object
        pipe._responses_output_text = self.response_text
        pipe._cloud_json_object = self.response_json
        self.folder = folder
        self.calls = 0

    def response_text(self, body):
        # Private diagnostic artifacts: omit credentials, IDs and source bytes.
        metadata = {key: body.get(key) for key in ('model', 'status', 'incomplete_details', 'usage')}
        (self.folder / f"transport-{self.calls}.json").write_text(json.dumps(metadata, ensure_ascii=False))
        return self.original_text(body)

    def response_json(self, text):
        (self.folder / f"raw-output-{self.calls}.txt").write_text(text)
        return self.original_json(text)

    async def __call__(self, source, *, prompt):
        self.calls += 1
        if self.calls > 5:
            raise RuntimeError('Evaluation request cap reached')
        (self.folder / f"request-{self.calls}.json").write_text(
            json.dumps({'prompt': prompt, 'source': source.text}, ensure_ascii=False),
        )
        try:
            result = await self.original(source, prompt=prompt)
        except Exception as exc:
            cause = exc.__cause__
            diagnostic = {
                'type': type(exc).__name__,
                'cause_type': type(cause).__name__,
            }
            if isinstance(cause, json.JSONDecodeError):
                diagnostic.update(json_position=cause.pos, output_characters=len(cause.doc))
            (self.folder / f"failure-{self.calls}.json").write_text(json.dumps(diagnostic))
            raise
        (self.folder / f"response-{self.calls}.json").write_text(json.dumps(result, ensure_ascii=False))
        return result


async def evaluate_document(record, folder, *, model=None):
    configured_pipe = _workflow_integration.attribute('configured_pipe')

    pipe = await configured_pipe()
    if model:
        pipe.valves = type(pipe.valves)(**{**pipe.valves.model_dump(), 'cloud_ocr_text_model': model})
    ids = record['payload']['source_ids']
    kind, owned = await pipe.owned_sources([{'id': fid, 'type': 'file'} for fid in ids], record['owner'])
    prepared = await pipe.prepare_owned_source_documents(kind, owned)
    capture = RequestCapture(pipe, folder)
    pipe.analyze_text = capture
    try:
        manifest = [
            {
                'id': p.source_file.id,
                'sha256': hashlib.sha256(p.pdf_path.read_bytes()).hexdigest(),
            }
            for p in prepared
        ]
        (folder / 'sources.json').write_text(json.dumps(manifest))
        with tracing_context(enabled=False):
            state = await DocumentAgent(pipe, None).run(list(prepared), 1, 1)
        result = {
            key: state.get(key)
            for key in (
                'analysis_status',
                'analysis_attempts',
                'errors',
                'csv_errors',
                'metadata_notices',
                'rows',
                'payload',
                'accepted_events',
                'pending_events',
                'repair_history',
                'semantic_review',
                'deferred',
            )
        }
        result.update(
            document_id=str(record['document_id']),
            calls=capture.calls,
            model=pipe.valves.cloud_ocr_text_model,
            document=asdict(state['document']) if state.get('document') else None,
        )
        for sid, source in state.get('sources', {}).items():
            (folder / f"{sid}.txt").write_text(source.text)
        if state.get('rows'):
            (folder / 'calendar.csv').write_bytes(pipe.calendar_csv(state['rows']))
        (folder / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    finally:
        for item in prepared:
            item.cleanup()


async def run_batch(records, output, evaluate=evaluate_document):
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    keys = [str(record['document_id']) for record in records]
    if len(set(keys)) != len(keys) or any(not key.isdecimal() for key in keys):
        raise ValueError('Unique numeric document IDs required')
    repo = Path(__file__).resolve().parents[1]
    paths = [
        *sorted((repo / 'examples/official_document_workflow').glob('**/*.py')),
        repo / 'examples/openwebui/functions/official_document_processor_pipe.py',
    ]
    (output / 'code-hashes.json').write_text(
        json.dumps(
            {str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
            indent=2,
        ),
    )
    results = []
    for record, key in zip(records, keys):
        folder = output / key
        folder.mkdir(mode=0o700)
        result = await evaluate(record, folder)
        results.append(result)
        # This is execution accounting, not a semantic pass grade.
        summary = {
            'attempted': [r['document_id'] for r in results],
            'unexecuted': keys[len(results):],
            'provider_deferred': bool(result.get('deferred')),
        }
        (output / 'execution.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        print(
            json.dumps(
                {
                    'document_id': key,
                    'calls': result.get('calls'),
                    'deferred': summary['provider_deferred'],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if summary['provider_deferred']:
            break
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cases', help='Comma-separated document IDs; default all inventory entries')
    parser.add_argument(
        '--model',
        help='Optional approved-model override for this private evaluation only',
    )
    args = parser.parse_args()
    os.umask(0o077)
    records = json.loads(args.inventory.read_text())
    if args.cases:
        wanted = set(args.cases.split(','))
        records = [r for r in records if str(r['document_id']) in wanted]
        if {str(r['document_id']) for r in records} != wanted:
            parser.error('Unknown case IDs')

    results = asyncio.run(run_batch(records, args.output, partial(evaluate_document, model=args.model)))
    return int(any(r.get('deferred') or r.get('errors') for r in results))


if __name__ == '__main__':
    raise SystemExit(main())
