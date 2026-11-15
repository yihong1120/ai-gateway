"""Stable document values and export contracts shared by every entry point."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Literal
from typing import Protocol

from examples.official_document_workflow.domain.calendar.csv_schema import CALENDAR_CSV_FORMAT  # noqa: F401 - public re-export
from examples.official_document_workflow.domain.calendar.csv_schema import CALENDAR_CSV_FORMAT_KEY  # noqa: F401 - public re-export
from examples.official_document_workflow.domain.workflow import JobPlan


class FileRecord(Protocol):
    id: str
    filename: str
    path: str
    meta: dict


logger = logging.getLogger(__name__)


INVALID_FILENAME_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*\x7f]+')


WHITESPACE = re.compile(r'\s+')


CJK_INLINE_SPACE = re.compile(r'(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])')


DOCX_INVALID_TEXT_CHARS = re.compile(r'[\x00-\x08\x0b\x0e-\x1f]')


DOCX_CONTENT_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'


DOCX_UNIFIED_FONT = 'DFKai-SB'


RASTER_LAYOUT_DOCX_DPI = 300


DOCX_EMU_PER_POINT = 12_700


IMAGE_SUFFIXES = frozenset({'.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif'})


IMAGE_CONTENT_TYPES = frozenset(
    {
        'image/jpeg',
        'image/jpg',
        'image/png',
        'image/webp',
        'image/heic',
        'image/heif',
        'image/heic-sequence',
        'image/heif-sequence',
    },
)


IMAGE_FORMATS = frozenset({'JPEG', 'PNG', 'WEBP', 'HEIF', 'HEIC'})


CALENDAR_PROJECT_CATEGORIES = (
    '01-代辦國道3號古坑交流道平面側車道工程',
    '01-台18線70K 100等2處災害明隧道修復工程',
)


class ProcessingError(ValueError):
    """An expected, user-safe document processing error."""


class OutputNotWritten(ProcessingError):
    """The output failed before any externally visible write began."""


class OutputUncertain(ProcessingError):
    """An output may have committed; reconciliation is required before retry."""


class CloudTemporaryFailure(ProcessingError):
    """Temporary model availability failure; defer reads, never replay writes."""


class CloudRateLimited(CloudTemporaryFailure):
    """Explicit HTTP 429 rejection; may be deferred, never a write retry."""


class CloudExtractionError(RuntimeError):
    """A failure of the required, source-validated cloud extraction."""


@dataclass(frozen=True)
class DocumentDate:
    roc: str
    compact_roc: str
    gregorian: str


@dataclass(frozen=True)
class OfficialDocument:
    issued: DocumentDate
    reference_number: str
    # Source-faithful title used by the renamed PDF, DOCX, metadata, and the
    # visible document summary.  It is never filled from CSV-only synthesis.
    subject: str
    recipient: str
    extraction_source: str
    handler_name: str = '未載明'
    handling_unit: str = '未載明'
    contact_phone: str = '未載明'
    contact_email: str = '未載明'
    sender_address: str = '未載明'
    primary_recipients: str = '未載明'
    # An optional source-faithful, self-contained phrase selected by the one
    # cloud extraction for filesystem-length-constrained output names.  It
    # never alters the formal subject displayed in the response or CSV.
    filename_subject: str = ''
    explanation: str = ''
    generated_description: str = ''
    # A cloud-generated, evidence-backed semantic title is deliberately a
    # calendar-only value.  It cannot alter the document files or names.
    calendar_subject: str = ''


@dataclass(frozen=True)
class DocumentTime:
    display: str
    twenty_four_hour: str


@dataclass(frozen=True)
class Deadline:
    date: DocumentDate
    time: DocumentTime | None
    action: str
    sentence: str
    responsible_party: str
    category: str = ''
    # The cloud model makes semantic choices after reading the full document.
    # They remain optional because an ambiguous document must leave CSV fields
    # blank or use the organisation defaults.
    end_date: DocumentDate | None = None
    end_time: DocumentTime | None = None
    all_day_event: Literal['真', '假', ''] = ''
    private: Literal['真', '假', ''] = ''
    priority: Literal['低', '普通', '高', ''] = ''
    completed: Literal['0', '1', ''] = ''
    # Event-specific LLM summary and source-resolved context. Never used to
    # rename source documents or to select the EIP owner.
    calendar_title: str = ''
    calendar_description: str = ''
    tentative_schedule: dict[str, str] | None = None


@dataclass(frozen=True)
class CloudDocumentExtraction:
    """The result of the one required source-bounded cloud extraction."""

    model: str
    document: OfficialDocument
    deadlines: tuple[Deadline, ...]


@dataclass(frozen=True)
class CloudSemanticSource:
    """A complete, transient local document reading for cloud semantics.

    It retains page and OCR-layout boundaries so the cloud model can distinguish
    a visual A4 line wrap from a real paragraph boundary.  It is never saved as
    a user download or used as a replacement for the positioned DOCX layout.
    """

    text: str
    kind: Literal['native_pdf_text', 'paddle_layout_ocr']
    # 1-based lines inserted by the local source builder, never inferred from
    # document/model text. Literal tag-looking document lines are not included.
    structural_lines: frozenset[int] = frozenset()


@dataclass
class PreparedDocument:
    """A verified PDF, including a temporary PDF made from Office files or photos.

    Image uploads are deliberately represented as one generated PDF before
    OCR and cloud extraction. Pages use a natural, case-insensitive filename
    order, which produces one billable cloud request and prevents a single
    multi-photo letter from becoming several unrelated calendar exports.
    """

    kind: Literal['pdf', 'office', 'images']
    source_files: tuple[FileRecord, ...]
    pdf_path: Path
    image_count: int = 0
    temporary_directory: Any | None = None
    original_path: Path | None = None

    @property
    def source_file(self) -> FileRecord:
        return self.source_files[0]

    @property
    def is_original_pdf(self) -> bool:
        return self.kind == 'pdf'

    def cleanup(self) -> None:
        if self.temporary_directory is not None:
            try:
                self.temporary_directory.cleanup()
            except Exception:
                logger.warning('Could not clean up temporary document files')


@dataclass(frozen=True)
class OcrLayoutBlock:
    """One recognised OCR block in the source page's pixel coordinate space."""

    text: str
    bbox: tuple[float, float, float, float]
    # Structural region labels come from PaddleOCR-VL itself.  They are used
    # only to keep obvious marginal stamps/signatures in the raster underlay;
    # no document wording is classified or filtered locally.
    label: str = ''


@dataclass(frozen=True)
class OcrLayoutPage:
    """OCR blocks and its rendered page dimensions, in source-page order."""

    width: float
    height: float
    blocks: tuple[OcrLayoutBlock, ...]


@dataclass
class ProcessedDocument:
    """One successfully exported source document within a request.

    A multi-PDF request deliberately owns one instance per PDF.  This keeps
    cloud extraction, OCR processing, output files and calendar evidence scoped
    to their original source instead of allowing fields from two letters to be
    mixed together.
    """

    prepared_source: PreparedDocument
    document: OfficialDocument
    deadlines: tuple[Deadline, ...]
    output_pdf: FileRecord | None = None
    output_pdf_name: str = ''
    docx_file: FileRecord | None = None
    docx_name: str = ''
    docx_label: str = '可編輯 DOCX（統一標楷體）'
    searchable_pdf_file: FileRecord | None = None
    searchable_pdf_name: str = ''


@dataclass(frozen=True)
class PendingCalendarReminder:
    """Only the minimum source-verified state needed for one follow-up.

    No PDF, image, OCR text, cloud request/response, EIP credential, or
    earlier chat content is retained.  The data is encrypted in the PostgreSQL
    continuation repository and is re-authorised against the original file
    ownership before it can create a replacement CSV.
    """

    calendar_rows: tuple[dict[str, str], ...]
    job_plan: JobPlan
    selected_deadline_indices: tuple[tuple[int, int], ...]
    unresolved_operation_index: int
    source_file_ids: tuple[str, ...]
    csv_base_name: str
    batch_mode: bool


class ProcessingText:
    """Small helper kept outside Pipe so parsing code remains readable."""

    @staticmethod
    def clean(value: str) -> str:
        return CJK_INLINE_SPACE.sub('', WHITESPACE.sub(' ', value)).strip(' ，、。；:：')
