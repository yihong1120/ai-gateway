"""DocumentExports responsibilities for the shared document processor."""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import zipfile
from datetime import datetime
from datetime import timezone
from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version
from pathlib import Path
from typing import Any

from open_webui.models.files import FileForm
from open_webui.models.files import FileModel
from open_webui.models.files import Files
from open_webui.storage.provider import Storage

from examples.official_document_workflow.domain.documents import DOCX_EMU_PER_POINT
from examples.official_document_workflow.domain.documents import DOCX_UNIFIED_FONT
from examples.official_document_workflow.domain.documents import OcrLayoutPage
from examples.official_document_workflow.domain.documents import OfficialDocument
from examples.official_document_workflow.domain.documents import OutputNotWritten
from examples.official_document_workflow.domain.documents import OutputUncertain
from examples.official_document_workflow.domain.documents import PreparedDocument
from examples.official_document_workflow.domain.documents import ProcessingError
from examples.official_document_workflow.domain.documents import RASTER_LAYOUT_DOCX_DPI
from examples.shared.runtime.modules import ModuleReference

_PIL = ModuleReference('PIL')
_docx = ModuleReference('docx')
_docx_enum_section = ModuleReference('docx.enum.section')
_docx_shared = ModuleReference('docx.shared')
_workflow_components = ModuleReference('examples.official_document_workflow.services.components')
_workflow_file_access = ModuleReference('examples.official_document_workflow.services.file_access')
_workflow_output_deletion = ModuleReference('examples.official_document_workflow.services.output_deletion')
_workflow_output_delivery = ModuleReference('examples.official_document_workflow.services.output_delivery')
_shared_concurrency = ModuleReference('examples.shared.runtime.concurrency')
_shared_io = ModuleReference('examples.shared.runtime.io')
_pymupdf = ModuleReference('pymupdf')

logger = logging.getLogger(__name__)


ConfiguredService = _workflow_components.attribute('ConfiguredService')
FileOutput, OutputDelivery = _workflow_output_delivery.attributes('FileOutput', 'OutputDelivery')
FileAccess = _workflow_file_access.attribute('FileAccess')
OutputDeletion = _workflow_output_deletion.attribute('OutputDeletion')
run_sync = _shared_io.attribute('run_sync')
run_ocr = _shared_concurrency.attribute('run_ocr')


WORD_RENDER_REVISION = 'positioned-editable-docx-v1'


@lru_cache(maxsize=1)
def word_renderer_versions():
    try:
        return {name: version(name) for name in ('PyMuPDF', 'python-docx', 'Pillow')}
    except PackageNotFoundError as error:
        raise ProcessingError('伺服器缺少建立可編輯版面 DOCX 所需的套件') from error


def validate_word_payload(payload):
    if not isinstance(payload, bytes) or not payload:
        raise ProcessingError('Word 輸出內容格式無效')
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            if not all(archive.getinfo(name).file_size for name in ('[Content_Types].xml', 'word/document.xml')):
                raise ProcessingError('Word 輸出內容不完整')
    except (zipfile.BadZipFile, KeyError) as error:
        raise ProcessingError('Word 輸出內容格式無效') from error


class DocumentExports(ConfiguredService):
    def __init__(self, request, ocr, sources, rendering):
        super().__init__(request)
        self.ocr, self.sources, self.rendering = ocr, sources, rendering
        self.files = FileAccess(Files, Storage)
        self.delivery = OutputDelivery(request.outputs, self._write_file, self.files.find)
        self.deletion = OutputDeletion(request.outputs, self.files)

    async def word_payload(self, source_path, *, spotting_required=True, load_word_document=None):
        """Reuse only pure DOCX bytes; ownership and delivery remain caller-owned."""
        if load_word_document is not None:
            # Upload's selected layout pages are scoped to that batch; an opaque
            # callback cannot be fingerprinted as the EIP spotting renderer.
            return await load_word_document()

        async def render():
            spotting = await run_ocr(self.ocr.paddleocr_vl_spotting_response, source_path) if spotting_required else None
            return await asyncio.to_thread(self.render_word, source_path, spotting)

        return await self.cached_word_payload(source_path, render, mode='spotting' if spotting_required else 'native')

    async def cached_word_payload(self, source_path, render, *, mode):
        """Reuse the same renderer, preserving selected-page geometry identities."""
        shared = self.content_cache
        if shared is None:
            return await render()

        def fingerprint():
            with source_path.open('rb') as stream:
                return hashlib.file_digest(stream, 'sha256').hexdigest()

        key = shared.key(
            'word-payload', WORD_RENDER_REVISION, await asyncio.to_thread(fingerprint), word_renderer_versions(),
            DOCX_UNIFIED_FONT, RASTER_LAYOUT_DOCX_DPI, DOCX_EMU_PER_POINT, mode,
            self.ocr.content_identity() if mode != 'native' else None,
        )
        # Coordinate before spotting enters the GPU semaphore. Other owners
        # await the same content while keeping their output IDs and receipts.
        return await shared.aget_or_compute(key, render, validate=validate_word_payload)

    def render_word(
        self,
        source_path: Path,
        response_body: dict[str, Any] | None,
        *,
        layout_pages: tuple[OcrLayoutPage, ...] | None = None,
    ) -> bytes:
        """Create a fixed-position, editable DOCX from an original PDF.

        This renderer keeps each source page as a rotation-correct background,
        removes only neutral OCR-covered ink, and puts recognised text in
        native, page-anchored Word paragraphs at matching page coordinates.
        It works for source PDFs with or without a native text layer: the
        result is ordinary editable ``w:p``/``w:r``/``w:t`` text while stamps,
        coloured seals, signatures and unreliable coloured text remain
        visually stable in the source underlay. Pages containing only a
        signature, stamp, distribution list, or other OCR-empty content still
        retain their faithful background and do not discard editable text from
        the rest of the document.

        PaddleOCR-VL's required ``spotting`` path supplies physical-line
        rectangles. Text remains editable, but a user who makes one line much
        longer than its source can still make that one frame wrap; it never
        makes the rest of the document reflow.
        """
        self.ocr._validate_pdf_page_count(source_path)
        try:
            pymupdf = _pymupdf.load()
            WordDocument = _docx.attribute('Document')
            WD_SECTION_START = _docx_enum_section.attribute('WD_SECTION_START')
            Emu, Pt = _docx_shared.attributes('Emu', 'Pt')
            Image = _PIL.attribute('Image')
        except ImportError as exc:
            raise ProcessingError('伺服器缺少建立可編輯版面 DOCX 所需的套件') from exc

        layout_pages = (
            layout_pages
            if layout_pages is not None
            else (
                self.ocr.paddleocr_vl_spotting_pages(response_body)
                if response_body is not None
                else self.rendering.native_pdf_layout_pages(source_path)
            )
        )
        try:
            document = WordDocument()
            self.rendering._set_docx_unified_normal_font(document)
            has_editable_placements = False
            with pymupdf.open(str(source_path)) as pdf:
                if pdf.page_count < 1:
                    raise ProcessingError('PDF 不含可轉換頁面')
                if len(layout_pages) != pdf.page_count:
                    raise ProcessingError('GPU OCR 頁面數與原始 PDF 不一致')

                for page_index, page in enumerate(pdf):
                    layout = layout_pages[page_index]
                    section = (
                        document.sections[0] if page_index == 0 else document.add_section(WD_SECTION_START.NEW_PAGE)
                    )
                    page_width_pt = float(page.rect.width)
                    page_height_pt = float(page.rect.height)
                    page_width = Emu(round(page_width_pt * DOCX_EMU_PER_POINT))
                    page_height = Emu(round(page_height_pt * DOCX_EMU_PER_POINT))
                    section.page_width = page_width
                    section.page_height = page_height
                    for setting in (
                        'top_margin',
                        'bottom_margin',
                        'left_margin',
                        'right_margin',
                        'header_distance',
                        'footer_distance',
                        'gutter',
                    ):
                        setattr(section, setting, Emu(0))

                    pixmap = page.get_pixmap(dpi=RASTER_LAYOUT_DOCX_DPI, alpha=False)
                    with Image.open(io.BytesIO(pixmap.tobytes('png'))) as opened:
                        page_image = opened.convert('RGB')
                    try:
                        placements = self.rendering._editable_ocr_placements(
                            page_image,
                            layout,
                            page_width_pt=page_width_pt,
                            page_height_pt=page_height_pt,
                        )
                        # Administrative letters commonly include a mostly
                        # blank signature, distribution, or attachment page.
                        # That page still needs its faithful background, but
                        # must not make every other recognised page lose its
                        # editable Word text. Reject the export only if OCR
                        # cannot place any text on the *entire* document.
                        if placements:
                            has_editable_placements = True
                            self.rendering._erase_ocr_text_from_page_image(
                                page_image,
                                placements,
                                layout,
                            )
                        image_stream = io.BytesIO()
                        page_image.save(image_stream, format='PNG')
                    finally:
                        page_image.close()
                    image_stream.seek(0)

                    background = document.add_paragraph()
                    background.paragraph_format.space_before = Pt(0)
                    background.paragraph_format.space_after = Pt(0)
                    background.paragraph_format.line_spacing = Pt(1)
                    picture = background.add_run().add_picture(
                        image_stream,
                        width=page_width,
                        height=page_height,
                    )
                    self.rendering._anchor_docx_picture_to_page(
                        picture._inline,
                        picture_id=10_000 + page_index,
                    )

                    for block, x_pt, y_pt, width_pt, height_pt, font_size_pt in placements:
                        self.rendering._append_docx_ocr_frame_paragraph(
                            document,
                            block=block,
                            x_pt=x_pt,
                            y_pt=y_pt,
                            width_pt=width_pt,
                            height_pt=height_pt,
                            font_size_pt=font_size_pt,
                        )

            if not has_editable_placements:
                raise ProcessingError('GPU OCR 沒有可定位的可編輯文字區塊')

            output = io.BytesIO()
            document.save(output)
            return output.getvalue()
        except ProcessingError:
            raise
        except Exception as exc:  # Third-party PDF/DOCX libraries have no common errors.
            logger.info('Could not build editable PDF layout DOCX: %s', type(exc).__name__)
            raise ProcessingError('無法將 PDF 建立為可編輯版面 DOCX') from exc

    async def _create_or_rename_pdf(
        self,
        source: PreparedDocument,
        user_id: str,
        output_name: str,
        document: OfficialDocument,
        *,
        force_derived: bool = False,
    ) -> FileModel:
        source_file = source.source_file
        audit = {
            'source_filename': source_file.filename,
            'source_kind': source.kind,
            'source_file_ids': [str(file.id) for file in source.source_files],
            'input_image_count': source.image_count,
            'generated_filename': output_name,
            'issued_date': document.issued.roc,
            'reference_number': document.reference_number,
            'subject': document.subject,
            'processed_at': datetime.now(timezone.utc).isoformat(),
        }
        # A JPG/HEIC must never be renamed with a .pdf suffix. Image sources
        # always create a new, merged PDF even if an administrator enables the
        # original-PDF rename option.
        if source.is_original_pdf and self.valves.rename_original_attachment and not force_derived:
            await Files.update_file_metadata_by_id(source_file.id, {'official_document': audit})
            renamed = await Files.update_file_name_by_id(source_file.id, output_name)
            if renamed is None:
                raise ProcessingError('無法更新附件名稱')
            return renamed

        payload = await asyncio.to_thread(source.pdf_path.read_bytes)
        return await self.store_output(
            user_id=user_id,
            payload=payload,
            display_name=output_name,
            content_type='application/pdf',
            source_file_id=source_file.id,
            kind='renamed_pdf',
            extra_metadata={
                'official_document': audit,
                **self.sources._derived_source_metadata(source),
            },
        )

    async def store_output(
        self,
        *,
        user_id: str,
        payload: bytes,
        display_name: str,
        content_type: str,
        source_file_id: str,
        kind: str,
        extra_metadata: dict[str, Any] | None = None,
        output_id: str | None = None,
    ) -> FileModel:
        stream = io.BytesIO(payload) if isinstance(payload, bytes) else payload

        def fingerprint():
            stream.seek(0)
            digest, size = hashlib.sha256(), 0
            while block := stream.read(1024 * 1024):
                digest.update(block)
                size += len(block)
            stream.seek(0)
            return size, digest.hexdigest()

        try:
            size, digest = await run_sync(fingerprint)
            key, job = FileOutput.identity(user_id, source_file_id, kind, display_name, output_id)
        except Exception as error:
            raise OutputNotWritten('輸出內容尚未寫入。') from error
        output = FileOutput(
            key, user_id, display_name, digest, size, source_file_id, kind, content_type,
            stream, extra_metadata or {}, job,
        )
        return await self.delivery.store(output)

    async def _write_file(self, output):
        storage_name = output.id + (Path(output.name).suffix.lower() or '.bin')
        tags = {'OpenWebUI-User-Id': output.owner, 'OpenWebUI-File-Id': output.id}
        try:
            _, storage_path = await run_sync(Storage.upload_file, output.stream, storage_name, tags)
            metadata = {
                **output.metadata,
                'name': output.name, 'content_type': output.content_type, 'size': output.size,
                'file_hash': output.sha256,
                'data': {
                    'generated_by': 'official_document_processor_pipe', 'kind': output.kind,
                    'source_file_id': output.source, 'output_job_id': output.job,
                },
            }
            item = await Files.insert_new_file(
                output.owner, FileForm(
                    id=output.id, filename=output.name,
                    path=storage_path, data={}, meta=metadata,
                ),
            )
            if item is None:
                raise OutputUncertain('下載紀錄寫入結果需要核對。')
            return item
        except Exception as error:
            # Upload or database commit can succeed before returning an error.
            # Every output has a durable identity; never delete it on this path.
            raise OutputUncertain(f'輸出 {output.id} 寫入結果需核對。') from error

    async def find_output(self, record):
        return await self.files.find(record)

    async def delete_output(self, file: FileModel):
        owner = getattr(file, 'user_id', None) or self._usage_owner
        return await self.deletion.delete(file.id, owner)

    @staticmethod
    def download_link(request: Any, file_id: str, display_name: str) -> str:
        root_path = ''
        scope = getattr(request, 'scope', None)
        if isinstance(scope, dict):
            root_path = str(scope.get('root_path') or '').rstrip('/')
        label = re.sub(r'([\\\[\]\(\)])', r'\\\1', display_name)
        return f'[{label}]({root_path}/api/v1/files/{file_id}/content?attachment=true)'
