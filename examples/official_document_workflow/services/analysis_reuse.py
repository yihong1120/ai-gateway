"""Share source-derived model calls; keep instructions and personal calendars private."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os

from jsonschema import Draft202012Validator

from examples.shared.runtime.io import run_sync
from examples.shared.runtime.concurrency import ModelNotStarted
from .output_delivery import output_job_identity


class DeclinedAnalysis(ModelNotStarted):
    """A proven pre-call denial/429; preserve its original caller-facing error."""

    def __init__(self, original):
        self.original = original
        super().__init__('Analysis did not start.')


def neutral_event(event):
    """Source duty/specification only; calculated recipient dates stay local."""
    from examples.official_document_workflow.domain.calendar.relative_dates import RelativeDateProof
    from examples.official_document_workflow.domain.calendar.tracking_reminders import TrackingReminderProof

    result = {key: deepcopy(value) for key, value in event.items() if not key.startswith('_')}
    if isinstance(event.get('_relative_date_proof'), RelativeDateProof) or isinstance(event.get('_tracking_reminder_proof'), TrackingReminderProof):
        result.update(start_date=None, end_date=None, tentative_schedule=None)
        result['assignment'] = {**result.get('assignment', {}), 'anchor_date': None}
    return result


def document_revisions(members):
    """Exact original bytes, independent of private upload IDs and filenames."""
    result = []
    for member in members:
        with (member.original_path or member.pdf_path).open('rb') as source:
            result.append(hashlib.file_digest(source, 'sha256').hexdigest())
    return result


async def source_revisions(pipe, members):
    if getattr(pipe, 'analysis_results', None) is None:
        return ()
    return await run_sync(document_revisions, members)


def content_request(source):
    """Remove private display filenames from model input, keeping source coordinates."""
    try:
        data = json.loads(source.text)
    except (TypeError, ValueError):
        return source
    if not isinstance(data, dict):
        return source
    for entry in data.get('sources', []):
        if isinstance(entry, dict) and 'name' in entry and isinstance(entry.get('source_id'), str):
            entry['name'] = entry['source_id'] + '.pdf'
    return type(source)(text=json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')), kind=source.kind)


async def analyze_content(pipe, source, *, stage, prompt, schema, max_output_tokens=None, revisions=()):
    """Call only with source facts, never instructions, login data or remote matches.

    JSON shape is checked before publication. Every consumer still validates
    literal evidence and recipient-specific dates before exporting or writing.
    """
    registry = getattr(pipe, 'analysis_results', None)
    kwargs = {'prompt': prompt, 'schema': schema}
    if max_output_tokens is not None:
        kwargs['max_output_tokens'] = max_output_tokens
    if registry is None:
        return await pipe.analyze_text(source, **kwargs)
    authorize = getattr(pipe, '_analysis_authorize', None)
    if authorize is not None:
        await authorize()  # Every recipient, including hits, must retain access.
    source = content_request(source)
    identity = getattr(pipe, '_analysis_model_identity', None)
    if identity is None:
        # Routing/settings identify semantics; provider credentials are checked
        # by the actual transport only if a new model invocation is required.
        identity = ('document-service', os.getenv('OFFICIAL_DOCUMENT_LITELLM_BASE_URL', 'http://127.0.0.1:4000/v1').strip().rstrip('/'),
                    pipe.valves.cloud_ocr_text_model, pipe.valves.cloud_ocr_text_reasoning_effort,
                    pipe.valves.cloud_ocr_text_max_output_tokens)
    key = registry.key(stage, list(revisions), source.kind, source.text, prompt, schema, max_output_tokens, identity)
    scope = output_job_identity()
    owner = getattr(pipe, '_usage_owner', None)
    job = scope[1] if scope and scope[0] == owner else ''

    def validate(result):
        if not isinstance(result, dict):
            raise ValueError('公文判讀回應必須為物件。')
        Draft202012Validator(schema).validate(result)

    async def compute():
        # The reservation already exists. Preserve its initiating task even
        # when the provider outcome subsequently becomes unknown.
        await run_sync(registry.record_use, key, owner, job)
        if authorize is not None:
            try:
                await authorize()  # Access may have changed while waiting.
            except Exception as error:
                raise DeclinedAnalysis(error) from error
        try:
            return await pipe.analyze_text(source, **kwargs)
        except getattr(pipe, '_analysis_declined_errors', ()) as error:
            raise DeclinedAnalysis(error) from error

    try:
        result = await registry.aget_or_compute(key, compute, validate=validate, replay_safe=False)
    except DeclinedAnalysis as error:
        raise error.original from error
    await run_sync(registry.record_use, key, owner, job)
    return deepcopy(result)
