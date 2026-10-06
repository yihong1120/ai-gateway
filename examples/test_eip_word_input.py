"""Explicit read-only EIP sample test. Generates private files; NEVER imports calendar."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT/'.env.openwebui')
os.environ['FROM_INIT_PY'] = 'true'


async def main():
    from official_document_workflow.application import automation, integration
    from official_document_workflow.infrastructure.eip.netask import Netask
    from official_document_workflow.infrastructure.office.office_input import validate
    store = automation.store()
    log = logging.getLogger('official_document_eip_pipe')
    log.setLevel(logging.INFO)
    log.addHandler(logging.StreamHandler())
    with store.db() as db:
        owners = db.execute('SELECT owner FROM accounts WHERE identity=?', ('1709',)).fetchall()
    if len(owners) != 1:
        raise RuntimeError('需要唯一且已綁定的測試帳號')
    owner = owners[0]['owner']
    credentials = store.account(owner, secrets=True)
    adapter = Netask()
    try:
        await adapter.login(credentials['account'], credentials['password'])
        credentials.pop('password', None)
        if adapter.user_id != '1709':
            raise RuntimeError('EIP 身分不符')
        docs = await adapter.documents('sign', '2026-09-01', '2026-09-04')
        if not any(d['id'] == '59105' for d in docs):
            raise RuntimeError('指定文件不在本人簽核清單')
        attachments = await adapter.attachments('59105', 'sign')
        target = next(a for a in attachments if a['name'] == '公文-橋面板裂縫補強.doc')
        output = Path(tempfile.mkdtemp(prefix='eip-word-59105-', dir=ROOT/'example_files'))
        for item in attachments:
            payload = await adapter.download(item['url'], item['name'])
            validate(payload, item['name'])
            (output/Path(item['name']).name).write_bytes(payload)
            print('Validated attachment:', item['name'], len(payload), flush=True)
        original = (output/target['name']).read_bytes()
    finally:
        await adapter.close()
    print('Test directory:', output, flush=True)
    start = time.monotonic()
    source = await integration.source_file(owner, original, target['name'])
    print('Word normalized to PDF:', source.id, flush=True)
    result = await integration.process(owner, [source.id], '', None, groups=[[source.id]])
    print(
        json.dumps(
            {
                'complete': result['complete'], 'files': result['files'],
                'has_deadlines': result['has_deadlines'], 'message': result['message'],
                'seconds': round(time.monotonic()-start, 1), 'calendar_imported': False,
            }, ensure_ascii=False,
        ), flush=True,
    )


if __name__ == '__main__':
    asyncio.run(main())
