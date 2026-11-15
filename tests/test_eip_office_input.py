from __future__ import annotations

import asyncio
import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

from examples.official_document_workflow.application import integration
from examples.official_document_workflow.infrastructure.eip.netask import Netask
from examples.official_document_workflow.infrastructure.office import office_input
from examples.official_document_workflow.infrastructure.office.office_input import to_pdf
from examples.official_document_workflow.infrastructure.office.office_input import validate
from examples.official_document_workflow.runtime import current_resources
from examples.shared.runtime.modules import ModuleReference
from tests.eip_postgres import EIPPostgresTestCase
from tests.test_official_document_processor_pipe import PIPE_MODULE as M

_docx = ModuleReference('docx')
_fitz = ModuleReference('fitz')
_pymupdf = ModuleReference('pymupdf')


def docx_bytes():
    Document = _docx.attribute('Document')
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
        pipe = SimpleNamespace(store_output=AsyncMock(return_value='file'))
        converted = stream(b'%PDF-converted')
        with patch.object(integration, 'configured_pipe', AsyncMock(return_value=pipe)), \
                patch.object(integration, 'current_resources', return_value=SimpleNamespace(shared_content=None)), \
                patch('examples.official_document_workflow.infrastructure.office.office_input.to_pdf', AsyncMock(return_value=(converted, 'a.pdf'))) as convert:
            payload = stream(b'word')
            await integration.source_file('alice', payload, 'a.doc')
            convert.assert_awaited_once_with(payload, 'a.doc', content_cache=None)
            args = pipe.store_output.call_args.kwargs
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
        fitz = _fitz.load()
        payload, name = await to_pdf(stream(docx_bytes()), '測試.docx')
        self.assertEqual(name, '測試.pdf')
        with fitz.open(stream=payload.read(), filetype='pdf') as doc:
            text = ''.join(page.get_text() for page in doc)
            self.assertIn('Editable Word attachment', text)
            self.assertIn('測試中文內容', text)


class SharedOfficeInputTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_same_authorized_bytes_convert_once_but_return_independent_streams(self):
        cache = await asyncio.to_thread(lambda: current_resources().shared_content)
        data = docx_bytes()
        with patch.object(office_input, '_convert_pdf_bytes', AsyncMock(return_value=b'%PDF-converted')) as convert:
            alice, alice_name = await to_pdf(stream(data), 'alice.docx', content_cache=cache)
            bob, bob_name = await to_pdf(stream(data), 'bob.docx', content_cache=cache)
        convert.assert_awaited_once()
        self.assertIsNot(alice, bob)
        self.assertEqual((alice_name, bob_name), ('alice.pdf', 'bob.pdf'))
        alice.close()
        with bob:
            self.assertEqual(bob.read(), b'%PDF-converted')

    async def test_source_format_is_validated_before_any_cache_access(self):
        cache = Mock()
        with self.assertRaises(ValueError), patch.object(office_input, '_convert_pdf_bytes', AsyncMock()) as convert:
            await to_pdf(stream(b'<html>login</html>'), 'a.doc', content_cache=cache)
        cache.key.assert_not_called()
        cache.aget_or_compute.assert_not_called()
        convert.assert_not_awaited()

    async def test_different_source_bytes_or_converter_revision_do_not_share(self):
        cache = await asyncio.to_thread(lambda: current_resources().shared_content)
        with patch.object(office_input, '_convert_pdf_bytes', AsyncMock(return_value=b'%PDF-converted')) as convert:
            for data in (b'{\\rtf1 first}', b'{\\rtf1 second}'):
                output, _ = await to_pdf(stream(data), 'a.doc', content_cache=cache)
                output.close()
            with patch.object(office_input, 'OFFICE_IMAGE', 'ai-gateway/eip-office:next'):
                output, _ = await to_pdf(stream(b'{\\rtf1 first}'), 'a.doc', content_cache=cache)
                output.close()
            with patch.object(office_input, 'CONVERSION_REVISION', 'office-pdf-v2'):
                output, _ = await to_pdf(stream(b'{\\rtf1 first}'), 'a.doc', content_cache=cache)
                output.close()
        self.assertEqual(convert.await_count, 4)

    async def test_invalid_cached_pdf_is_never_returned_to_source_storage(self):
        cache = SimpleNamespace(key=Mock(return_value='key'), aget_or_compute=AsyncMock(return_value=b'not a PDF'))
        with self.assertRaisesRegex(ValueError, '格式不符'):
            await to_pdf(stream(b'{\\rtf1 test}'), 'a.doc', content_cache=cache)


class SharedOfficeSourceTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.files = {}
        self.original = docx_bytes()
        self.cache = current_resources().shared_content
        self.pipe = M.Pipe()
        self.pipe.content_cache = self.cache

        async def owned(file_id, owner):
            file = self.files.get(file_id)
            return file if file is not None and file.user_id == owner else None

        self.enterContext(
            patch.object(
                M.Files, 'get_file_by_id_and_user_id',
                AsyncMock(side_effect=owned), create=True,
            ),
        )
        self.local = self.enterContext(patch.object(M.Storage, 'get_file', side_effect=lambda path: path, create=True))

    def upload(self, owner):
        path = self.root / owner
        path.write_bytes(self.original)
        file = SimpleNamespace(
            id=owner + '-upload', user_id=owner, filename=owner + '-private-original', path=str(path),
            meta={'content_type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'},
        )
        self.files[file.id] = file
        return file

    async def prepare(self, file, owner):
        prepared = await self.pipe.sources._prepare_source_documents([{'id': file.id, 'type': 'file'}], owner)
        for item in prepared:
            self.addCleanup(item.cleanup)
        return prepared[0]

    @staticmethod
    def pdf(pages=1):
        pymupdf = _pymupdf.load()
        with pymupdf.open() as pdf:
            for number in range(pages):
                pdf.new_page().insert_text((30, 30), f'Synthetic page {number + 1}')
            return pdf.tobytes()

    async def test_upload_preparation_and_eip_storage_share_conversion_keep_private_sources(self):
        alice, bob = self.upload('alice'), self.upload('bob')
        converted = self.pdf()
        saved = []

        async def save(**values):
            saved.append({**values, 'bytes': values['payload'].getvalue()})
            return SimpleNamespace(id='bob-eip-source', filename=values['display_name'])

        eip_input = io.BytesIO(self.original)
        with patch.object(office_input, '_convert_pdf_bytes', AsyncMock(return_value=converted)) as convert, \
                patch.object(integration, 'configured_pipe', AsyncMock(return_value=SimpleNamespace(store_output=AsyncMock(side_effect=save)))):
            first = await self.prepare(alice, 'alice')
            eip_file = await integration.source_file('bob', eip_input, 'bob-eip.docx')
            second = await self.prepare(bob, 'bob')
        convert.assert_awaited_once()
        self.assertEqual(first.pdf_path.read_bytes(), converted)
        self.assertEqual(second.pdf_path.read_bytes(), converted)
        self.assertNotEqual(first.pdf_path, second.pdf_path)
        self.assertEqual((first.original_path, second.original_path), (Path(alice.path), Path(bob.path)))
        self.assertEqual((first.source_file.id, second.source_file.id), (alice.id, bob.id))
        self.assertEqual(eip_file.id, 'bob-eip-source')
        self.assertEqual(saved[0]['bytes'], converted)
        self.assertEqual(saved[0]['user_id'], 'bob')
        self.assertEqual(saved[0]['display_name'], 'bob-eip.pdf')
        self.assertEqual(saved[0]['source_file_id'], '')
        self.assertTrue(eip_input.closed)
        self.assertTrue(saved[0]['payload'].closed)
        for source in (alice, bob):
            self.assertEqual(Path(source.path).read_bytes(), self.original)
            self.assertFalse(source.filename.endswith('.docx'))
        first.cleanup()
        self.assertFalse(first.pdf_path.exists())
        self.assertTrue(second.pdf_path.exists())
        self.assertEqual(Path(alice.path).read_bytes(), self.original)

    async def test_warm_office_cache_never_bypasses_upload_owner_acl(self):
        alice = self.upload('alice')
        with patch.object(office_input, '_convert_pdf_bytes', AsyncMock(return_value=self.pdf())) as convert:
            await self.prepare(alice, 'alice')
            reads = self.local.call_count
            with patch.object(self.cache, 'aget_or_compute', wraps=self.cache.aget_or_compute) as lookup:
                with self.assertRaises(M.ProcessingError):
                    await self.prepare(alice, 'bob')
            lookup.assert_not_called()
        convert.assert_awaited_once()
        self.assertEqual(self.local.call_count, reads)

    async def test_cached_conversion_still_enforces_current_page_limit_and_cleans_tempfile(self):
        alice, bob = self.upload('alice'), self.upload('bob')
        with patch.object(office_input, '_convert_pdf_bytes', AsyncMock(return_value=self.pdf(2))) as convert:
            await self.prepare(alice, 'alice')
            self.pipe.valves.max_pdf_pages = 1
            directories = []
            original = tempfile.TemporaryDirectory

            def temporary(*args, **kwargs):
                directory = original(*args, **kwargs)
                directories.append(Path(directory.name))
                return directory

            with patch('examples.official_document_workflow.services.sources.tempfile.TemporaryDirectory', side_effect=temporary):
                with self.assertRaisesRegex(M.ProcessingError, '超過 1 頁'):
                    await self.prepare(bob, 'bob')
        convert.assert_awaited_once()
        self.assertTrue(directories)
        self.assertTrue(all(not path.exists() for path in directories))
        self.assertEqual(Path(bob.path).read_bytes(), self.original)
