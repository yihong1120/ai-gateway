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

from company_agent.config import Root
from company_agent.config import Settings
from company_agent.runtime.context import Context
from company_agent.runtime.models import close_model
from company_agent.runtime.storage import Store
from company_agent.services.office.schemas import UnderstandImage
from company_agent.services.office.vision import understand


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


async def main(owner):
    from company_agent.api.service import service
    from company_agent.runtime.context import verified_user
    from company_agent.tests.postgres_fixture import PostgreSQLTestCase, make_state_directory
    import pymupdf

    user = await verified_user(owner)
    model = service().model_factory(user)
    os.environ['COMPANY_AGENT_TEST_DATABASE_URL'] = os.environ['COMPANY_AGENT_DATABASE_URL']
    fixture = PostgreSQLTestCase()
    fixture.setUp()
    try:
        with tempfile.TemporaryDirectory(prefix='company-vision-smoke-') as temporary:
            root = Path(temporary)
            state = root / 'state'
            make_state_directory(state)
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
                model_name=os.getenv('COMPANY_AGENT_MODEL', 'Kimi-K2.5'),
            )
            store = Store(state)
            job = store.job(owner)
            ctx = Context(owner, job, settings, store, model)
            started = time.monotonic()
            # Synthetic-only diagnostic: report response envelope on failure,
            # never enable this capture in the production tools.
            from unittest.mock import patch
            from company_agent.runtime.models import billed_analysis

            capture = AnalysisCapture(billed_analysis)
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
    finally:
        fixture.doCleanups()
        await close_model(model)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--owner', required=True)
    asyncio.run(main(parser.parse_args().owner))
