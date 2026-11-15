"""DocumentSources responsibilities for the shared document processor."""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import unicodedata
import warnings
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Literal

from open_webui.models.files import FileModel, Files
from open_webui.storage.provider import Storage

from examples.official_document_workflow.domain.documents import (
    IMAGE_CONTENT_TYPES,
    IMAGE_FORMATS,
    IMAGE_SUFFIXES,
    PreparedDocument,
    ProcessingError,
)
from examples.official_document_workflow.infrastructure.office import office_input

logger = logging.getLogger(__name__)


from .components import ConfiguredService


class DocumentSources(ConfiguredService):
    def __init__(self, request, ocr):
        super().__init__(request)
        self.ocr = ocr

    async def _prepare_source_documents(
        self,
        attachments: list[dict[str, Any]] | None,
        user_id: str,
    ) -> tuple[PreparedDocument, ...]:
        """Normalize mixed document files to PDFs, or photos to one page set."""
        kind, files = await self.owned_sources(attachments, user_id)
        return await self.prepare_owned_source_documents(kind, files)

    async def prepare_owned_source_documents(
        self,
        kind: Literal['pdf', 'images'],
        files: tuple[FileModel, ...],
    ) -> tuple[PreparedDocument, ...]:
        """Prepare already ACL-validated source files without re-querying them.

        ``pipe()`` uses this after a cheap ownership/type preflight so an
        invalid upload does not trigger an instruction-planning model call.
        The public helper above remains available for internal callers and
        existing tests that only have Open WebUI attachment payloads.
        """
        if kind == 'pdf':
            maximum_total_bytes = self.valves.max_batch_pdf_total_size_mb * 1024 * 1024
            total_bytes = 0
            total_pages = 0
            prepared_documents: list[PreparedDocument] = []
            with ExitStack() as cleanup:
                for file in files:
                    source_path = await self._get_local_document_path(file)
                    source_bytes = source_path.stat().st_size
                    if not self.valves.eip_large_sources and total_bytes + source_bytes > maximum_total_bytes:
                        raise ProcessingError(
                            f'文件合計超過 {self.valves.max_batch_pdf_total_size_mb} MB 的安全處理上限'
                        )
                    pdf_path, temporary_directory = source_path, None
                    source_kind = self._source_file_kind(file)
                    if source_kind == 'office':
                        temporary_directory = tempfile.TemporaryDirectory(prefix='openwebui-official-office-')
                        cleanup.callback(temporary_directory.cleanup)
                        name = file.filename
                        if Path(name).suffix.lower() not in office_input.OFFICE_SUFFIXES:
                            name += office_input.OFFICE_CONTENT_TYPES[str(file.meta['content_type']).lower()]
                        with source_path.open('rb') as source:
                            converted, _ = await office_input.to_pdf(source, name, content_cache=self.content_cache)
                        pdf_path = Path(temporary_directory.name) / 'document.pdf'
                        with converted:
                            await asyncio.to_thread(pdf_path.write_bytes, converted.getvalue())
                    self._validate_pdf_path(pdf_path)
                    total_bytes += max(source_bytes, pdf_path.stat().st_size)
                    if not self.valves.eip_large_sources and total_bytes > maximum_total_bytes:
                        raise ProcessingError(
                            f'文件合計超過 {self.valves.max_batch_pdf_total_size_mb} MB 的安全處理上限'
                        )
                    total_pages += self.ocr._validate_pdf_page_count(pdf_path)
                    if not self.valves.eip_large_sources and total_pages > self.valves.max_batch_pdf_total_pages:
                        raise ProcessingError(
                            '文件轉 PDF 後合計有 '
                            f'{total_pages} 頁，超過 {self.valves.max_batch_pdf_total_pages} 頁的安全處理上限',
                        )
                    prepared_documents.append(
                        PreparedDocument(
                            kind=source_kind,
                            source_files=(file,),
                            pdf_path=pdf_path,
                            temporary_directory=temporary_directory,
                            original_path=source_path,
                        ),
                    )
                # Ownership transfers only after the whole preparation succeeds.
                # A failed/cancelled conversion cleans every temporary PDF here.
                cleanup.pop_all()
            return tuple(prepared_documents)

        image_paths = await self._get_local_image_paths(files)
        temporary_directory = tempfile.TemporaryDirectory(prefix='openwebui-official-document-images-')
        output_path = Path(temporary_directory.name) / 'images.pdf'
        try:
            await asyncio.to_thread(self._images_to_pdf, image_paths, output_path)
            return (
                PreparedDocument(
                    kind='images',
                    source_files=files,
                    pdf_path=output_path,
                    image_count=len(files),
                    temporary_directory=temporary_directory,
                ),
            )
        except Exception:
            temporary_directory.cleanup()
            raise

    async def owned_sources(
        self,
        attachments: list[dict[str, Any]] | None,
        user_id: str,
    ) -> tuple[Literal['pdf', 'images'], tuple[FileModel, ...]]:
        """Find owned document files or a naturally filename-sorted image set."""
        file_ids: list[str] = []
        for item in attachments or []:
            if not isinstance(item, dict) or item.get('type', 'file') != 'file':
                continue
            file_id = item.get('id')
            if not isinstance(file_id, str) or not file_id or file_id.startswith(('http://', 'https://', 'data:')):
                continue
            if file_id not in file_ids:
                file_ids.append(file_id)

        document_files: list[FileModel] = []
        image_files: list[FileModel] = []
        for file_id in file_ids:
            file = await Files.get_file_by_id_and_user_id(file_id, user_id)
            if file is None:
                continue
            file_kind = self._source_file_kind(file)
            if file_kind in {'pdf', 'office'}:
                document_files.append(file)
            elif file_kind == 'image':
                image_files.append(file)

        if document_files and image_files:
            raise ProcessingError('PDF、ODT、DOC、DOCX 可混合上傳；同一份公文的圖片請另批上傳')
        if document_files:
            if len(document_files) > self.valves.max_batch_pdf_count:
                raise ProcessingError(
                    f'一次最多處理 {self.valves.max_batch_pdf_count} 份文件（PDF／ODT／DOC／DOCX 合計）；請分批上傳',
                )
            # Use the same human-friendly deterministic ordering as image
            # pages.  It defines the sequential processing/output order only;
            # PDFs never become a merged source document.
            document_files.sort(key=self._natural_image_filename_key)
            return 'pdf', tuple(document_files)
        if image_files:
            image_limit = min(self.valves.max_image_count, self.valves.max_ocr_pages)
            if len(image_files) > image_limit:
                raise ProcessingError(f'一次最多處理 {image_limit} 張圖片；請分批上傳同一份公文')
            # ``list.sort`` is stable, so names that compare the same after
            # case folding and number normalisation retain their attachment
            # order. The normal case is page-2 < page-10 regardless of the
            # order in which a browser happened to upload those photos.
            image_files.sort(key=self._natural_image_filename_key)
            return 'images', tuple(image_files)
        raise ProcessingError('請附加你自己上傳的 PDF、ODT、DOC、DOCX，或同一份公文的 JPG、PNG、WebP、HEIC 圖片')

    @staticmethod
    def _natural_image_filename_key(file: FileModel) -> tuple[tuple[int, int | str], ...]:
        """Return a stable, case-insensitive natural ordering key for a filename.

        Sorting uses the basename only: an uploaded Windows path cannot affect
        page order. Numeric runs compare as numbers, so ``page-2.jpg`` comes
        before ``page-10.jpg``. The caller deliberately relies on Python's
        stable sort for identical natural keys.
        """
        filename = str(getattr(file, 'filename', ''))
        basename = Path(filename.replace('\\', '/')).name
        normalized = unicodedata.normalize('NFKC', basename).casefold()
        return tuple((0, int(part)) if part.isdecimal() else (1, part) for part in re.split(r'(\d+)', normalized))

    @staticmethod
    def _source_file_kind(file: FileModel) -> Literal['pdf', 'office', 'image'] | None:
        filename = str(getattr(file, 'filename', ''))
        metadata = getattr(file, 'meta', None)
        content_type = str(metadata.get('content_type') or '').lower() if isinstance(metadata, dict) else ''
        suffix = Path(filename).suffix.lower()
        if suffix in office_input.OFFICE_SUFFIXES or content_type in office_input.OFFICE_CONTENT_TYPES:
            return 'office'
        if suffix == '.pdf' or content_type == 'application/pdf':
            return 'pdf'
        if suffix in IMAGE_SUFFIXES or content_type in IMAGE_CONTENT_TYPES:
            return 'image'
        return None

    async def _get_local_document_path(self, file: FileModel) -> Path:
        if not file.path:
            raise ProcessingError('附件沒有可讀取的儲存位置')
        path = Path(await asyncio.to_thread(Storage.get_file, file.path))
        if not path.is_file():
            raise ProcessingError('附件暫時無法讀取')
        if not self.valves.eip_large_sources and path.stat().st_size > self.valves.max_pdf_size_mb * 1024 * 1024:
            raise ProcessingError(f'文件超過 {self.valves.max_pdf_size_mb} MB 的安全處理上限')
        return path

    async def _get_local_image_paths(self, files: tuple[FileModel, ...]) -> tuple[Path, ...]:
        maximum_bytes = self.valves.max_image_total_size_mb * 1024 * 1024
        total_bytes = 0
        paths: list[Path] = []
        for file in files:
            if not file.path:
                raise ProcessingError('附件沒有可讀取的儲存位置')
            path = Path(await asyncio.to_thread(Storage.get_file, file.path))
            if not path.is_file():
                raise ProcessingError('附件暫時無法讀取')
            total_bytes += path.stat().st_size
            if total_bytes > maximum_bytes:
                raise ProcessingError(
                    f'圖片合計超過 {self.valves.max_image_total_size_mb} MB 的安全處理上限',
                )
            paths.append(path)
        return tuple(paths)

    def _validate_pdf_path(self, path: Path) -> None:
        if not path.is_file():
            raise ProcessingError('附件暫時無法讀取')
        maximum_bytes = self.valves.max_pdf_size_mb * 1024 * 1024
        if not self.valves.eip_large_sources and path.stat().st_size > maximum_bytes:
            raise ProcessingError(f'PDF 超過 {self.valves.max_pdf_size_mb} MB 的安全處理上限')
        with path.open('rb') as stream:
            if stream.read(5) != b'%PDF-':
                raise ProcessingError('附件不是有效的 PDF')

    def _images_to_pdf(self, image_paths: tuple[Path, ...], output_path: Path) -> None:
        """Build a bounded PDF from supported phone images in the prepared filename order."""
        try:
            from PIL import Image, ImageOps, UnidentifiedImageError
            from pillow_heif import register_heif_opener
        except ImportError as exc:
            raise ProcessingError('伺服器缺少手機影像處理套件；請聯絡管理員') from exc

        try:
            register_heif_opener()
        except Exception as exc:
            logger.warning('Could not enable HEIC image support: %s', type(exc).__name__)
            raise ProcessingError('伺服器無法啟用 HEIC 圖片處理；請改傳 JPG 或 PNG') from exc

        pages: list[Any] = []
        total_pixels = 0
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                for image_path in image_paths:
                    with Image.open(image_path) as image:
                        image_format = str(image.format or '').upper()
                        if image_format not in IMAGE_FORMATS:
                            raise ProcessingError('圖片格式不支援；請使用 JPG、PNG、WebP、HEIC 或 HEIF')
                        if bool(getattr(image, 'is_animated', False)) or getattr(image, 'n_frames', 1) != 1:
                            raise ProcessingError('不支援動畫圖片；請上傳單張公文照片')
                        width, height = image.size
                        pixels = width * height
                        if pixels < 1 or pixels > self.valves.max_image_pixels_per_image:
                            raise ProcessingError(
                                f'單張圖片超過 {self.valves.max_image_pixels_per_image:,} 像素的安全處理上限',
                            )
                        total_pixels += pixels
                        if total_pixels > self.valves.max_image_total_pixels:
                            raise ProcessingError(
                                f'圖片合計超過 {self.valves.max_image_total_pixels:,} 像素的安全處理上限',
                            )
                        image.load()
                        oriented = ImageOps.exif_transpose(image)
                        try:
                            if oriented.mode in {'RGBA', 'LA'} or 'transparency' in oriented.info:
                                rgba = oriented.convert('RGBA')
                                try:
                                    page = Image.new('RGB', rgba.size, 'white')
                                    page.paste(rgba, mask=rgba.getchannel('A'))
                                finally:
                                    rgba.close()
                            else:
                                page = oriented.convert('RGB')
                        finally:
                            if oriented is not image:
                                oriented.close()
                        pages.append(page)

                if not pages:
                    raise ProcessingError('沒有可處理的圖片')
                output_path.parent.mkdir(parents=True, exist_ok=True)
                pages[0].save(
                    output_path,
                    format='PDF',
                    save_all=True,
                    append_images=pages[1:],
                    resolution=150.0,
                )
            self._validate_pdf_path(output_path)
            self.ocr._validate_pdf_page_count(output_path)
        except ProcessingError:
            raise
        except (
            UnidentifiedImageError,
            OSError,
            TypeError,
            ValueError,
            Image.DecompressionBombWarning,
            Image.DecompressionBombError,
        ) as exc:
            logger.info('Could not make a PDF from source images: %s', type(exc).__name__)
            raise ProcessingError('有圖片無法辨識；請上傳原始 JPG、PNG、WebP、HEIC 或 HEIF 照片') from exc
        finally:
            for page in pages:
                page.close()

    @staticmethod
    def _derived_source_metadata(source: PreparedDocument) -> dict[str, Any]:
        """Keep a source audit trail without exposing filenames in the chat."""
        return {
            'official_document_input': {
                'source_kind': source.kind,
                'source_file_ids': [str(file.id) for file in source.source_files],
                'input_image_count': source.image_count,
            },
        }
