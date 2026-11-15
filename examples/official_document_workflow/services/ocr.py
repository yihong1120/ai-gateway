"""DocumentOCR responsibilities for the shared document processor."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import math
import re
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any
from typing import Literal
from urllib.parse import urlsplit

import httpx

from examples.official_document_workflow.domain.documents import CloudSemanticSource
from examples.official_document_workflow.domain.documents import OcrLayoutBlock
from examples.official_document_workflow.domain.documents import OcrLayoutPage
from examples.official_document_workflow.domain.documents import ProcessingError
from examples.official_document_workflow.infrastructure.storage.processing_cache import (
    active_requests,
)
from examples.shared.runtime.concurrency import run_ocr
from examples.shared.runtime.modules import ModuleReference

_workflow_components = ModuleReference('examples.official_document_workflow.services.components')
_pymupdf = ModuleReference('pymupdf')

logger = logging.getLogger(__name__)


ConfiguredService = _workflow_components.attribute('ConfiguredService')


OCR_CONTENT_REVISION = 'paddle-layout-and-spotting-v2'
NATIVE_TEXT_REVISION = 'pdftotext-raw-page-boundaries-v1'


@lru_cache(maxsize=1)
def native_text_tool_version():
    try:
        result = subprocess.run(
            ['pdftotext', '-v'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors='replace', check=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ProcessingError('伺服器無法確認 PDF 文字擷取工具版本') from error
    return result.stdout.strip()


def validate_native_source(value):
    if not isinstance(value, dict) or not isinstance(value.get('text'), str):
        raise ProcessingError('PDF 原生文字快取格式無效')
    text = value['text']
    structural = value.get('structural_lines')
    line_count = len(text.splitlines())
    if (
        not text.strip() or any(ord(char) < 32 and char not in '\t\n' for char in text)
        or not isinstance(structural, list)
        or any(type(number) is not int or not 1 <= number <= line_count for number in structural)
        or structural != sorted(set(structural))
    ):
        raise ProcessingError('PDF 原生文字快取的原文或行號無效')


def validated_ocr_result(response: Any, pages: int) -> dict[str, Any]:
    """Validate fresh and cached chunk responses before assigning source pages."""
    if not isinstance(response, dict) or response.get('errorCode') != 0:
        raise ProcessingError('GPU OCR 無法處理此文件；請確認 PDF 未加密且掃描完整')
    result = response.get('result')
    items = result.get('layoutParsingResults') if isinstance(result, dict) else None
    if not isinstance(items, list) or len(items) != pages:
        raise ProcessingError('GPU OCR 回傳頁數不符，未使用不完整結果')
    for index, item in enumerate(items):
        pruned = item.get('prunedResult') if isinstance(item, dict) else None
        if not isinstance(pruned, dict):
            raise ProcessingError('GPU OCR 回傳的頁面缺少版面資訊')
        page_index = pruned.get('page_index')
        if page_index is not None and (type(page_index) is not int or page_index != index):
            raise ProcessingError('GPU OCR 回傳頁序不符，未使用錯置結果')
    info = result.get('dataInfo')
    if info is not None and (
        not isinstance(info, dict) or not isinstance(info.get('pages', []), list)
    ):
        raise ProcessingError('GPU OCR 回傳頁面資訊不符')
    return result


class DocumentOCR(ConfiguredService):
    def content_identity(self):
        """All recognition settings that can change reusable text or geometry."""
        return (
            OCR_CONTENT_REVISION,
            self._paddleocr_vl_endpoint(),
            self.valves.paddleocr_vl_max_new_tokens,
            min(self.valves.ocr_chunk_pages, self.valves.max_ocr_pages),
        )

    def _paddleocr_vl_response(self, source_path: Path) -> dict[str, Any]:
        """Call the loopback PaddleOCR-VL layout path and validate its envelope."""
        return self._paddleocr_vl_request(source_path, use_layout_detection=True)

    def paddleocr_vl_spotting_response(self, source_path: Path) -> dict[str, Any]:
        """Get physical-line OCR coordinates for a visual editable PDF DOCX.

        Recognition never alters the source PDF. The approved content cache
        may reuse its validated geometry, giving the DOCX renderer physical
        line boxes instead of inferring line breaks from larger layout blocks.
        """
        return self._paddleocr_vl_request(
            source_path,
            use_layout_detection=False,
            prompt_label='spotting',
        )

    def _paddleocr_vl_request(
        self,
        source_path: Path,
        *,
        use_layout_detection: bool,
        prompt_label: str | None = None,
    ) -> dict[str, Any]:
        """Read all pages in bounded sequential chunks, preserving source order."""
        pages = self._validate_pdf_page_count(source_path)
        chunk_size = min(self.valves.ocr_chunk_pages, self.valves.max_ocr_pages)
        if pages <= chunk_size:
            return self._paddleocr_vl_checkpointed_request(
                source_path,
                pages=pages,
                use_layout_detection=use_layout_detection,
                prompt_label=prompt_label,
            )
        pymupdf = _pymupdf.load()

        merged, metadata = [], []
        with (
            tempfile.TemporaryDirectory(prefix='official-ocr-chunks-') as directory,
            pymupdf.open(source_path) as source,
        ):
            for start in range(0, pages, chunk_size):
                end = min(start + chunk_size, pages)
                path = Path(directory) / f'pages-{start + 1}-{end}.pdf'
                with pymupdf.open() as chunk:
                    chunk.insert_pdf(source, from_page=start, to_page=end - 1)
                    chunk.save(path, no_new_id=True)
                try:
                    logger.info(
                        'GPU OCR chunk pages=%s-%s total=%s mode=%s',
                        start + 1,
                        end,
                        pages,
                        prompt_label or 'layout',
                    )
                    response = self._paddleocr_vl_checkpointed_request(
                        path,
                        pages=end - start,
                        use_layout_detection=use_layout_detection,
                        prompt_label=prompt_label,
                    )
                    result = response['result']
                    items = result['layoutParsingResults']
                    data_pages = (result.get('dataInfo') or {}).get('pages', [])
                    for local, item in enumerate(items):
                        # Global numbering belongs to this document, not the cached chunk.
                        merged.append({**item, 'prunedResult': {**item['prunedResult'], 'page_index': start + local}})
                        metadata.append(data_pages[local] if len(data_pages) == len(items) else {})
                except ProcessingError as exc:
                    raise ProcessingError(f'PDF 第 {start + 1}–{end} 頁：{exc}') from exc
                finally:
                    path.unlink(missing_ok=True)
        return {
            'errorCode': 0,
            'result': {'layoutParsingResults': merged, 'dataInfo': {'pages': metadata}},
            'sourcePageCount': pages,
            'ocrChunkPages': chunk_size,
        }

    def _paddleocr_vl_checkpointed_request(self, path, *, pages, use_layout_detection, prompt_label):
        def recognize():
            response = self._paddleocr_vl_single_request(
                path,
                use_layout_detection=use_layout_detection,
                prompt_label=prompt_label,
            )
            validated_ocr_result(response, pages)
            return response

        shared = self.content_cache
        cache = getattr(self, '_eip_processing_cache', None)
        if cache is None and (requests := active_requests.get()) is not None:
            cache = requests.backend
        key = None
        if shared is not None or cache is not None:
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if shared is not None:
            key = shared.key('ocr-chunk', *self.content_identity(), digest, pages, use_layout_detection, prompt_label)
            return shared.get_or_compute(key, recognize, validate=lambda response: validated_ocr_result(response, pages))
        if cache is not None:
            key = cache.key(
                'ocr-chunk-v1',
                digest,
                use_layout_detection,
                prompt_label,
                self.valves.paddleocr_vl_base_url,
                self.valves.paddleocr_vl_max_new_tokens,
            )
            cached = cache.get(key)
            if cached is not None:
                try:
                    validated_ocr_result(cached, pages)
                except ProcessingError:
                    logger.warning('Ignored invalid OCR chunk cache; rebuilding from source')
                else:
                    return cached
        response = recognize()
        if cache is not None:
            cache.put(key, response)
        return response

    def _paddleocr_vl_single_request(
        self, source_path: Path, *, use_layout_detection: bool, prompt_label: str | None = None,
    ) -> dict[str, Any]:
        """One local request. No retry, fallback, semantic truncation or parallel GPU load."""
        endpoint = self._paddleocr_vl_endpoint()
        pdf_bytes = source_path.read_bytes()
        payload = {
            'file': base64.b64encode(pdf_bytes).decode('ascii'),
            'fileType': 0,
            'useLayoutDetection': use_layout_detection,
            'restructurePages': False,
            'visualize': False,
            'maxNewTokens': self.valves.paddleocr_vl_max_new_tokens,
        }
        if prompt_label is not None:
            payload['promptLabel'] = prompt_label
        timeout = httpx.Timeout(self.valves.paddleocr_vl_timeout_seconds, connect=10.0)
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
                response = client.post(endpoint, json=payload)
        except httpx.TimeoutException as exc:
            raise ProcessingError('GPU OCR 文件辨識逾時，請改用較小或較清楚的 PDF') from exc
        except httpx.HTTPError as exc:
            raise ProcessingError('目前無法連線至本機 GPU OCR 服務；請聯絡管理員') from exc

        if response.status_code != 200:
            raise ProcessingError('GPU OCR 服務暫時無法處理此文件；請稍後再試')
        try:
            response_body = response.json()
        except ValueError as exc:
            raise ProcessingError('GPU OCR 回傳格式無法辨識；請聯絡管理員') from exc
        return response_body

    def _paddleocr_vl_endpoint(self) -> str:
        configured = self.valves.paddleocr_vl_base_url.strip()
        try:
            parsed = urlsplit(configured)
            port = parsed.port
        except ValueError as exc:
            raise ProcessingError('GPU OCR 服務設定無效；請聯絡管理員') from exc
        if (
            parsed.scheme != 'http'
            or parsed.hostname != '127.0.0.1'
            or port is None
            or not 1 <= port <= 65535
            or parsed.path not in {'', '/'}
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ProcessingError('GPU OCR 服務必須設定為本機 loopback 位址；請聯絡管理員')
        return f'http://127.0.0.1:{port}/layout-parsing'

    def _validate_pdf_page_count(self, source_path: Path) -> int:
        info = self._run_text(['pdfinfo', str(source_path)], timeout_seconds=15)
        match = re.search(r'^Pages:\s*(\d+)\s*$', info, flags=re.MULTILINE)
        if match is None:
            raise ProcessingError('無法確認 PDF 頁數；請確認 PDF 未加密或損毀')
        pages = int(match.group(1))
        if pages < 1:
            raise ProcessingError('PDF 沒有可辨識的頁面')
        if not self.valves.eip_large_sources and pages > self.valves.max_pdf_pages:
            raise ProcessingError(f'PDF 有 {pages} 頁，超過 {self.valves.max_pdf_pages} 頁的安全處理上限')
        return pages

    def semantic_source(
        self,
        source_path: Path,
        scan_page_indices: tuple[int, ...],
        paddle_response: dict[str, Any] | None,
    ) -> CloudSemanticSource:
        """Build the one complete source accepted by the cloud extractor.

        A PDF with even one scanned page must use the all-page Paddle layout
        result; mixing its partial native text layer with OCR would silently
        omit visual pages.  A fully native PDF uses ``pdftotext -raw`` because
        it removes visual A4 wraps at the source.  No document field, keyword
        or sentence meaning is interpreted locally here.
        """
        source_kind: Literal['native_pdf_text', 'paddle_layout_ocr']
        structural_lines: set[int] = set()
        if scan_page_indices:
            source_kind = 'paddle_layout_ocr'
            if paddle_response is None:
                raise ProcessingError('GPU OCR 未提供掃描頁的版面文字')
            text = self._paddleocr_vl_cloud_text(paddle_response, structural_lines=structural_lines)
        else:
            source_kind = 'native_pdf_text'
            shared = self.content_cache
            if shared is None:
                text = self._native_pdf_text_for_cloud_semantics(source_path, structural_lines=structural_lines)
            else:
                def extract():
                    markers = set()
                    value = {
                        'text': self._native_pdf_text_for_cloud_semantics(source_path, structural_lines=markers),
                        'structural_lines': sorted(markers),
                    }
                    validate_native_source(value)
                    return value

                with source_path.open('rb') as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                key = shared.key('native-source', NATIVE_TEXT_REVISION, native_text_tool_version(), digest)
                value = shared.get_or_compute(key, extract, validate=validate_native_source)
                text = value['text']
                structural_lines.update(value['structural_lines'])

        text = self._bounded_cloud_semantic_text(text)
        if not text:
            source_label = 'GPU OCR 版面文字' if scan_page_indices else 'PDF 原生文字層'
            raise ProcessingError(f'無法取得完整{source_label}供雲端公文判讀')
        return CloudSemanticSource(text=text, kind=source_kind, structural_lines=frozenset(structural_lines))

    def _bounded_cloud_semantic_text(self, value: str) -> str:
        """Validate a complete local source without truncating it mid-document."""
        if not isinstance(value, str):
            return ''
        # ``pdftotext`` uses form feed between pages. Convert it to an
        # explicit non-document page boundary before it reaches JSON/LLM text.
        cleaned = value.replace('\r\n', '\n').replace('\r', '\n').replace('\f', '\n\n<page-break />\n\n').strip()
        if not cleaned:
            return ''
        if any(ord(character) < 32 and character not in '\t\n' for character in cleaned):
            return ''
        if not self.valves.eip_large_sources and len(cleaned) > self.valves.cloud_ocr_text_max_input_characters:
            # Never feed only the first N characters: a trailing page may
            # contain the formal explanation or deadline that the model needs.
            raise ProcessingError('公文文字超過完整雲端判讀上限；請分拆文件後再試')
        return cleaned

    def _native_pdf_text_for_cloud_semantics(self, source_path: Path, *, structural_lines=None) -> str:
        """Read complete native PDF text for semantic review without A4 wraps."""
        raw_text = self._run_text(['pdftotext', '-raw', str(source_path), '-'], timeout_seconds=30)
        return self._page_tagged_cloud_text(raw_text, structural_lines=structural_lines)

    @classmethod
    def _paddleocr_vl_cloud_text(cls, response_body: dict[str, Any], *, structural_lines=None) -> str:
        """Convert Paddle layout blocks into a semantic source for the cloud.

        PaddleOCR-VL has already grouped physical OCR lines into page-layout
        blocks.  We retain those geometry-derived block boundaries and only
        fold single visual lines *inside* each block.  This is intentionally
        not a keyword, title, sentence, or field heuristic.
        """
        pages = cls._paddleocr_vl_layout_pages(response_body)
        parts: list[tuple[str, bool]] = []
        for page_index, page in enumerate(pages, start=1):
            block_parts: list[tuple[str, bool]] = []
            for block in page.blocks:
                block_text = cls.reflow_ocr_layout_block_text(block.text)
                if block_text:
                    if block_parts:
                        block_parts.append(('', False))
                    block_parts.extend([('<ocr-block>', True), (block_text, False), ('</ocr-block>', True)])
            if parts:
                parts.append(('', False))
            parts.extend([(f'<page index="{page_index}">', True), *block_parts, ('</page>', True)])
        text = cls._join_semantic_parts(parts, structural_lines)
        if not any(not inserted and value.strip() for value, inserted in parts):
            raise ProcessingError('GPU OCR 沒有辨識到可用的版面文字')
        return text

    @classmethod
    def reflow_ocr_layout_block_text(cls, value: str) -> str:
        """Fold only physical visual wraps inside one OCR layout block.

        PaddleOCR-VL's layout parser is the sole authority for a block
        boundary.  Within that block, an empty OCR line is retained as a
        paragraph boundary; ordinary adjacent lines are joined with general
        Unicode spacing rules.  This deliberately has no knowledge of
        government-document labels, keywords, or field names.
        """
        paragraphs: list[str] = []
        current_lines: list[str] = []
        for raw_line in value.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
            line = raw_line.strip()
            if line:
                current_lines.append(line)
                continue
            if current_lines:
                paragraphs.append(cls._join_visual_ocr_lines(current_lines))
                current_lines = []
        if current_lines:
            paragraphs.append(cls._join_visual_ocr_lines(current_lines))
        return '\n\n'.join(part for part in paragraphs if part).strip()

    @classmethod
    def _join_visual_ocr_lines(cls, lines: list[str]) -> str:
        """Join physical lines with generic Unicode spacing, never semantics."""
        joined = ''
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            if not joined:
                joined = line
                continue
            separator = ' ' if cls._needs_inline_space(joined[-1], line[0]) else ''
            joined += separator + line
        return joined

    @staticmethod
    def _needs_inline_space(left: str, right: str) -> bool:
        """Keep a space only for two adjacent Latin/digit word fragments."""
        return left.isascii() and right.isascii() and left.isalnum() and right.isalnum()

    @staticmethod
    def _page_tagged_cloud_text(value: str, *, structural_lines=None) -> str:
        """Represent PDF page boundaries without treating them as document text."""
        pages = [page.strip() for page in value.replace('\r\n', '\n').replace('\r', '\n').split('\f')]
        # pdftotext terminates the final page with a form feed. Interior blank
        # pages still count: their physical numbers must survive PDF splitting.
        if value.rstrip('\r\n').endswith('\f'):
            pages.pop()
        parts = []
        for index, page in enumerate(pages, start=1):
            if parts:
                parts.append(('', False))
            parts.extend([(f'<page index="{index}">', True), (page, False), ('</page>', True)])
        return DocumentOCR._join_semantic_parts(parts, structural_lines)

    @staticmethod
    def _join_semantic_parts(parts, structural_lines=None) -> str:
        """Track provenance while inserting markup, including duplicate literals."""
        lines = []
        for text, inserted in parts:
            # Same normalization as the input bounder, BEFORE computing offsets.
            text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\f', '\n\n<page-break />\n\n')
            if inserted and structural_lines is not None:
                structural_lines.add(len(('\n'.join(lines) + '\n').splitlines()) + 1 if lines else 1)
            lines.extend(text.split('\n'))
        return '\n'.join(lines)

    async def ocr_context(
        self,
        source_path: Path,
    ) -> tuple[tuple[int, ...], dict[str, Any] | None]:
        """Return the structural source selected for the one analysis route.

        This is input classification, not a retry: every scanned or mixed
        PDF must provide a complete PaddleOCR-VL layout reading, while a fully
        native PDF must provide complete raw PDF text in the next stage.
        """
        page_indices = await asyncio.to_thread(
            self._pages_without_native_pdf_text,
            source_path,
        )
        if not page_indices:
            return page_indices, None
        response = await run_ocr(self._paddleocr_vl_response, source_path)
        return page_indices, response

    @staticmethod
    def _pages_without_native_pdf_text(source_path: Path) -> tuple[int, ...]:
        """Find pages needing OCR without mistaking native headers for body text.

        Any displayed raster image may contain scanned text, including pages
        with native headers or logos. OCR the full page conservatively; unused
        image resources do not qualify. Text/vector pages keep native geometry
        when their words are extractable, without content or area heuristics.
        """
        try:
            pymupdf = _pymupdf.load()
        except ImportError as exc:
            raise ProcessingError('伺服器缺少讀取 PDF 所需的 PyMuPDF 套件') from exc

        try:
            with pymupdf.open(str(source_path)) as pdf:
                return tuple(
                    page_index for page_index, page in enumerate(pdf)
                    if not page.get_text('words') or page.get_image_info()
                )
        except (OSError, RuntimeError, ValueError) as exc:
            raise ProcessingError('無法檢查 PDF 文字層') from exc

    @classmethod
    def _paddleocr_vl_layout_pages(
        cls,
        response_body: dict[str, Any],
    ) -> tuple[OcrLayoutPage, ...]:
        """Parse the official PaddleOCR-VL block geometry without reordering it."""
        result = response_body.get('result')
        if not isinstance(result, dict):
            raise ProcessingError('GPU OCR 回傳結果缺少文件內容')
        pages = result.get('layoutParsingResults')
        if not isinstance(pages, list) or not pages:
            raise ProcessingError('GPU OCR 沒有辨識到可用的文件頁面')

        data_info = result.get('dataInfo')
        data_pages = data_info.get('pages') if isinstance(data_info, dict) else None
        parsed_pages: list[OcrLayoutPage] = []
        for page_index, page in enumerate(pages):
            if not isinstance(page, dict):
                raise ProcessingError('GPU OCR 回傳的頁面格式無效')
            pruned = page.get('prunedResult')
            if not isinstance(pruned, dict):
                raise ProcessingError('GPU OCR 回傳的頁面缺少版面資訊')
            metadata_page = (
                data_pages[page_index]
                if isinstance(data_pages, list)
                and page_index < len(data_pages)
                and isinstance(data_pages[page_index], dict)
                else {}
            )
            width = cls._positive_ocr_number(pruned.get('width')) or cls._positive_ocr_number(
                metadata_page.get('width'),
            )
            height = cls._positive_ocr_number(pruned.get('height')) or cls._positive_ocr_number(
                metadata_page.get('height'),
            )
            if width is None or height is None:
                raise ProcessingError('GPU OCR 回傳的頁面缺少尺寸')

            raw_blocks = pruned.get('parsing_res_list')
            blocks: list[OcrLayoutBlock] = []
            if isinstance(raw_blocks, list):
                for raw_block in raw_blocks:
                    if not isinstance(raw_block, dict):
                        continue
                    text = raw_block.get('block_content')
                    if not isinstance(text, str) or not text.strip():
                        continue
                    bbox = cls._ocr_block_bbox(raw_block)
                    if bbox is None:
                        continue
                    raw_label = raw_block.get('block_label')
                    label = raw_label.strip().lower() if isinstance(raw_label, str) else ''
                    blocks.append(OcrLayoutBlock(text=text.strip(), bbox=bbox, label=label))
            parsed_pages.append(OcrLayoutPage(width=width, height=height, blocks=tuple(blocks)))
        return tuple(parsed_pages)

    @classmethod
    def paddleocr_vl_spotting_pages(
        cls,
        response_body: dict[str, Any],
        *,
        allow_empty: bool = False,
    ) -> tuple[OcrLayoutPage, ...]:
        """Parse PaddleOCR-VL's physical-line spotting coordinates.

        The official local endpoint returns one ``rec_texts`` item and one
        four-point ``rec_polys`` polygon for each recognised printed line.
        Pairing them by index preserves the OCR provider's reading order and
        avoids locally inferring line breaks from document wording.
        """
        result = response_body.get('result')
        if not isinstance(result, dict):
            raise ProcessingError('GPU OCR 回傳結果缺少文件內容')
        pages = result.get('layoutParsingResults')
        if not isinstance(pages, list) or not pages:
            raise ProcessingError('GPU OCR 沒有辨識到可用的文件頁面')

        data_info = result.get('dataInfo')
        data_pages = data_info.get('pages') if isinstance(data_info, dict) else None
        parsed_pages: list[OcrLayoutPage] = []
        recognised_lines = 0
        for page_index, page in enumerate(pages):
            if not isinstance(page, dict):
                raise ProcessingError('GPU OCR 回傳的頁面格式無效')
            pruned = page.get('prunedResult')
            if not isinstance(pruned, dict):
                raise ProcessingError('GPU OCR 回傳的頁面缺少版面資訊')
            metadata_page = (
                data_pages[page_index]
                if isinstance(data_pages, list)
                and page_index < len(data_pages)
                and isinstance(data_pages[page_index], dict)
                else {}
            )
            width = cls._positive_ocr_number(pruned.get('width')) or cls._positive_ocr_number(
                metadata_page.get('width'),
            )
            height = cls._positive_ocr_number(pruned.get('height')) or cls._positive_ocr_number(
                metadata_page.get('height'),
            )
            if width is None or height is None:
                raise ProcessingError('GPU OCR 回傳的頁面缺少尺寸')

            spotting = pruned.get('spotting_res')
            texts = spotting.get('rec_texts') if isinstance(spotting, dict) else None
            polygons = spotting.get('rec_polys') if isinstance(spotting, dict) else None
            if not isinstance(texts, list) or not isinstance(polygons, list):
                raise ProcessingError('GPU OCR 回傳結果缺少逐行文字座標')
            blocks: list[OcrLayoutBlock] = []
            for raw_text, polygon in zip(texts, polygons, strict=False):
                if not isinstance(raw_text, str) or not raw_text.strip():
                    continue
                bbox = cls._ocr_block_bbox({'block_polygon_points': polygon})
                if bbox is None:
                    continue
                blocks.append(
                    OcrLayoutBlock(
                        text=raw_text.strip(),
                        bbox=bbox,
                        label='ocr_line',
                    ),
                )
            recognised_lines += len(blocks)
            parsed_pages.append(OcrLayoutPage(width=width, height=height, blocks=tuple(blocks)))

        if recognised_lines == 0 and not allow_empty:
            raise ProcessingError('GPU OCR 沒有辨識到可用的逐行文字')
        return tuple(parsed_pages)

    @staticmethod
    def _positive_ocr_number(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number > 0 else None

    @classmethod
    def _ocr_block_bbox(cls, block: dict[str, Any]) -> tuple[float, float, float, float] | None:
        """Use the OCR polygon when available, otherwise its declared bbox."""
        polygon = block.get('block_polygon_points')
        points: list[tuple[float, float]] = []
        if isinstance(polygon, list):
            for point in polygon:
                if not isinstance(point, (list, tuple)) or len(point) < 2:
                    points = []
                    break
                x = cls._positive_or_zero_ocr_number(point[0])
                y = cls._positive_or_zero_ocr_number(point[1])
                if x is None or y is None:
                    points = []
                    break
                points.append((x, y))
        if len(points) >= 3:
            return cls._normalised_ocr_bbox(
                min(point[0] for point in points),
                min(point[1] for point in points),
                max(point[0] for point in points),
                max(point[1] for point in points),
            )

        raw_bbox = block.get('block_bbox')
        if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) < 4:
            return None
        values = [cls._positive_or_zero_ocr_number(value) for value in raw_bbox[:4]]
        if any(value is None for value in values):
            return None
        x0, y0, x1, y1 = values
        assert x0 is not None and y0 is not None and x1 is not None and y1 is not None
        return cls._normalised_ocr_bbox(x0, y0, x1, y1)

    @staticmethod
    def _positive_or_zero_ocr_number(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number >= 0 else None

    @staticmethod
    def _normalised_ocr_bbox(
        x0: float,
        y0: float,
        x1: float,
        y1: float,
    ) -> tuple[float, float, float, float] | None:
        return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None

    @staticmethod
    def _run_text(arguments: list[str], timeout_seconds: int) -> str:
        try:
            completed = subprocess.run(
                arguments,
                check=False,
                capture_output=True,
                text=True,
                errors='replace',
                shell=False,
                timeout=timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise ProcessingError(f'伺服器缺少必要的文件工具：{arguments[0]}') from exc
        except subprocess.TimeoutExpired as exc:
            raise ProcessingError('文件辨識逾時，請改用較小或較清楚的 PDF') from exc
        if completed.returncode != 0:
            raise ProcessingError('文件文字擷取失敗，請確認 PDF 沒有損毀或加密')
        return completed.stdout
