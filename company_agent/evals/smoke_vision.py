"""Opt-in ONE billed vision request on synthetic shapes, never company files.

Pass the authorized owner's WebUI UUID explicitly; gateway identity is verified
server-side. Uses a disposable isolated test schema, never deletes user schemas.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import company_agent.evals.sandbox as _sandbox_imports
import company_agent.runtime.models as _models_imports
import examples.identity.adapters.webui as _webui_imports
from company_agent.api.service import ServiceConfiguration
from company_agent.config import Root
from company_agent.config import Settings
from company_agent.evals.runner import evaluation
from company_agent.evals.runner import own_model
from company_agent.evals.runner import own_sandbox
from company_agent.runtime.context import Context
from company_agent.runtime.storage import Store
from company_agent.services.office.schemas import UnderstandImage
from company_agent.services.office.vision import understand
from examples.shared.runtime.modules import ModuleReference
# Synthetic-only diagnostic: report response envelope on failure,
# never enable this capture in the production tools.

_pymupdf = ModuleReference('pymupdf')


def synthetic_pass(words):
    return all(
        any(w in words.lower() for w in choices)
        for choices in [
            ('red', '紅', '红'),
            ('blue', '藍', '蓝'),
            ('square', '方', '四邊形', '四边形'),
            ('circle', '圓', '圆'),
        ]
    )


class AnalysisCapture:
    def __init__(self, analyze):
        self.analyze, self.responses = analyze, []

    async def invoke(self, *args):
        text = await self.analyze(*args)
        self.responses.append(text)
        return text


@evaluation
async def main(owner):
    pymupdf = _pymupdf.load()

    user = await _webui_imports.verified_user(owner)
    model = own_model(ServiceConfiguration.model(user))
    own_sandbox(os.environ['COMPANY_AGENT_DATABASE_URL'])
    with tempfile.TemporaryDirectory(prefix='company-vision-smoke-') as temporary:
        root = Path(temporary)
        state = root / 'state'
        _sandbox_imports.make_state_directory(state)
        inputs = root / 'inputs'
        inputs.mkdir(mode=0o700)
        doc = pymupdf.open()
        p = doc.new_page(width=480, height=240)
        p.draw_rect(pymupdf.Rect(40, 60, 160, 180), color=(1, 0, 0), fill=(1, 0, 0))
        p.draw_circle(pymupdf.Point(340, 120), 60, color=(0, 0, 1), fill=(0, 0, 1))
        (inputs / 'shapes.png').write_bytes(p.get_pixmap().tobytes('png'))
        doc.close()
        settings = Settings(
            state_dir=str(state),
            roots=[Root(name='test', path=str(inputs), readers=[owner], model_access=True)],
            enable_vision=True,
            require_user_billing=True,
            model_name=model.client.model_name,
        )
        store = Store(state)
        job = store.job(owner)
        ctx = Context(owner, job, settings, store, model)
        started = time.monotonic()

        capture = AnalysisCapture(_models_imports.billed_analysis)
        captured = capture.responses

        try:
            with patch(
                'company_agent.services.office.vision.billed_analysis',
                side_effect=capture.invoke,
            ):
                result = await understand(
                    ctx,
                    UnderstandImage(
                        root='test',
                        path='shapes.png',
                        question='圖中有哪些圖形及顏色？只描述直接可見內容。',
                    ),
                )
        except Exception:
            print(
                json.dumps(
                    {'synthetic_response': captured, 'passed': False}, ensure_ascii=False,
                ),
            )
            raise
        with store.db() as db:
            usage = [
                json.loads(r[0]).get('usage', {})
                for r in db.execute(
                    "SELECT result FROM tool_receipts WHERE name='billed_analysis' AND result IS NOT NULL",
                ).fetchall()
            ]
        words = json.dumps(result, ensure_ascii=False).lower()
        passed = synthetic_pass(words)
        print(
            json.dumps(
                {
                    'passed': passed,
                    'model': settings.model_name,
                    'billed_requests': len(usage),
                    'usage': usage,
                    'seconds': round(time.monotonic() - started, 2),
                    'company_documents_sent': 0,
                    'eip_writes': 0,
                    'summary': result['summary'],
                },
                ensure_ascii=False,
            ),
        )
        if not passed:
            raise RuntimeError('Synthetic vision quality check failed')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--owner', required=True)
    asyncio.run(main(parser.parse_args().owner))
