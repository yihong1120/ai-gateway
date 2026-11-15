"""EIP flow-scoped semantics, independent physical exports, structured outcomes.

Group membership comes from server-owned EIP attachment handles, never the LLM.
Text and raw model responses are transient; only exports and safe errors survive.
"""
from __future__ import annotations


import re
import unicodedata

from examples.official_document_workflow.domain.calendar.contracts import GroupAnalysis
from examples.official_document_workflow.domain.calendar.presentation import (
    clean_ocr_presentation,
)


def metadata_text(pipe, value: str | None) -> str | None:
    """Compare presentation only; retain paragraph, word and numeric boundaries."""
    if value is None:
        return value
    # Native PDFs can encode 年/路 as canonically equivalent CJK glyphs 年/路.
    # NFC preserves semantic distinctions; do not use fuzzy matching or NFKC.
    text = pipe.reflow_ocr_layout_block_text(unicodedata.normalize('NFC', clean_ocr_presentation(value)))
    # Removing an OCR text wrapper can leave a layout gap after a CJK quote.
    # Keep digits, Latin words and paragraph breaks distinct.
    return re.sub(
        r'(?<=[\u3400-\u9fff「」『』（）])[ \t]+(?=[\u3400-\u9fff「」『』（）])',
        '',
        text,
    )


def parse_group_document(pipe,  payload: GroupAnalysis, sources, *, document_source=None):
    """Keep metadata validation separate from CSV validation; never relax evidence."""
    version = payload.get('calendar_contract_version')
    if version is True or version is False or version != 2 or str(version) != '2':
        raise ValueError('行事曆契約版本必須為 2，未使用舊格式判讀。')
    ids = set(sources)
    read_ids = payload.get('read_source_ids')
    if len(read_ids) != len(ids) or set(read_ids) != ids:
        raise ValueError('整組判讀未確認所有附件，請檢查；未使用部分內容冒充完整判讀。')
    primary = payload.get('primary_source_id')
    if payload.get('group_status') != 'related' or primary not in ids:
        raise ValueError('無法確認唯一主公文及附件關係，請人工確認或調整選取範圍。')
    data = payload['document']
    source = document_source if document_source is not None else sources[primary]
    # Same bounded presentation normalization on both the claimed
    # metadata and its source evidence. Do not rewrite letters/numbers
    # or replace literal source validation with semantic similarity.
    display_data = {k: metadata_text(pipe, v) for k, v in data.items() if k != 'evidence'}
    if data.get('evidence') is not None:
        display_data['evidence'] = {}
        for key, value in data['evidence'].items():
            display_data['evidence'][key] = metadata_text(pipe, value)
        # The formal subject comes from the literal quotation, not a second
        # model transcription. The existing validator still checks that quote
        # against this letter's main pages before it can name any output.
        display_data['subject'] = display_data['evidence'].get('subject')
    document = pipe.cloud_document_from_payload(
        display_data,
        metadata_text(pipe, source.text),
        pipe.valves.cloud_ocr_text_model,
        extraction_source='EIP 同一簽核附件組判讀',
        validate_calendar_description=False,
    )
    return primary, document


def parse_group(
    pipe,  payload: GroupAnalysis, sources, *, diagnostics=None,
    accepted_events=None, document_source=None,
):
    primary, document = parse_group_document(
        pipe,  payload, sources, document_source=document_source,
    )
    deadlines, rows, errors = pipe.calendar.partition(

        payload,
        sources,
        document,
        accepted_events if accepted_events is not None else [],
        estimate_relative_dates=getattr(pipe, '_estimate_relative_dates', False),
        estimate_tracking_reminders=getattr(pipe, '_eip_tracking_reminders', False),
        receipt_context=getattr(pipe, '_eip_receipt_context', None),
    )
    return primary, document, deadlines, rows, errors


def outcome(files, csv_ids, errors, notices, has_deadlines, warnings=None):
    warnings = list(warnings or [])
    status = 'partial' if errors and files else 'error' if errors else 'attention' if warnings else 'done'
    title = {
        'done': '公文處理完成',
        'attention': '文件處理完成，提醒待確認',
        'partial': '公文部分完成，請查看未完成項目',
        'error': '公文處理未完成',
    }[status]
    # Downloads are structured data, rendered once by the dashboard. Never
    # embed model/file names as Markdown or mistake an unmet request for success.
    summary = f"{title}。已產生 {len(files)} 個可下載檔案。"
    return {
        'status': status,
        'complete': not errors and not warnings,
        'files': files,
        'csv_ids': csv_ids,
        'has_deadlines': has_deadlines,
        'errors': errors,
        'notices': notices,
        'warnings': warnings,
        'summary': summary,
        'message': '\n'.join([summary, *notices, *warnings, *errors]),
    }
