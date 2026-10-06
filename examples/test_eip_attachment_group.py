"""Explicit local sample test: generates private files, NEVER imports calendar.

Uses the three already downloaded attachments from flow 59105; no EIP login
or Detail request. Run only when intentionally testing cloud/GPU processing.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT/'.env.openwebui')
os.environ['FROM_INIT_PY'] = 'true'


async def main():
    from official_document_workflow.application import automation, integration
    from open_webui.models.files import Files
    from open_webui.storage.provider import Storage
    db = automation.store()
    with db.db() as conn:
        owners = conn.execute('SELECT owner FROM accounts WHERE identity=?', ('1709',)).fetchall()
    if len(owners) != 1:
        raise RuntimeError('需要唯一已綁定的測試帳號')
    owner = owners[0]['owner']
    folder = ROOT/'example_files/eip-word-59105-e7cs826l'
    names = ['公文-橋面板裂縫補強.doc', '附件2.pdf', '附件1-經綸EPOXY裂縫灌注(懸臂橋面板).pdf']
    ids = []
    for name in names:
        f = await integration.source_file(owner, (folder/name).read_bytes(), name)
        ids.append(f.id)

    async def emit(event):
        print(event.get('data', {}).get('description', ''), flush=True)
    start = time.monotonic()
    result = await integration.process(owner, ids, '產生 Word、PDF 與行事曆，另於每項期限後七天提醒我追蹤進度。', emit, groups=[ids])
    print(json.dumps(result, ensure_ascii=False), flush=True)
    for output in result['files']:
        item = await Files.get_file_by_id_and_user_id(output['id'], owner)
        if not item:
            raise RuntimeError('輸出不屬於測試帳號')
        path = await asyncio.to_thread(Storage.get_file, item.path)
        if output['name'].endswith('.docx'):
            with zipfile.ZipFile(path) as z:
                tree = ElementTree.fromstring(z.read('word/document.xml'))
                _ = ''.join(tree.itertext())
                runs = tree.findall('.//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t')
                chars = sum(len(t.text or '') for t in runs)
                print('Editable Word verification:', output['id'], 'text characters', chars, flush=True)
                if not chars:
                    raise RuntimeError('Word 沒有可編輯文字')
    print('Seconds:', round(time.monotonic()-start, 1), 'Calendar imported: false', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
