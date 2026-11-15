"""Offline contracts for explicit document acceptance tools."""
from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import pymupdf
import pytest

from examples.official_document_workflow.evals import eip_samples
from examples.official_document_workflow.evals import runner
from examples.official_document_workflow.evals import sample_corpus
from examples.official_document_workflow.evals.local_outputs import LocalOutputs
from examples.shared.runtime.modules import ModuleReference
# Only the test installs the host stubs; shipped tools never import tests.

_docx = ModuleReference('docx')
_fixture_test_eip_calendar_csv = ModuleReference('tests.test_eip_calendar_csv')
_fixture_test_official_document_processor_pipe = ModuleReference('tests.test_official_document_processor_pipe')


@pytest.mark.parametrize(
    'module',
    [
        'examples.official_document_workflow.cli.prepare_calendar_csv',
        'examples.official_document_workflow.evals.sample_corpus',
        'examples.official_document_workflow.evals.eip_samples',
    ],
)
def test_help_without_host_or_database(module):
    result = subprocess.run(
        [sys.executable, '-m', module, '--help'],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
        env={**os.environ, 'DATABASE_URL': 'invalid'},
    )
    assert result.returncode == 0, result.stderr


def test_import_has_no_host_or_environment_side_effects():
    code = """import os, sys
before = dict(os.environ)
from examples.official_document_workflow.evals import sample_corpus, eip_samples
assert dict(os.environ) == before
assert not any(n.startswith(('open_webui', 'tests.')) for n in sys.modules)
"""
    subprocess.run([sys.executable, '-c', code], check=True, timeout=15)


def test_inventory_groups_and_rejects_escape(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'a.pdf').write_bytes(b'example')
    (source / 'b.docx').write_bytes(b'example')
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(
        json.dumps([{'name': '../display-only', 'files': ['a.pdf', 'b.docx']}]),
    )
    available = sample_corpus.cases(source, manifest)
    assert len(available) == 1 and len(available[0][1]) == 2
    assert sample_corpus.inventory(available)[0]['files'][0][
        'sha256'
    ] == sample_corpus.sha(source / 'a.pdf')
    (tmp_path / 'outside.pdf').write_bytes(b'private')
    manifest.write_text(json.dumps([{'name': 'escape', 'files': ['../outside.pdf']}]))
    with pytest.raises(ValueError, match='inside input root'):
        sample_corpus.cases(source, manifest)


def test_default_inventory_does_not_execute_or_create_output(tmp_path):
    (tmp_path / 'a.pdf').write_bytes(b'not even a PDF')
    output = tmp_path / 'output'
    result = subprocess.run(
        [
            sys.executable,
            '-m',
            'examples.official_document_workflow.evals.sample_corpus',
            '--input-root',
            str(tmp_path),
            '--output',
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
        env={**os.environ, 'DATABASE_URL': 'invalid'},
    )
    assert len(json.loads(result.stdout)) == 1
    assert not output.exists()


@pytest.mark.parametrize('selection', ['', ' ', '1,,2', 'not-an-index', '0', '3'])
def test_invalid_explicit_case_selection_stops_before_hashing_or_execution(tmp_path, monkeypatch, selection):
    (tmp_path / 'a.pdf').write_bytes(b'synthetic')
    output = tmp_path / 'new-output'
    monkeypatch.setattr(
        sys, 'argv', [
            'sample_corpus', '--input-root', str(tmp_path),
            '--cases', selection, '--execute', '--output', str(output),
        ],
    )
    with patch.object(sample_corpus, 'sha') as digest, patch.object(sample_corpus, 'execute') as execute, \
            pytest.raises(SystemExit) as error:
        sample_corpus.main()
    assert error.value.code == 2
    digest.assert_not_called()
    execute.assert_not_called()
    assert not output.exists()


def test_subset_inventory_reads_only_selected_sources(tmp_path, monkeypatch, capsys):
    first, selected = tmp_path / 'a.pdf', tmp_path / 'b.pdf'
    first.write_bytes(b'first')
    selected.write_bytes(b'selected')
    monkeypatch.setattr(sys, 'argv', ['sample_corpus', '--input-root', str(tmp_path), '--cases', '2'])
    with patch.object(sample_corpus, 'sha', wraps=sample_corpus.sha) as digest:
        assert sample_corpus.main() == 0
    digest.assert_called_once_with(selected)
    inventory = json.loads(capsys.readouterr().out)
    assert [entry['index'] for entry in inventory] == [2]
    assert inventory[0]['name'] == 'b'


@pytest.mark.parametrize(
    'entry', [
        None, 7, 'wrong-shape', {}, {'name': 'missing-files'},
        {'name': ' ', 'files': ['a.pdf']}, {'name': 'bad-path', 'files': [7]},
        {'name': 'unknown-field', 'files': ['a.pdf'], 'file': 'typo'},
    ],
)
def test_manifest_shape_has_one_explicit_validation_boundary(tmp_path, entry):
    (tmp_path / 'a.pdf').write_bytes(b'synthetic')
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps([entry]))
    with pytest.raises(ValueError, match='Every case needs'):
        sample_corpus.cases(tmp_path, manifest)


def test_local_sink_formats_and_no_overwrite(tmp_path):
    sink = LocalOutputs(tmp_path)
    with pymupdf.open() as document:
        document.new_page()
        payload = document.tobytes()
    kwargs = {
        'display_name': 'result.pdf',
        'payload': io.BytesIO(payload),
        'kind': 'document_pdf',
        'extra_metadata': {'official_document_output_pages': [1]},
    }
    result = asyncio.run(sink.store(**kwargs))
    assert Path(result.path).read_bytes() == payload
    assert sink.files[0]['page_count_matches_metadata']
    with pytest.raises(FileExistsError):
        asyncio.run(sink.store(**kwargs))
    with pytest.raises(ValueError, match='Unsafe'):
        asyncio.run(sink.store(**{**kwargs, 'display_name': '../outside.pdf'}))
    Document = _docx.attribute('Document')

    doc = Document()
    doc.add_paragraph('可編輯文字')
    stream = io.BytesIO()
    doc.save(stream)
    asyncio.run(
        sink.store(display_name='result.docx', payload=stream, kind='document_docx'),
    )
    assert sink.files[1]['editable_characters'] == 5
    assert sink.files[1]['zip_ok']


@pytest.mark.parametrize('identity', ['expected', 'wrong'])
def test_eip_identity_and_stream_cleanup(tmp_path, identity):
    stream = io.BytesIO(b'validated by adapter')
    adapter = SimpleNamespace(
        user_id=identity,
        login=AsyncMock(),
        close=AsyncMock(),
        documents=AsyncMock(return_value=[{'id': '42'}]),
        attachments=AsyncMock(return_value=[{'name': 'a.pdf', 'url': '/fo/file'}]),
        download=AsyncMock(return_value=stream),
    )
    args = SimpleNamespace(
        output=tmp_path / 'new',
        document_id='42',
        folder='sign',
        start='2026-01-01',
        end='2026-01-02',
    )
    with patch.dict(
        os.environ,
        EIP_SAMPLE_IDENTITY='expected',
        EIP_SAMPLE_ACCOUNT='account',
        EIP_SAMPLE_PASSWORD='secret',
    ):
        if identity == 'wrong':
            with pytest.raises(ValueError, match='identity mismatch'):
                asyncio.run(eip_samples.download(args, adapter))
            adapter.download.assert_not_called()
            assert not args.output.exists()
        else:
            asyncio.run(eip_samples.download(args, adapter))
            assert stream.closed
            assert json.loads((args.output / 'manifest.json').read_text())[0][
                'files'
            ] == ['a.pdf']
    adapter.close.assert_awaited_once()


def test_csv_cli_preserves_existing_output(tmp_path):
    fixture = _fixture_test_eip_calendar_csv.attribute('fixture')

    source, target = tmp_path / 'input.csv', tmp_path / 'output.csv'
    source.write_bytes(fixture())
    command = [
        sys.executable,
        '-m',
        'examples.official_document_workflow.cli.prepare_calendar_csv',
        str(source),
        '--output',
        str(target),
    ]
    subprocess.run(command, check=True, capture_output=True, timeout=15)
    before = target.read_bytes()
    assert (
        subprocess.run(command, check=False, capture_output=True, timeout=15).returncode
        != 0
    )
    assert target.read_bytes() == before


@pytest.mark.parametrize('partial', [False, True])
def test_runner_uses_local_exports_and_records_partial_success(tmp_path, partial):
    _fixture_test_official_document_processor_pipe.attribute('PIPE_MODULE')

    source = tmp_path / 'source.pdf'
    with pymupdf.open() as doc:
        doc.new_page()
        source.write_bytes(doc.tobytes())
    output = tmp_path / 'reports'
    output.mkdir()

    class Batch:
        def __init__(self, processor, *_args):
            self.processor = processor
            self.payload = {'diagnostic': 'local'}
            self.failed_groups = []
            self.pending_groups = int(partial)
            self.calendar_pending = []
            self.unread = {}
            self.export_failed = self.csv_failed = False

        async def run(self):
            await self.processor.exports.store_output(
                payload=source.read_bytes(),
                display_name='output.pdf',
                kind='document_pdf',
                extra_metadata={'official_document_output_pages': [1]},
            )
            return 'partial' if partial else 'complete'

    with (
        patch.object(runner, 'UploadBatch', Batch),
        patch.object(runner, 'unique_uploads', return_value=((), {})),
    ):
        result = asyncio.run(runner.evaluate(1, 'case-1', [source], output, {}))
    assert result['completed'] is not partial
    assert result['output_checks_passed']
    assert result['source_files_unchanged']
    assert result['calendar_written'] is False
    assert result['pending_groups'] == int(partial)


def test_failed_download_closes_stream_and_never_publishes_manifest(tmp_path):
    stream = io.BytesIO(b'first')
    adapter = SimpleNamespace(
        user_id='expected',
        login=AsyncMock(),
        close=AsyncMock(),
        documents=AsyncMock(return_value=[{'id': '42'}]),
        attachments=AsyncMock(
            return_value=[{'name': n, 'url': '/fo/file'} for n in ['a.pdf', 'b.pdf']],
        ),
        download=AsyncMock(side_effect=[stream, OSError('temporary failure')]),
    )
    args = SimpleNamespace(
        output=tmp_path / 'new',
        document_id='42',
        folder='sign',
        start='2026-01-01',
        end='2026-01-02',
    )
    with (
        patch.dict(
            os.environ,
            EIP_SAMPLE_IDENTITY='expected',
            EIP_SAMPLE_ACCOUNT='account',
            EIP_SAMPLE_PASSWORD='secret',
        ),
        pytest.raises(OSError, match='temporary failure'),
    ):
        asyncio.run(eip_samples.download(args, adapter))
    assert stream.closed
    assert not (args.output / 'manifest.json').exists()
    adapter.close.assert_awaited_once()


def test_provider_failure_stops_remaining_cases(tmp_path):
    _fixture_test_official_document_processor_pipe.attribute('PIPE_MODULE')

    first, second = tmp_path / 'a.pdf', tmp_path / 'b.pdf'
    first.write_bytes(b'a')
    second.write_bytes(b'b')
    args = SimpleNamespace(output=tmp_path / 'new', settings=None)
    report = {
        'completed': False,
        'output_checks_passed': False,
        'source_files_unchanged': True,
        'model_calls': [{'state': 'failed', 'type': 'CloudRateLimited'}],
    }
    evaluate = AsyncMock(return_value=report)
    with (
        patch.object(sample_corpus, 'settings', return_value={}),
        patch.object(runner, 'evaluate', evaluate),
    ):
        status = asyncio.run(
            sample_corpus.execute(
                args, [('first', [first]), ('second', [second])], {1, 2},
            ),
        )
    assert status == 1
    evaluate.assert_awaited_once()
    assert len(json.loads((args.output / 'execution.json').read_text())) == 1
