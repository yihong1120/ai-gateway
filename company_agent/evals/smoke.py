"""Opt-in synthetic end-to-end demo. Default is offline; --live costs tokens."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from langchain_core.messages import AIMessage

from company_agent.config import Root
from company_agent.config import Settings
from company_agent.runtime.execution import DocumentAgent
from company_agent.runtime.models import close_model
from company_agent.runtime.models import GatewayModel
from company_agent.services.office.documents import extract
from company_agent.tests.postgres_fixture import make_state_directory


class DemoInvocation:
    def __init__(self, model, role):
        self.model, self.role = model, role

    async def ainvoke(self, messages):
        return await self.model.respond(self.role, messages)


async def progress(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


class DemoModel:
    """Explicit offline multi-agent script, not a runtime fallback."""

    def __init__(self):
        self.steps = {'coordinator': 0, 'knowledge': 0, 'document': 0}

    def bind_tools(self, tools, **kwargs):
        names = {tool.name for tool in tools}
        from company_agent.agents.registry import ROLE_TOOLS

        role = next(role for role, allowed in ROLE_TOOLS.items() if names == allowed)
        return DemoInvocation(self, role)

    async def respond(self, role, messages):
        recipes = {
            'coordinator': [
                ('delegate_knowledge', {'task': '搜尋並讀取 demo/sample.txt，提供來源與限制。'}),
                (
                    'delegate_document',
                    {
                        'task': '根據合成資料，製作可編輯工程週報與兩頁簡報，只建立草稿。',
                        'context': 'demo/sample.txt：本週完成基礎施工，下週安排鋼筋檢查；未指定確切期限。',
                    },
                ),
            ],
            'knowledge': [
                ('search_files', {'root': 'demo', 'query': 'sample'}),
                ('read_file', {'root': 'demo', 'path': 'sample.txt'}),
            ],
            'document': [
                (
                    'create_word',
                    {
                        'name': '工程週報.docx',
                        'title': '合成工程週報',
                        'paragraphs': [{'text': '測試工程本週完成基礎施工，下週安排鋼筋檢查。'}],
                    },
                ),
                (
                    'create_slides',
                    {
                        'name': '工程簡報.pptx',
                        'slides': [
                            {
                                'title': '合成工程進度',
                                'bullets': ['本週完成基礎施工', '下週安排鋼筋檢查'],
                            },
                            {
                                'title': '資料限制',
                                'bullets': ['此為合成測試資料', '未指定確切期限，不建立行事曆'],
                            },
                        ],
                    },
                ),
            ],
        }
        recipe = recipes[role]
        if self.steps[role] >= len(recipe):
            return AIMessage(
                content=(
                    '來源已讀取，沒有明確期限。'
                    if role == 'knowledge'
                    else '已完成可編輯 Word 與 PPTX 草稿，沒有寫入 NAS 或 EIP。'
                ),
            )
        name, arguments = recipe[self.steps[role]]
        self.steps[role] += 1
        return AIMessage(
            content='',
            tool_calls=[{'id': f"demo-{role}-{self.steps[role]}", 'name': name, 'args': arguments}],
        )


async def run(output: Path, live=False):
    # Refuse to reuse/overwrite an existing test directory or its results.
    output.mkdir(parents=True, exist_ok=False)
    docs, private, exports = output / 'sources', output / 'state', output / 'downloads'
    docs.mkdir()
    make_state_directory(private)
    exports.mkdir()
    (docs / 'sample.txt').write_text(
        '合成測試工程：本週完成基礎施工，下週安排鋼筋檢查。\n'
        '沒有明確日期或公文期限，不要建立行事曆。\n',
        encoding='utf-8',
    )
    settings = Settings(
        state_dir=str(private.resolve()),
        roots=[
            Root(name='demo', path=str(docs.resolve()), readers=['demo-user'], model_access=True),
        ],
    )
    model = (
        GatewayModel(
            os.environ.get('COMPANY_AGENT_BASE_URL', ''),
            os.environ.get('COMPANY_AGENT_API_KEY', ''),
            os.environ.get('COMPANY_AGENT_MODEL', ''),
        )
        if live
        else DemoModel()
    )
    agent = DocumentAgent(settings, model)

    try:
        result = await agent.run(
            'demo-user',
            '找 demo 裡的 sample 文件並閱讀，依照內容產生 Word 工程週報及兩頁 PPTX。'
            '只建立草稿，不申請存回資料夾；沒有日期就不建立行事曆。',
            progress,
        )
    finally:
        if live:
            await close_model(model)
    verified = []
    for artifact in result['files']:
        name, sha, data = agent.store.read_artifact('demo-user', artifact['id'])
        suffix = Path(name).suffix.lower()
        blocks = extract(data, suffix)
        chars = sum(len(b['text']) for b in blocks)
        if not chars:
            raise RuntimeError('產出沒有可編輯文字')
        with open(exports / name, 'xb') as stream:
            stream.write(data)
        verified.append({'name': name, 'sha256': sha, 'editable_characters': chars})
    report = {
        'mode': 'live' if live else 'offline-scripted-model',
        'result': result,
        'verified': verified,
        'nas_accessed': False,
        'eip_accessed': False,
    }
    (output / 'report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8',
    )
    if result['status'] != 'done' or not {'.docx', '.pptx'} <= {
        Path(f['name']).suffix.lower() for f in verified
    }:
        raise RuntimeError('驗收未通過，詳見 report.json；未自動重試')
    print(
        json.dumps(
            {'verified': verified, 'report': str(output / 'report.json'), 'status': 'passed'},
            ensure_ascii=False,
            indent=2,
        ),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='必須是尚不存在的新資料夾')
    parser.add_argument(
        '--live', action='store_true', help='明確使用 COMPANY_AGENT_* 雲端設定，會產生費用',
    )
    args = parser.parse_args()
    asyncio.run(run(args.output, args.live))


if __name__ == '__main__':
    main()
