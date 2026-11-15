"""Keep the exact source/line provenance paired with durable document analysis."""
from __future__ import annotations

import hashlib
from copy import copy

from examples.shared.runtime.io import run_sync
from examples.official_document_workflow.domain.documents import CloudSemanticSource
from .ocr import OCR_CONTENT_REVISION, NATIVE_TEXT_REVISION, native_text_tool_version, validate_native_source


async def read_source(pipe, prepared):
    async def read(reader):
        pages, response = await reader.ocr.ocr_context(prepared.pdf_path)
        source = await run_sync(reader.semantic_source, prepared.pdf_path, pages, response)
        return {'text': source.text, 'kind': source.kind, 'structural_lines': sorted(source.structural_lines),
                'spotting_required': bool(pages)}

    registry = getattr(pipe, 'analysis_results', None)
    if registry is None:
        value = await read(pipe)
        return CloudSemanticSource(value['text'], value['kind'], frozenset(value['structural_lines'])), value['spotting_required']
    else:
        # Admission limits remain per recipient, including on archive hits.
        await run_sync(pipe.ocr._validate_pdf_page_count, prepared.pdf_path)

        def fingerprint():
            with (prepared.original_path or prepared.pdf_path).open('rb') as source:
                return hashlib.file_digest(source, 'sha256').hexdigest()

        key = registry.key('source-reading-v1', await run_sync(fingerprint), OCR_CONTENT_REVISION,
                           NATIVE_TEXT_REVISION, await run_sync(native_text_tool_version), *pipe.ocr.content_identity())

        def validate(value):
            validate_native_source(value)
            if value.get('kind') not in {'native_pdf_text', 'paddle_layout_ocr'} or type(value.get('spotting_required')) is not bool:
                raise ValueError('公文原文判讀依據格式不符。')

        async def complete_reading():
            # Pure reading is shared before each recipient's text-size check.
            # A stricter recipient cannot publish a truncated source or poison
            # the full reading needed by another authorized recipient.
            reader = copy(pipe)
            reader.valves = reader.valves.model_copy(update={'eip_large_sources': True})
            return await read(reader)

        value = await registry.aget_or_compute(key, complete_reading, validate=validate, replay_safe=True)
    text = pipe.ocr._bounded_cloud_semantic_text(value['text'])
    if not text:
        raise ValueError('公文原文判讀依據不完整。')
    return CloudSemanticSource(text, value['kind'], frozenset(value['structural_lines'])), value['spotting_required']
