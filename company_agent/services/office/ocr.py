"""Local-only PaddleOCR, one page per call. No cloud image transmission."""
from __future__ import annotations

import base64
import json
import os
from urllib.parse import urlsplit

import httpx

from company_agent.services.office.sandbox import office_worker

NOTICE = 'OCR 可能誤字或漏字；僅辨識文字／區塊位置，不代表圖片內容、施工尺寸或整份文件已完整驗證。'


def endpoint():
    value = os.getenv('COMPANY_AGENT_OCR_URL', 'http://127.0.0.1:8113')
    p = urlsplit(value)
    if (
        p.scheme != 'http'
        or p.hostname != '127.0.0.1'
        or not p.port
        or p.path not in ('', '/')
        or p.username
        or p.password
        or p.query
        or p.fragment
    ):
        raise ValueError()
    return f"http://127.0.0.1:{p.port}/layout-parsing"


def ocr_page(data, suffix, page):
    info, png = office_worker('ocr_page', data=data, suffix=suffix, page=page)
    payload = {
        'file': base64.b64encode(png).decode(),
        'fileType': 1,
        'useLayoutDetection': True,
        'restructurePages': False,
        'visualize': False,
        'maxNewTokens': 4096,
    }
    with httpx.Client(
        timeout=httpx.Timeout(120, connect=5), follow_redirects=False, trust_env=False,
    ) as client:
        with client.stream('POST', endpoint(), json=payload) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 8000000:
                    raise ValueError('OCR 回應超過上限。')
    result = json.loads(body)
    if result.get('errorCode') != 0:
        raise ValueError()
    pages = result['result']['layoutParsingResults']
    if len(pages) != 1:
        raise ValueError()
    raw = pages[0]['prunedResult']['parsing_res_list']
    blocks = []
    for index, block in enumerate(raw):
        value = block.get('block_content')
        if not isinstance(value, str):
            raise ValueError()
        for offset in range(0, len(value), 5000):
            blocks.append(
                {
                    'location': f"page:{page}/block:{index}/offset:{offset}",
                    'text': value[offset: offset + 5000],
                    'bbox': block.get('block_bbox'),
                    'label': block.get('block_label'),
                },
            )
    if not blocks or len(json.dumps(blocks, ensure_ascii=False)) > 1000000:
        raise ValueError()
    return {**info, 'blocks': blocks, 'notice': NOTICE}
