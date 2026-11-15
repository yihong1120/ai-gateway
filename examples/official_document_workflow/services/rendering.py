"""Stateless document rendering operations, independent of request and host state."""

from __future__ import annotations

import math
import unicodedata
from pathlib import Path
from typing import Any

from examples.official_document_workflow.domain.documents import (
    DOCX_INVALID_TEXT_CHARS,
    DOCX_UNIFIED_FONT,
    INVALID_FILENAME_CHARS,
    WHITESPACE,
    OcrLayoutBlock,
    OcrLayoutPage,
    OfficialDocument,
    ProcessingError,
)


class DocumentRendering:
    def pdf_filename(
        self, document: OfficialDocument, *, suffix: str = '', attachment_label: str = '', attachment_title: str = ''
    ) -> str:
        date_part = document.issued.compact_roc
        number_part = self.safe_filename_part(document.reference_number, 72)
        # Reserve space for the longer DOCX extension so both formats share a stem.
        attachment_part = self.safe_filename_part(attachment_label, 60) + '-' if attachment_label else ''
        fixed = len((f'{date_part}_{number_part}_{attachment_part}{suffix}.docx').encode())
        subject_part = self.safe_filename_subject_part(
            attachment_title or document.subject,
            max(32, 200 - fixed),
        )
        return f'{date_part}_{number_part}_{attachment_part}{subject_part}{suffix}.pdf'

    def _csv_filename(self, document: OfficialDocument) -> str:
        number_part = self.safe_filename_part(document.reference_number, 72)
        return f'{document.issued.compact_roc}_{number_part}.csv'

    def _docx_filename(self, document: OfficialDocument) -> str:
        """Return the canonical editable-Word counterpart of the renamed PDF."""
        return self.pdf_filename(document)[:-4] + '.docx'

    @staticmethod
    def native_pdf_layout_pages(source_path: Path) -> tuple[OcrLayoutPage, ...]:
        """Use an existing PDF text layer and its coordinates for editable Word.

        OCR remains necessary for scanned pages. A native PDF already contains
        more accurate glyphs and geometry, so running OCR over it can corrupt
        uncommon names or document numbers even though the source text is exact.
        """
        try:
            import pymupdf
        except ImportError as exc:
            raise ProcessingError('伺服器缺少讀取 PDF 文字層所需的套件') from exc

        pages: list[OcrLayoutPage] = []
        try:
            with pymupdf.open(str(source_path)) as pdf:
                for page in pdf:
                    blocks: list[OcrLayoutBlock] = []
                    page_dict = page.get_text('dict')
                    for raw_block in page_dict.get('blocks', []):
                        if raw_block.get('type') != 0:
                            continue
                        for line in raw_block.get('lines', []):
                            spans = line.get('spans', [])
                            text = ''.join(
                                span.get('text', '') for span in spans if isinstance(span.get('text'), str)
                            ).strip()
                            bbox = line.get('bbox')
                            if not text or not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                                continue
                            x0, y0, x1, y1 = (float(value) for value in bbox)
                            if x1 <= x0 or y1 <= y0:
                                continue
                            blocks.append(
                                OcrLayoutBlock(
                                    text=text,
                                    bbox=(x0, y0, x1, y1),
                                    label='native_line',
                                ),
                            )
                    pages.append(
                        OcrLayoutPage(
                            width=float(page.rect.width),
                            height=float(page.rect.height),
                            blocks=tuple(blocks),
                        ),
                    )
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            raise ProcessingError('無法讀取 PDF 原生文字座標') from exc
        if not pages or not any(page.blocks for page in pages):
            raise ProcessingError('PDF 原生文字層沒有可定位的文字')
        return tuple(pages)

    @classmethod
    def _editable_ocr_placements(
        cls,
        page_image: Any,
        layout: OcrLayoutPage,
        *,
        page_width_pt: float,
        page_height_pt: float,
    ) -> list[tuple[OcrLayoutBlock, float, float, float, float, float]]:
        """Choose geometry-valid neutral-ink OCR blocks for Word page frames.

        The decision deliberately uses only OCR structural labels, coordinates
        and source-image colour.  It never uses the document's wording or
        field-name keywords.  Marginal coloured/seal/signature regions stay in
        the image background because turning uncertain OCR noise into editable
        black text would make the exported page less faithful.
        """
        placements: list[tuple[OcrLayoutBlock, float, float, float, float, float]] = []
        for block in layout.blocks:
            if block.label in {'aside_text', 'paragraph_title'}:
                continue
            position = cls._ocr_block_page_points(
                block,
                layout,
                page_width_pt=page_width_pt,
                page_height_pt=page_height_pt,
            )
            if position is None:
                continue
            if cls._ocr_block_has_coloured_ink(page_image, block, layout):
                continue
            x_pt, y_pt, width_pt, height_pt = position
            font_size_pt = cls._ocr_frame_font_size_pt(
                block.text,
                width_pt=width_pt,
                height_pt=height_pt,
            )
            placements.append((block, x_pt, y_pt, width_pt, height_pt, font_size_pt))
        return placements

    @staticmethod
    def _ocr_block_page_points(
        block: OcrLayoutBlock,
        layout: OcrLayoutPage,
        *,
        page_width_pt: float,
        page_height_pt: float,
    ) -> tuple[float, float, float, float] | None:
        """Map a visual PaddleOCR block rectangle to the rendered Word page."""
        x0, y0, x1, y1 = block.bbox
        x0 = min(max(x0, 0.0), layout.width)
        y0 = min(max(y0, 0.0), layout.height)
        x1 = min(max(x1, 0.0), layout.width)
        y1 = min(max(y1, 0.0), layout.height)
        if x1 <= x0 or y1 <= y0:
            return None
        x_pt = x0 * page_width_pt / layout.width
        y_pt = y0 * page_height_pt / layout.height
        width_pt = min(page_width_pt - x_pt, (x1 - x0) * page_width_pt / layout.width)
        height_pt = min(page_height_pt - y_pt, (y1 - y0) * page_height_pt / layout.height)
        # Native Word glyph metrics can slightly exceed an OCR rectangle.
        # Keep a small bounded safety margin to avoid clipping editable text.
        width_pt = min(page_width_pt - x_pt, width_pt + 2.0)
        height_pt = min(page_height_pt - y_pt, height_pt + 5.0)
        if width_pt < 2.0 or height_pt < 2.0:
            return None
        return x_pt, y_pt, width_pt, height_pt

    @staticmethod
    def _ocr_block_has_coloured_ink(
        page_image: Any,
        block: OcrLayoutBlock,
        layout: OcrLayoutPage,
    ) -> bool:
        """Return whether a block is primarily coloured source ink.

        Text and stamps cannot be distinguished safely from OCR content alone.
        Sampling source-image saturation keeps coloured seals/signatures in
        the background without treating any particular text as special.
        """
        image_width, image_height = page_image.size
        x0, y0, x1, y1 = block.bbox
        left = max(0, min(image_width - 1, round(x0 * image_width / layout.width)))
        top = max(0, min(image_height - 1, round(y0 * image_height / layout.height)))
        right = max(left + 1, min(image_width, round(x1 * image_width / layout.width)))
        bottom = max(top + 1, min(image_height, round(y1 * image_height / layout.height)))
        step_x = max(1, (right - left) // 64)
        step_y = max(1, (bottom - top) // 32)
        pixels = page_image.load()
        dark_samples = 0
        coloured_samples = 0
        for y in range(top, bottom, step_y):
            for x in range(left, right, step_x):
                red, green, blue = pixels[x, y]
                high = max(red, green, blue)
                low = min(red, green, blue)
                if high > 230:
                    continue
                dark_samples += 1
                if high - low >= 50:
                    coloured_samples += 1
        return dark_samples >= 8 and coloured_samples / dark_samples >= 0.35

    @staticmethod
    def _ocr_frame_font_size_pt(text: str, *, width_pt: float, height_pt: float) -> float:
        """Choose a font size from the OCR block's area and visual width.

        PaddleOCR-VL can return several printed lines as one text block without
        preserving the source line breaks.  Sizing such a block against its
        entire unbroken string makes Word text unrealistically tiny. Estimate
        the number of visual lines from the block area instead; Word performs
        the actual wrapping inside its fixed page-anchored frame.
        """
        lines = text.splitlines() or [text]
        explicit_line_count = max(1, len(lines))

        def visual_width(value: str) -> float:
            width = 0.0
            for character in value:
                if character.isspace():
                    width += 0.35
                elif unicodedata.east_asian_width(character) in {'F', 'W', 'A'}:
                    width += 1.0
                else:
                    width += 0.56
            return max(1.0, width)

        total_width = sum(visual_width(line) for line in lines)
        longest_line = max(visual_width(line) for line in lines)
        inferred_line_count = max(
            explicit_line_count,
            int(round(math.sqrt((total_width * height_pt) / max(1.0, 1.25 * width_pt)))),
        )
        horizontal_limit = width_pt / max(1.0, total_width / inferred_line_count)
        # An OCR block with explicit source line breaks must still fit its
        # longest explicit line, whereas an unbroken OCR paragraph may use
        # ordinary Word wrapping to reproduce the original visual lines.
        if explicit_line_count > 1:
            horizontal_limit = min(horizontal_limit, width_pt / longest_line)
        vertical_limit = height_pt / max(1.0, inferred_line_count * 1.25)
        # A little slack avoids visible clipping from Word's glyph metrics.
        return max(5.5, min(28.0, horizontal_limit, vertical_limit) * 0.91)

    @classmethod
    def _erase_ocr_text_from_page_image(
        cls,
        page_image: Any,
        placements: list[tuple[OcrLayoutBlock, float, float, float, float, float]],
        layout: OcrLayoutPage,
    ) -> None:
        """Clear only blocks that will be replaced by visible Word text."""
        try:
            from PIL import ImageDraw
        except ImportError as exc:
            raise ProcessingError('伺服器缺少建立可編輯掃描 DOCX 所需的影像套件') from exc

        painter = ImageDraw.Draw(page_image)
        image_width, image_height = page_image.size
        for block, _, _, _, _, _ in placements:
            x0, y0, x1, y1 = block.bbox
            left = x0 * image_width / layout.width
            top = y0 * image_height / layout.height
            right = x1 * image_width / layout.width
            bottom = y1 * image_height / layout.height
            # Geometry-scaled padding masks edge antialiasing without
            # deliberately inspecting or rewriting document wording.
            pad_x = max(2.0, (right - left) * 0.012)
            pad_y = max(3.0, (bottom - top) * 0.10)
            painter.rectangle(
                (
                    max(0.0, left - pad_x),
                    max(0.0, top - pad_y),
                    min(image_width, right + pad_x),
                    min(image_height, bottom + pad_y),
                ),
                fill=(255, 255, 255),
            )

    @staticmethod
    def _set_docx_unified_normal_font(document: Any) -> None:
        """Apply the required common font to newly-created Word documents."""
        try:
            from docx.oxml.ns import qn
            from docx.shared import Pt
        except ImportError as exc:
            raise ProcessingError('伺服器缺少建立 DOCX 所需的 python-docx 套件') from exc

        normal_style = document.styles['Normal']
        normal_style.font.name = DOCX_UNIFIED_FONT
        normal_style.font.size = Pt(11)
        normal_fonts = normal_style._element.get_or_add_rPr().get_or_add_rFonts()
        for attribute in ('w:ascii', 'w:hAnsi', 'w:eastAsia', 'w:cs'):
            normal_fonts.set(qn(attribute), DOCX_UNIFIED_FONT)

    @staticmethod
    def _append_docx_ocr_frame_paragraph(
        document: Any,
        *,
        block: OcrLayoutBlock,
        x_pt: float,
        y_pt: float,
        width_pt: float,
        height_pt: float,
        font_size_pt: float,
    ) -> None:
        """Append one editable, page-anchored native Word paragraph.

        ``w:framePr`` deliberately uses regular Word paragraphs rather than
        VML drawing textboxes. This makes the OCR text discoverable and
        editable through normal Word APIs while its page-relative frame keeps
        neighbouring OCR blocks from flowing when one block is edited.
        """
        try:
            from docx.oxml import OxmlElement
            from docx.oxml.ns import qn
            from docx.shared import Pt
        except ImportError as exc:
            raise ProcessingError('伺服器缺少建立可編輯掃描 DOCX 所需的套件') from exc

        safe_text = DOCX_INVALID_TEXT_CHARS.sub(
            '',
            block.text.replace('\r\n', '\n').replace('\r', '\n'),
        )
        if not safe_text.strip():
            return

        paragraph = document.add_paragraph()
        paragraph.paragraph_format.space_before = Pt(0)
        paragraph.paragraph_format.space_after = Pt(0)
        paragraph.paragraph_format.line_spacing = Pt(max(1.0, font_size_pt * 1.10))
        paragraph_properties = paragraph._p.get_or_add_pPr()
        frame = OxmlElement('w:framePr')
        for attribute, value in {
            'w:w': str(max(1, round(width_pt * 20))),
            'w:h': str(max(1, round(height_pt * 20))),
            'w:hRule': 'exact',
            'w:x': str(round(x_pt * 20)),
            'w:y': str(round(y_pt * 20)),
            'w:hAnchor': 'page',
            'w:vAnchor': 'page',
            'w:wrap': 'notBeside',
            'w:anchorLock': '1',
        }.items():
            frame.set(qn(attribute), value)
        paragraph_properties.insert(0, frame)
        if block.label == 'doc_title':
            justification = OxmlElement('w:jc')
            justification.set(qn('w:val'), 'center')
            paragraph_properties.append(justification)

        run = paragraph.add_run()
        run_properties = run._element.get_or_add_rPr()
        fonts = run_properties.get_or_add_rFonts()
        for attribute in ('w:ascii', 'w:hAnsi', 'w:eastAsia', 'w:cs'):
            fonts.set(qn(attribute), DOCX_UNIFIED_FONT)
        font_size = str(max(2, round(font_size_pt * 2)))
        for tag in ('w:sz', 'w:szCs'):
            size = OxmlElement(tag)
            size.set(qn('w:val'), font_size)
            run_properties.append(size)
        for line_index, line in enumerate(safe_text.split('\n')):
            if line_index:
                run.add_break()
            run.add_text(line)

    @staticmethod
    def _anchor_docx_picture_to_page(inline: Any, *, picture_id: int) -> None:
        """Turn a python-docx inline picture into a full-page Word anchor.

        ``python-docx`` only exposes inline pictures.  Retagging that exact
        OOXML element (instead of rebuilding it) retains the image
        relationship and its calculated extent while preventing Word from
        laying it out as a paragraph.  The page raster is therefore pinned to
        the same top-left coordinate on every page.
        """
        try:
            from docx.oxml import OxmlElement
            from docx.oxml.ns import qn
        except ImportError as exc:
            raise ProcessingError('伺服器缺少建立保留版面 DOCX 所需的套件') from exc

        doc_properties = inline.find(qn('wp:docPr'))
        if doc_properties is None:
            raise ProcessingError('無法定位 Word 頁面影像')

        # Preserve existing image OOXML and relationships, only changing the
        # positioning wrapper from ``wp:inline`` to ``wp:anchor``.
        anchor = inline
        anchor.tag = qn('wp:anchor')
        for attribute, value in {
            'distT': '0',
            'distB': '0',
            'distL': '0',
            'distR': '0',
            'simplePos': '0',
            'relativeHeight': '0',
            # This page is an image facsimile, so it must not push any
            # accidental empty paragraph onto another page.
            'behindDoc': '1',
            'locked': '0',
            'layoutInCell': '1',
            'allowOverlap': '1',
        }.items():
            anchor.set(attribute, value)

        simple_position = OxmlElement('wp:simplePos')
        simple_position.set('x', '0')
        simple_position.set('y', '0')
        anchor.insert(0, simple_position)
        for axis in ('H', 'V'):
            position = OxmlElement(f'wp:position{axis}')
            position.set('relativeFrom', 'page')
            offset = OxmlElement('wp:posOffset')
            offset.text = '0'
            position.append(offset)
            anchor.insert(1 if axis == 'H' else 2, position)

        # Word expects wrap immediately after extent. The native inline
        # extent stays at index 3 after the three coordinate elements.
        anchor.insert(4, OxmlElement('wp:wrapNone'))
        doc_properties.set('id', str(picture_id))
        doc_properties.set('name', f'掃描公文第{picture_id}頁')

    @staticmethod
    def _docx_download_label() -> str:
        """Label the one page-anchored editable Word export."""
        return '可編輯版面 DOCX（OCR 逐行定位文字；請校對）'

    @staticmethod
    def _clean_filename_part(value: str) -> str:
        value = INVALID_FILENAME_CHARS.sub(' ', value)
        value = WHITESPACE.sub(' ', value).strip(' ._')
        if not value:
            return '未命名'
        return value

    @classmethod
    def safe_filename_part(cls, value: str, maximum_bytes: int) -> str:
        value = cls._clean_filename_part(value)
        while len(value.encode('utf-8')) > maximum_bytes:
            value = value[:-1]
        return value.rstrip(' ._') or '未命名'

    @classmethod
    def safe_filename_subject_part(cls, value: str, maximum_bytes: int) -> str:
        """Bound a subject without pretending a byte-truncated phrase is whole.

        The verified full subject supplies the filename. If
        it exceeds the filesystem budget, this final technical guard
        uses Unicode sentence punctuation or spacing as a generic boundary and
        marks the omitted tail with an ellipsis.  It contains no document
        terminology or local semantic inference.
        """
        value = cls._clean_filename_part(value)
        if len(value.encode('utf-8')) <= maximum_bytes:
            return value

        ellipsis = '…'
        content_budget = maximum_bytes - len(ellipsis.encode('utf-8'))
        if content_budget <= 0:
            return cls.safe_filename_part(ellipsis, maximum_bytes)

        prefix = cls.safe_filename_part(value, content_budget)
        # Prefer a general Unicode sentence/phrase boundary that is not an
        # identifier connector (such as an underscore).  The lower bound keeps
        # an incidental early comma from discarding most of the useful title.
        boundary_minimum = max(1, math.floor(content_budget * 0.6))
        boundary = ''
        for index, character in enumerate(prefix):
            category = unicodedata.category(character)
            if category == 'Po' or category.startswith('Z'):
                candidate = prefix[:index].rstrip(' ._')
                if candidate and len(candidate.encode('utf-8')) >= boundary_minimum:
                    boundary = candidate
        return f'{boundary or prefix.rstrip(" ._") or "未命名"}{ellipsis}'
