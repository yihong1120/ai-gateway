"""title: 公文 PDF／Word／ODT／圖片批次：附件拆分、可編輯 DOCX 與行事曆 CSV（雲端 LLM）
author: Visionnaire
version: 3.16.0
required_open_webui_version: 0.11.1
description: Processes up to 10 mixed PDF, ODT, DOC and DOCX files, or naturally sorted photos of one document. Office uploads use the existing isolated PDF converter before the shared document workflow. Original uploads are preserved. Main letters and attachments have separate PDF and Word downloads named for each parent letter; identical pages reuse PDF splitting and Word rendering within the batch. Internal OCR supports source-validated cloud analysis and editable layout DOCX. Calendar CSV contains verified events and suggested preparation reminders. Background tasks never process uploads."""
from __future__ import annotations

import logging
import tempfile  # noqa: F401 - adapter contract
from typing import Any

import httpx  # noqa: F401 - adapter contract
from open_webui.models.files import FileForm  # noqa: F401 - public re-export
from open_webui.models.files import FileModel  # noqa: F401 - public re-export
from open_webui.models.files import Files  # noqa: F401 - public re-export
from open_webui.storage.provider import Storage  # noqa: F401

from examples.official_document_workflow.application.pending import PendingClarificationScope  # noqa: F401 - public re-export
from examples.official_document_workflow.application.prompts import INSTRUCTION_PLANNER_PROMPT  # noqa: F401 - public re-export
from examples.official_document_workflow.domain.calendar.csv_schema import CALENDAR_CSV_FORMAT  # noqa: F401 - public re-export
from examples.official_document_workflow.domain.calendar.csv_schema import CALENDAR_CSV_FORMAT_KEY  # noqa: F401 - public re-export
from examples.official_document_workflow.domain.calendar.csv_schema import CALENDAR_HEADERS  # noqa: F401 - public re-export
from examples.official_document_workflow.domain.calendar.csv_schema import CALENDAR_TRANSLATION_ROW  # noqa: F401 - public re-export
from examples.official_document_workflow.domain.documents import CloudDocumentExtraction  # noqa: F401 - public re-export
from examples.official_document_workflow.domain.documents import CloudExtractionError  # noqa: F401 - public re-export
from examples.official_document_workflow.domain.documents import CloudRateLimited  # noqa: F401 - public re-export
from examples.official_document_workflow.domain.documents import CloudSemanticSource  # noqa: F401 - public re-export
from examples.official_document_workflow.domain.documents import CloudTemporaryFailure  # noqa: F401 - public re-export
from examples.official_document_workflow.domain.documents import Deadline  # noqa: F401 - public re-export
from examples.official_document_workflow.domain.documents import DocumentDate  # noqa: F401 - public re-export
from examples.official_document_workflow.domain.documents import DocumentTime  # noqa: F401 - public re-export
from examples.official_document_workflow.domain.documents import OcrLayoutBlock  # noqa: F401 - public re-export
from examples.official_document_workflow.domain.documents import OcrLayoutPage  # noqa: F401 - public re-export
from examples.official_document_workflow.domain.documents import OfficialDocument  # noqa: F401 - public re-export
from examples.official_document_workflow.domain.documents import PendingCalendarReminder  # noqa: F401 - public re-export
from examples.official_document_workflow.domain.documents import PreparedDocument  # noqa: F401 - public re-export
from examples.official_document_workflow.domain.documents import ProcessedDocument  # noqa: F401 - public re-export
from examples.official_document_workflow.domain.documents import ProcessingError  # noqa: F401 - public re-export
from examples.official_document_workflow.domain.documents import ProcessingText  # noqa: F401 - public re-export
from examples.official_document_workflow.domain.workflow import JobPlan  # noqa: F401 - public re-export
from examples.official_document_workflow.domain.workflow import OutputSelection  # noqa: F401 - public re-export
from examples.official_document_workflow.domain.workflow import PendingReminderResolution  # noqa: F401 - public re-export
from examples.shared.runtime.concurrency import document_request
from examples.shared.runtime.io import run_sync
from examples.shared.runtime.modules import ModuleReference

_workflow_webui = ModuleReference('examples.official_document_workflow.adapters.webui')
_workflow_documents = ModuleReference('examples.official_document_workflow.domain.documents')
_workflow_processor = ModuleReference('examples.official_document_workflow.services.processor')

logger = logging.getLogger(__name__)  # noqa: F401 - WebUI module contract
INVALID_FILENAME_CHARS = _workflow_documents.attribute('INVALID_FILENAME_CHARS')  # noqa: F401 - WebUI module contract
WHITESPACE = _workflow_documents.attribute('WHITESPACE')  # noqa: F401 - WebUI module contract
CJK_INLINE_SPACE = _workflow_documents.attribute('CJK_INLINE_SPACE')  # noqa: F401 - WebUI module contract
DOCX_INVALID_TEXT_CHARS = _workflow_documents.attribute('DOCX_INVALID_TEXT_CHARS')  # noqa: F401 - WebUI module contract
DOCX_CONTENT_TYPE = _workflow_documents.attribute('DOCX_CONTENT_TYPE')  # noqa: F401 - WebUI module contract
DOCX_UNIFIED_FONT = _workflow_documents.attribute('DOCX_UNIFIED_FONT')  # noqa: F401 - WebUI module contract
RASTER_LAYOUT_DOCX_DPI = _workflow_documents.attribute('RASTER_LAYOUT_DOCX_DPI')  # noqa: F401 - WebUI module contract
DOCX_EMU_PER_POINT = _workflow_documents.attribute('DOCX_EMU_PER_POINT')  # noqa: F401 - WebUI module contract
IMAGE_SUFFIXES = _workflow_documents.attribute('IMAGE_SUFFIXES')  # noqa: F401 - WebUI module contract
IMAGE_CONTENT_TYPES = _workflow_documents.attribute('IMAGE_CONTENT_TYPES')  # noqa: F401 - WebUI module contract
IMAGE_FORMATS = _workflow_documents.attribute('IMAGE_FORMATS')  # noqa: F401 - WebUI module contract
CALENDAR_PROJECT_CATEGORIES = _workflow_documents.attribute('CALENDAR_PROJECT_CATEGORIES')  # noqa: F401 - WebUI module contract
DocumentProcessor = _workflow_processor.attribute('DocumentProcessor')


LegacyPipeOperations = _workflow_webui.attribute('LegacyPipeOperations')


class Pipe(LegacyPipeOperations, DocumentProcessor):
    """Open WebUI entry; document behavior lives in the installed core package."""

    @document_request
    async def pipe(
        self, body: dict[str, Any], __user__: dict[str, Any] | None = None,
        __files__: list[dict[str, Any]] | None = None, __request__: Any = None,
        __event_emitter__: Any = None, __chat_id__: str | None = None,
        __task__: str | None = None, __metadata__: dict[str, Any] | None = None,
    ) -> str:
        if __request__ is not None and not __task__:
            await run_sync(__request__.app.state.workflow.configure_document_cache, self)
        return await self.process(
            body, __user__, __files__, __request__, __event_emitter__,
            __chat_id__, __task__, __metadata__,
        )
