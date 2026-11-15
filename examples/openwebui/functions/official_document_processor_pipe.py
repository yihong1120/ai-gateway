"""title: 公文 PDF／Word／ODT／圖片批次：附件拆分、可編輯 DOCX 與行事曆 CSV（雲端 LLM）
author: Visionnaire
version: 3.16.0
required_open_webui_version: 0.11.1
description: Processes up to 10 mixed PDF, ODT, DOC and DOCX files, or naturally sorted photos of one document. Office uploads use the existing isolated PDF converter before the shared document workflow. Original uploads are preserved. Main letters and attachments have separate PDF and Word downloads named for each parent letter; identical pages reuse PDF splitting and Word rendering within the batch. Internal OCR supports source-validated cloud analysis and editable layout DOCX. Calendar CSV contains verified events and suggested preparation reminders. Background tasks never process uploads."""
from __future__ import annotations
import tempfile  # noqa: F401 - adapter contract
from examples.official_document_workflow.domain.workflow import OutputSelection, JobPlan, PendingReminderResolution  # noqa: F401
from examples.official_document_workflow.application.pending import PendingClarificationScope  # noqa: F401

import httpx  # noqa: F401 - adapter contract
from open_webui.models.files import Files, FileModel, FileForm  # noqa: F401
from open_webui.storage.provider import Storage  # noqa: F401
from examples.official_document_workflow.domain.calendar.csv_schema import CALENDAR_CSV_FORMAT, CALENDAR_CSV_FORMAT_KEY, CALENDAR_HEADERS, CALENDAR_TRANSLATION_ROW  # noqa: F401
from examples.official_document_workflow.application.prompts import INSTRUCTION_PLANNER_PROMPT  # noqa: F401


from typing import Any
from examples.shared.runtime.concurrency import document_request
from examples.shared.runtime.io import run_sync

from examples.official_document_workflow.domain.documents import ProcessingError  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import CloudTemporaryFailure  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import CloudRateLimited  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import CloudExtractionError  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import DocumentDate  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import OfficialDocument  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import DocumentTime  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import Deadline  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import CloudDocumentExtraction  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import CloudSemanticSource  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import PreparedDocument  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import OcrLayoutBlock  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import OcrLayoutPage  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import ProcessedDocument  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import PendingCalendarReminder  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import ProcessingText  # noqa: F401 - WebUI module contract
import logging

logger = logging.getLogger(__name__)  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import INVALID_FILENAME_CHARS  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import WHITESPACE  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import CJK_INLINE_SPACE  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import DOCX_INVALID_TEXT_CHARS  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import DOCX_CONTENT_TYPE  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import DOCX_UNIFIED_FONT  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import RASTER_LAYOUT_DOCX_DPI  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import DOCX_EMU_PER_POINT  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import IMAGE_SUFFIXES  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import IMAGE_CONTENT_TYPES  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import IMAGE_FORMATS  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.domain.documents import CALENDAR_PROJECT_CATEGORIES  # noqa: F401 - WebUI module contract
from examples.official_document_workflow.services.processor import DocumentProcessor


from examples.official_document_workflow.adapters.webui import LegacyPipeOperations


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
