"""Validate document uploads and normalize Office files in a networkless container."""
from __future__ import annotations

import asyncio
import functools
import io
import os
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from contextlib import AsyncExitStack
from contextlib import ExitStack
from pathlib import Path
from typing import BinaryIO

OFFICE_CONTENT_TYPES = {
    'application/msword': '.doc',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
    'application/vnd.oasis.opendocument.text': '.odt',
}
OFFICE_SUFFIXES = frozenset(OFFICE_CONTENT_TYPES.values())
SUFFIXES = {'.pdf', *OFFICE_SUFFIXES}
OFFICE_IMAGE = 'ai-gateway/eip-office:1'


def kill_running(process):
    if process.returncode is None:
        process.kill()


def validate(stream: BinaryIO, name: str):
    suffix = Path(name).suffix.lower()
    stream.seek(0)
    prefix = stream.read(16)
    stream.seek(0)
    if suffix not in SUFFIXES or not prefix:
        raise ValueError('附件只接受有效的 PDF、ODT、DOC、DOCX。')
    if suffix == '.pdf':
        valid = prefix.startswith(b'%PDF-')
    elif suffix == '.doc':
        # Some Word 97-compatible attachments are RTF saved with a .doc name.
        valid = prefix.startswith((b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1', b'{\\rtf'))
    else:
        valid = False
        with ExitStack() as cleanup, zipfile.ZipFile(stream) as archive:
            cleanup.callback(stream.seek, 0)
            entries = archive.infolist()
            names = {entry.filename for entry in entries}
            if suffix == '.odt':
                valid = ({'mimetype', 'content.xml', 'META-INF/manifest.xml'} <= names
                         and archive.getinfo('mimetype').file_size < 100
                         and archive.read('mimetype') == b'application/vnd.oasis.opendocument.text'
                         and not any(n.lower().startswith(('basic/', 'scripts/')) for n in names))
            else:
                valid = {'[Content_Types].xml', 'word/document.xml'} <= names
            valid = (
                valid and not any(e.flag_bits & 1 for e in entries)
                and not any(n.lower().endswith('vbaproject.bin') for n in names)
            )
    if not valid:
        raise ValueError('附件內容與 PDF／Word／ODT 格式不符、已加密或包含不支援的巨集，已停止處理。')
    stream.seek(0)
    return suffix


async def to_pdf(source: BinaryIO, name: str):
    suffix = validate(source, name)
    if suffix == '.pdf':
        return source, Path(name).name
    async with AsyncExitStack() as cleanup:
        directory = cleanup.enter_context(tempfile.TemporaryDirectory(prefix='eip-office-'))
        root = Path(directory)
        source.seek(0)
        with (root / ('source'+suffix)).open('wb') as target:
            await asyncio.to_thread(functools.partial(shutil.copyfileobj, source, target, length=1024*1024))
        profile = root/'profile'/'user'
        profile.mkdir(parents=True)
        (profile/'registrymodifications.xcu').write_text(
            '''<?xml version="1.0"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting">
<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>
<prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop>
<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>
</item></oor:items>''', encoding='utf-8',
        )
        container = 'eip-office-'+uuid.uuid4().hex
        command = [
            'docker', 'run', '--rm', '--pull=never', '--name', container,
            '--network=none', '--read-only', '--cap-drop=ALL',
            '--security-opt=no-new-privileges', '--pids-limit=128',
            '--memory=1g', '--cpus=2',
            '--user', f'{os.getuid()}:{os.getgid()}',
            '--tmpfs', '/tmp:rw,noexec,nosuid,size=128m',
            '--mount', f'type=bind,src={root},dst=/work', OFFICE_IMAGE,
            '--convert-to', 'pdf:writer_pdf_Export', '--outdir', '/work', '/work/source'+suffix,
        ]
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        cleanup.push_async_callback(process.wait)
        cleanup.callback(kill_running, process)
        cleanup.push_async_callback(
            asyncio.to_thread, subprocess.run,
            ['docker', 'rm', '-f', container],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=15, check=False,
        )
        await asyncio.wait_for(process.wait(), timeout=120)
        output = root/'source.pdf'
        if process.returncode != 0 or not output.is_file():
            raise ValueError('Word／ODT 轉 PDF 失敗，請確認檔案未加密及隔離轉換服務可用。')
        result = io.BytesIO(await asyncio.to_thread(output.read_bytes))
        validate(result, 'source.pdf')
        return result, Path(name).stem+'.pdf'
