"""Mixed upload access, conversion lifecycle, and existing PDF batch routing."""
from __future__ import annotations

import asyncio
import io
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch

import pymupdf

from .test_official_document_processor_pipe import PIPE_MODULE as M
from official_document_workflow.infrastructure.office import office_input


def pdf_bytes(pages=1):
    with pymupdf.open() as pdf:
        for n in range(pages):
            pdf.new_page().insert_text((72, 72), f'Page {n + 1}')
        return pdf.tobytes()


class MixedOfficeUploads(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pipe = M.Pipe()
        self.files = {}
        self.paths = []
        self.attachments = []

        async def owned(file_id, user_id):
            file = self.files.get(file_id)
            return file if file and file.user_id == user_id else None
        patches = [
            patch.object(M.Files, 'get_file_by_id_and_user_id', AsyncMock(side_effect=owned), create=True),
            patch.object(M.Storage, 'get_file', side_effect=lambda path: path, create=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def add(self, name, payload=b'office-input', owner='alice', content_type='application/octet-stream'):
        file_id = str(len(self.files) + 1)
        path = self.root / file_id
        path.write_bytes(payload)
        file = types.SimpleNamespace(
            id=file_id, user_id=owner, filename=name,
            path=str(path), meta={'content_type': content_type},
        )
        self.files[file_id] = file
        self.attachments.append({'id': file_id, 'type': 'file'})
        return file

    def watch_temporary_directories(self):
        original = tempfile.TemporaryDirectory

        def make(*args, **kwargs):
            directory = original(*args, **kwargs)
            self.paths.append(Path(directory.name))
            return directory
        return patch.object(M.tempfile, 'TemporaryDirectory', side_effect=make)

    async def test_ten_mixed_files_use_saved_types_acl_and_natural_order(self):
        for n in range(10, 0, -1):
            self.add(f'file-{n}' + ('.pdf', '.DOCX', '.doc', '.ODT')[n % 4])
        self.add('private.docx', owner='bob')
        self.attachments.append(self.attachments[0])
        with patch.object(office_input, 'to_pdf', AsyncMock()) as convert:
            kind, files = await self.pipe._get_owned_source_files(self.attachments, 'alice')
        self.assertEqual(kind, 'pdf')  # Normalized batch route, including Office inputs.
        self.assertEqual([f.filename.split('.')[0] for f in files], [f'file-{n}' for n in range(1, 11)])
        convert.assert_not_awaited()
        self.add('file-11.odt')
        with self.assertRaisesRegex(M.ProcessingError, '10 份文件'):
            await self.pipe._get_owned_source_files(self.attachments, 'alice')

    async def test_office_and_photos_remain_separate_upload_modes(self):
        self.add('main.odt')
        self.add('photo.png')
        with self.assertRaisesRegex(M.ProcessingError, '圖片請另批上傳'):
            await self.pipe._get_owned_source_files(self.attachments, 'alice')

    async def test_mixed_preparation_preserves_originals_and_cleans_converted_pdfs(self):
        pdf = pdf_bytes()
        for suffix in ('.pdf', '.doc', '.docx', '.odt'):
            self.add('source' + suffix, pdf if suffix == '.pdf' else b'office-' + suffix.encode())
        before = {f.path: Path(f.path).read_bytes() for f in self.files.values()}
        converted = pdf_bytes(2)
        with patch.object(office_input, 'to_pdf', AsyncMock(side_effect=lambda *_: (io.BytesIO(converted), 'source.pdf'))) as convert:
            prepared = await self.pipe._prepare_source_documents(self.attachments, 'alice')
        self.assertEqual(len(prepared), 4)
        self.assertEqual(convert.await_count, 3)
        for item in prepared:
            self.assertEqual(item.is_original_pdf, item.source_file.filename.endswith('.pdf'))
            with pymupdf.open(item.pdf_path) as doc:
                self.assertEqual(doc.page_count, 1 if item.is_original_pdf else 2)
            output = item.pdf_path
            item.cleanup()
            self.assertEqual(output.exists(), item.is_original_pdf)
        self.assertEqual({p: Path(p).read_bytes() for p in before}, before)

    async def test_office_mime_can_supply_missing_suffix_without_renaming_upload(self):
        original = self.add('公文', content_type='application/vnd.oasis.opendocument.text')
        with patch.object(office_input, 'to_pdf', AsyncMock(return_value=(io.BytesIO(pdf_bytes()), '公文.pdf'))) as convert:
            items = await self.pipe._prepare_source_documents(self.attachments, 'alice')
        try:
            self.assertEqual(convert.await_args.args[1], '公文.odt')
            self.assertEqual(original.filename, '公文')
            self.assertFalse(items[0].is_original_pdf)
        finally:
            items[0].cleanup()

    async def test_failed_or_cancelled_conversion_cleans_all_prior_pdfs(self):
        self.add('1.docx')
        self.add('2.odt')
        for failure in (ValueError('invalid office'), asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__), self.watch_temporary_directories(), \
                    patch.object(office_input, 'to_pdf', AsyncMock(side_effect=[(io.BytesIO(pdf_bytes()), '1.pdf'), failure])):
                with self.assertRaises(type(failure)):
                    await self.pipe._prepare_source_documents(self.attachments, 'alice')
            self.assertTrue(all(not p.exists() for p in self.paths))
        self.assertTrue(all(Path(f.path).exists() for f in self.files.values()))

    async def test_office_size_checked_before_conversion(self):
        self.pipe.valves.max_pdf_size_mb = 1
        self.add('huge.docx', b'x' * (1024 * 1024 + 1))
        with patch.object(office_input, 'to_pdf', AsyncMock()) as convert:
            with self.assertRaisesRegex(M.ProcessingError, '文件超過 1 MB'):
                await self.pipe._prepare_source_documents(self.attachments, 'alice')
        convert.assert_not_awaited()

    async def test_normalized_pdf_page_and_byte_limits_clean_temporary_outputs(self):
        self.add('1.docx')
        for cap in ('max_pdf_pages', 'max_batch_pdf_total_pages', 'max_batch_pdf_total_size_mb'):
            self.pipe = M.Pipe()
            setattr(self.pipe.valves, cap, 1)
            data = pdf_bytes(2)
            if cap.endswith('size_mb'):
                data += b' ' * (1024 * 1024)
            with self.subTest(cap=cap), self.watch_temporary_directories(), \
                    patch.object(office_input, 'to_pdf', AsyncMock(return_value=(io.BytesIO(data), '1.pdf'))):
                with self.assertRaisesRegex(M.ProcessingError, '超過'):
                    await self.pipe._prepare_source_documents(self.attachments, 'alice')
            self.assertTrue(all(not p.exists() for p in self.paths))

    async def test_chat_routes_office_into_existing_batch_and_always_cleans(self):
        self.add('main.docx')
        seen = []

        async def batch(pipe, module, owner, items, *args):
            self.assertEqual(owner, 'alice')
            self.assertEqual(items[0].kind, 'office')
            self.assertTrue(items[0].pdf_path.exists())
            seen.extend(item.pdf_path for item in items)
            return 'batch output'
        with patch.object(office_input, 'to_pdf', AsyncMock(return_value=(io.BytesIO(pdf_bytes()), 'main.pdf'))), \
                patch('official_document_workflow.application.upload_batch.process_uploads', AsyncMock(side_effect=batch)) as run:
            result = await self.pipe.pipe({'messages': []}, __user__={'id': 'alice'}, __files__=self.attachments)
        self.assertEqual(result, 'batch output')
        run.assert_awaited_once()
        self.assertTrue(all(not p.exists() for p in seen))
        self.assertTrue(all(Path(f.path).exists() for f in self.files.values()))
