"""Persisted document settings, shared by every entry point."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from pydantic import Field


class DocumentSettings(BaseModel):
    calendar_ai_preparation_reminders: bool = Field(
        default=True,
        description='Chat document CSV: include AI-suggested preparation tasks before verified events, with suggested start/end times and work hours. Does not enable EIP writes or notifications.',
    )
    eip_large_sources: bool = Field(
        default=False,
        description='EIP full-source chunking; total PDF bytes/pages/text are not rejected by legacy upload caps. Per-request OCR/model resource bounds remain.',
    )
    eip_source_window_characters: int = Field(
        default=12000,
        ge=2000,
        le=80000,
        description='Transport size of one EIP evidence-index request, not a total source-text limit.',
    )
    rename_original_attachment: bool = Field(
        default=False,
        description='For one PDF, when false preserve the source attachment and create a renamed PDF copy. Batch PDF mode always preserves originals and creates derived copies.',
    )
    max_pdf_size_mb: int = Field(default=30, ge=1, le=100)
    max_ocr_pages: int = Field(
        default=10,
        ge=1,
        le=30,
        description='Maximum pages per local OCR request, not per source PDF.',
    )
    max_pdf_pages: int = Field(
        default=60,
        ge=1,
        le=100,
        description='Maximum complete source PDF pages; long sources use bounded OCR chunks without truncation.',
    )
    ocr_chunk_pages: int = Field(
        default=3,
        ge=1,
        le=10,
        description='Sequential local OCR chunk size; also bounded by max_ocr_pages.',
    )
    max_batch_pdf_count: int = Field(
        default=10,
        ge=1,
        le=10,
        description='Maximum combined PDF, ODT, DOC and DOCX uploads in one batch. Conversion and OCR run serially; document grouping and analysis are shared.',
    )
    max_batch_pdf_total_size_mb: int = Field(
        default=90,
        ge=1,
        le=300,
        description='Maximum combined document bytes, counting the larger of each original and normalized PDF. max_pdf_size_mb remains the per-file limit.',
    )
    max_batch_pdf_total_pages: int = Field(
        default=100,
        ge=1,
        le=100,
        description='Maximum combined PDF pages after Office conversion. max_pdf_pages remains the per-document limit.',
    )
    max_image_count: int = Field(
        default=10,
        ge=1,
        le=30,
        description='Maximum photos accepted as pages of one document. They use natural, case-insensitive filename order.',
    )
    max_image_total_size_mb: int = Field(
        default=30,
        ge=1,
        le=100,
        description='Maximum combined source-image bytes before conversion to a temporary PDF.',
    )
    max_image_pixels_per_image: int = Field(
        default=20_000_000,
        ge=1_000_000,
        le=50_000_000,
        description='Maximum decoded pixels in each source photo to prevent image decompression exhaustion.',
    )
    max_image_total_pixels: int = Field(
        default=80_000_000,
        ge=1_000_000,
        le=100_000_000,
        description='Maximum decoded pixels across all source photos to prevent image decompression exhaustion.',
    )
    # Keep these historic Valve field names so existing Open WebUI
    # function configuration remains compatible with the official
    # PaddleOCR-VL GPU service.
    paddleocr_vl_base_url: str = Field(
        default='http://127.0.0.1:8113',
        description='Loopback-only official PaddleOCR-VL GPU service base URL. Only http://127.0.0.1:<port> is accepted.',
    )
    paddleocr_vl_timeout_seconds: int = Field(default=120, ge=15, le=900)
    paddleocr_vl_max_new_tokens: int = Field(
        default=4096,
        ge=256,
        le=8192,
        description='Maximum generation tokens for each PaddleOCR-VL recognition block. Keep 4096 unless a tested document needs a different limit.',
    )
    cloud_ocr_text_timeout_seconds: int = Field(
        default=120,
        ge=30,
        le=300,
        description='Maximum total time for the required OCR/native-text cloud analysis.',
    )
    cloud_ocr_text_max_input_characters: int = Field(
        default=80_000,
        ge=2_000,
        le=200_000,
        description='Maximum complete geometry-preserved OCR/native-text source accepted by the cloud path. Sources are never truncated.',
    )
    cloud_ocr_text_max_output_tokens: int = Field(
        default=3_000,
        ge=512,
        le=6_000,
        description='Maximum structured JSON output tokens for the primary OCR/native-text cloud analysis.',
    )
    document_analysis_cache_enabled: bool = Field(
        default=True,
        description='Reuse validated document checkpoints and shared OCR, evidence and editable Word content across upload and EIP entries for identical inputs and settings. Disable for fresh analysis and rendering.',
    )
    cloud_ocr_text_model: Literal[
        'gemini-3.5-flash-lite',
        'gemini-3.8-flash',
        'gemini-4-argon',
        'Kimi-K2.5',
        'Kimi-K2.6',
        'grok-4.6',
        'DeepSeek-V4-Flash',
        'DeepSeek-V4-Pro',
    ] = Field(
        default='gemini-3.8-flash',
        description='Approved text-only model for source-validated document fields and CSV. It receives no PDF/image bytes.',
    )
    cloud_ocr_text_reasoning_effort: Literal['low', 'medium', 'high'] = Field(
        default='low',
        description='Gemini thinking level, passed through LiteLLM. Applies to document analysis, grouping and calendar review.',
    )
    calendar_reconciliation_model: Literal[
        'gemini-3.5-flash-lite',
        'gemini-3.8-flash',
        'gemini-4-argon',
    ] = Field(
        default='gemini-3.8-flash',
        description='Approved model for comparing visible owned/shared EIP events and revised notices. Independent of document extraction; no fallback or external write authority.',
    )
    cloud_max_events: int = Field(
        default=20,
        ge=1,
        le=20,
        description='Maximum source-traceable calendar events accepted from one document.',
    )
    calendar_default_private: Literal['真', '假', ''] = Field(
        default='假',
        description='Calendar-import default for a newly generated record. This is an organisation setting, not OCR-derived document content.',
    )
    calendar_default_priority: Literal['低', '普通', '高', ''] = Field(
        default='普通',
        description='Calendar-import default for a newly generated record. This is an organisation setting, not OCR-derived document content.',
    )
    calendar_default_completed: Literal['0', '1', ''] = Field(
        default='0',
        description='Calendar-import default for a newly generated record. Use 0 for an uncompleted new task.',
    )
    instruction_planning_enabled: bool = Field(
        default=True,
        description='Use the controlled LangGraph planner on only the latest user message. It cannot read prior chat history or document text.',
    )
    instruction_plan_timeout_seconds: int = Field(
        default=30,
        ge=10,
        le=120,
        description='Maximum time for the bounded user-instruction planning request.',
    )
    instruction_plan_max_input_characters: int = Field(
        default=8_000,
        ge=100,
        le=20_000,
        description='Maximum characters from the latest user message accepted by the planning Agent; no chat history is sent.',
    )
    instruction_plan_max_output_tokens: int = Field(
        default=800,
        ge=256,
        le=2_000,
        description='Maximum structured JSON output tokens for the controlled instruction planner.',
    )
    instruction_plan_model: Literal[
        'Kimi-K2.5',
        'Kimi-K2.6',
        'grok-4.6',
        'DeepSeek-V4-Flash',
        'DeepSeek-V4-Pro',
    ] = Field(
        default='DeepSeek-V4-Flash',
        description='Approved text-only model for the bounded instruction planner. It receives only the latest user message.',
    )
    pending_clarification_ttl_seconds: int = Field(
        default=900,
        ge=60,
        le=3600,
        description='How long an ambiguous verified-deadline reminder can be completed in the same chat without re-uploading. State is memory-only and cleared on restart.',
    )
    pending_clarification_max_payload_bytes: int = Field(
        default=1_048_576,
        ge=65_536,
        le=2_097_152,
        description='Maximum UTF-8 bytes for one short-lived verified-calendar snapshot. Larger snapshots are not retained in memory for follow-up choices.',
    )
