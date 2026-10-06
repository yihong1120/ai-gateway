from __future__ import annotations

import io
import os
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

from official_document_workflow.application import integration
from official_document_workflow.infrastructure.eip.netask import Netask
from official_document_workflow.infrastructure.office.office_input import to_pdf
from official_document_workflow.infrastructure.office.office_input import validate


def docx_bytes():
    from docx import Document
    out = io.BytesIO()
    doc = Document()
    doc.add_paragraph('Editable Word attachment 2026-09-06')
    doc.add_paragraph('主旨：測試公文。說明：測試中文內容。')
    doc.save(out)
    return out.getvalue()


def stream(payload):
    return io.BytesIO(payload)


def odt_bytes(*, mimetype='application/vnd.oasis.opendocument.text', extra=()):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        archive.writestr('mimetype', mimetype)
        archive.writestr('content.xml', '<document/>')
        archive.writestr('META-INF/manifest.xml', '<manifest/>')
        for name in extra:
            archive.writestr(name, 'test')
    return out.getvalue()


class OfficeInputTests(unittest.IsolatedAsyncioTestCase):
    def test_signatures_and_extensions(self):
        self.assertEqual(validate(stream(docx_bytes()), 'document.DOCX'), '.docx')
        self.assertEqual(validate(stream(b'{\\rtf1 test}'), 'document.doc'), '.doc')
        self.assertEqual(validate(stream(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1test'), 'document.doc'), '.doc')
        self.assertEqual(validate(stream(odt_bytes()), 'document.ODT'), '.odt')
        for payload, name in [(b'<html>login</html>', 'a.doc'), (b'%PDF-test', 'a.docx'), (docx_bytes(), 'a.pdf'), (docx_bytes(), 'a.docm')]:
            with self.assertRaises(zipfile.BadZipFile if name == 'a.docx' else ValueError):
                validate(stream(payload), name)

    def test_non_word_zip_and_macro_rejected(self):
        for names in [['random.txt'], ['[Content_Types].xml', 'word/document.xml', 'word/vbaProject.bin']]:
            out = io.BytesIO()
            with zipfile.ZipFile(out, 'w') as z:
                for name in names:
                    z.writestr(name, 'test')
            with self.assertRaises(ValueError):
                validate(stream(out.getvalue()), 'a.docx')

    def test_odt_requires_text_document_package_and_rejects_scripts(self):
        for data in (
            docx_bytes(), odt_bytes(mimetype='application/vnd.oasis.opendocument.spreadsheet'),
            odt_bytes(extra=('Basic/Module1.xml',)), odt_bytes(extra=('Scripts/python/run.py',)),
        ):
            with self.assertRaises(ValueError):
                validate(stream(data), 'a.odt')

    async def test_pdf_does_not_start_converter(self):
        payload = stream(b'%PDF-original')
        with patch('asyncio.create_subprocess_exec', AsyncMock()) as run:
            self.assertEqual(await to_pdf(payload, 'a.pdf'), (payload, 'a.pdf'))
            run.assert_not_awaited()

    async def test_spooled_pdf_remains_streamed(self):
        with tempfile.SpooledTemporaryFile(max_size=4, mode='w+b') as payload:
            payload.write(b'%PDF-original')
            converted, name = await to_pdf(payload, 'a.pdf')
            self.assertIs(converted, payload)
            self.assertEqual(name, 'a.pdf')
            self.assertEqual(converted.read(), b'%PDF-original')

    async def test_source_normalized_before_private_file_storage(self):
        pipe = SimpleNamespace(_store_derived_bytes=AsyncMock(return_value='file'))
        converted = stream(b'%PDF-converted')
        with patch.object(integration, 'configured_pipe', AsyncMock(return_value=pipe)), patch('official_document_workflow.infrastructure.office.office_input.to_pdf', AsyncMock(return_value=(converted, 'a.pdf'))) as convert:
            payload = stream(b'word')
            await integration.source_file('alice', payload, 'a.doc')
            convert.assert_awaited_once_with(payload, 'a.doc')
            args = pipe._store_derived_bytes.call_args.kwargs
            self.assertEqual(args['user_id'], 'alice')
            self.assertEqual(args['display_name'], 'a.pdf')
            self.assertEqual(args['content_type'], 'application/pdf')

    async def test_remote_attachment_list_includes_word_but_not_programs(self):
        adapter = Netask()
        try:
            meta = ['']*18
            meta[17] = '5'
            rows = [[None, None, name, '/fo/'+name, None, None, None]
                    for name in ['a.pdf', 'b.docx', 'c.DOC', 'd.exe', 'e.odt']]
            adapter.rpc = AsyncMock(return_value=[[0], meta, *rows])
            self.assertEqual([a['name'] for a in await adapter.attachments('1', 'sign')], ['a.pdf', 'b.docx', 'c.DOC', 'e.odt'])
        finally:
            await adapter.close()

    async def test_failed_container_does_not_run_host_office(self):
        process = SimpleNamespace(returncode=1, wait=AsyncMock(return_value=1))
        with patch('asyncio.create_subprocess_exec', AsyncMock(return_value=process)) as run, \
                patch('subprocess.run') as remove:
            with self.assertRaises(ValueError):
                await to_pdf(stream(docx_bytes()), 'a.docx')
            args = run.call_args.args
            self.assertIn('--network=none', args)
            self.assertIn('--read-only', args)
            self.assertIn('--cap-drop=ALL', args)
            self.assertIn('--pull=never', args)
            self.assertEqual(remove.call_args.args[0][:3], ['docker', 'rm', '-f'])

    async def test_converter_timeout_propagates_and_reaps_process(self):
        process = SimpleNamespace(
            returncode=None, wait=AsyncMock(
                side_effect=[TimeoutError('conversion timeout'), 0],
            ), kill=Mock(),
        )
        with patch('asyncio.create_subprocess_exec', AsyncMock(return_value=process)) as run, \
                patch('subprocess.run') as remove:
            with self.assertRaisesRegex(TimeoutError, 'conversion timeout'):
                await to_pdf(stream(docx_bytes()), 'a.docx')
        run.assert_awaited_once()
        remove.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.await_count, 2)

    async def test_cleanup_failure_still_reaps_conversion_process(self):
        process = SimpleNamespace(returncode=None, wait=AsyncMock(side_effect=[TimeoutError(), 0]), kill=Mock())
        with patch('asyncio.create_subprocess_exec', AsyncMock(return_value=process)), \
                patch('subprocess.run', side_effect=FileNotFoundError('docker disappeared')):
            with self.assertRaises(FileNotFoundError):
                await to_pdf(stream(docx_bytes()), 'a.docx')
        process.kill.assert_called_once()
        self.assertEqual(process.wait.await_count, 2)

    @unittest.skipUnless(os.getenv('TEST_EIP_OFFICE') == '1', 'requires installed isolated office image')
    async def test_real_docx_to_pdf_text(self):
        import fitz
        payload, name = await to_pdf(stream(docx_bytes()), '測試.docx')
        self.assertEqual(name, '測試.pdf')
        with fitz.open(stream=payload.read(), filetype='pdf') as doc:
            text = ''.join(page.get_text() for page in doc)
            self.assertIn('Editable Word attachment', text)
            self.assertIn('測試中文內容', text)
