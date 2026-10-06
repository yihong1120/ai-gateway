#!/usr/bin/env python3
"""
真實使用場景測試：創建一個笑話文檔並保留檔案
"""
from __future__ import annotations

import asyncio
import json
import sys
from importlib import import_module
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / 'pipelines'))
Pipeline = import_module('word_mcp_smart_pipeline').Pipeline


async def test_real_usage():
    """模擬真實使用：用戶要求創建笑話文檔"""
    print('=' * 70)
    print('真實使用場景測試：創建笑話文檔')
    print('=' * 70)

    pipeline = Pipeline()
    await pipeline.on_startup()

    if not pipeline.tools_loaded:
        print('❌ 工具載入失敗')
        return False

    print(f"\n✅ 已載入 {len(pipeline.available_tools)} 個工具")

    # 模擬 LLM 的完整回應（創建文檔 + 添加笑話）
    simulated_response = {
        'messages': [
            {'role': 'user', 'content': '幫我想五個笑話並寫入word檔案'},
            {
                'role': 'assistant',
                'content': '好的，我來為您創建一個包含五個笑話的Word文檔。',
                'tool_calls': [
                    {
                        'id': 'call_1',
                        'type': 'function',
                        'function': {
                            'name': 'create_document',
                            'arguments': json.dumps({
                                'file_path': 'jokes_collection.docx',
                                'title': '五個笑話集錦',
                            }),
                        },
                    },
                    {
                        'id': 'call_2',
                        'type': 'function',
                        'function': {
                            'name': 'add_heading',
                            'arguments': json.dumps({
                                'file_path': 'jokes_collection.docx',
                                'text': '笑話精選',
                                'level': 1,
                            }),
                        },
                    },
                    {
                        'id': 'call_3',
                        'type': 'function',
                        'function': {
                            'name': 'add_paragraph',
                            'arguments': json.dumps({
                                'file_path': 'jokes_collection.docx',
                                'text': '1. 烏龜與蝸牛\n\n烏龜受傷了，讓蝸牛去買藥。過了兩個小時，蝸牛還沒回來。烏龜急了罵道：「你再不回來我就要死了！」這時門外傳來蝸牛的聲音：「你再罵？再罵我就不去了！」',
                            }),
                        },
                    },
                    {
                        'id': 'call_4',
                        'type': 'function',
                        'function': {
                            'name': 'add_paragraph',
                            'arguments': json.dumps({
                                'file_path': 'jokes_collection.docx',
                                'text': '2. 請假理由\n\n員工：「老闆，我明天想請假。」\n老闆：「為什麼？」\n員工：「因為明天我可能會生病。」',
                            }),
                        },
                    },
                    {
                        'id': 'call_5',
                        'type': 'function',
                        'function': {
                            'name': 'add_paragraph',
                            'arguments': json.dumps({
                                'file_path': 'jokes_collection.docx',
                                'text': '3. 數學老師的煩惱\n\n數學老師問小明：「如果你有100元，跟爸爸借了50元，再跟媽媽借了50元，那你現在有多少錢？」\n小明：「0元。」\n老師：「你根本不懂數學！」\n小明：「老師，是你根本不懂我爸媽！」',
                            }),
                        },
                    },
                    {
                        'id': 'call_6',
                        'type': 'function',
                        'function': {
                            'name': 'add_paragraph',
                            'arguments': json.dumps({
                                'file_path': 'jokes_collection.docx',
                                'text': '4. 減肥的決心\n\nA：「我決定了，我要開始減肥！從明天開始，不吃晚餐！」\nB：「那今天的晚餐呢？」\nA：「今天當然要吃頓好的，來慶祝我明天開始減肥啊！」',
                            }),
                        },
                    },
                    {
                        'id': 'call_7',
                        'type': 'function',
                        'function': {
                            'name': 'add_paragraph',
                            'arguments': json.dumps({
                                'file_path': 'jokes_collection.docx',
                                'text': '5. 面試\n\n面試官：「你的缺點是什麼？」\n應徵者：「誠實。」\n面試官：「我不覺得誠實是個缺點。」\n應徵者：「我根本不在乎你怎麼想。」',
                            }),
                        },
                    },
                ],
            },
        ],
    }

    print('\n執行工具調用...')
    result = await pipeline.outlet(simulated_response.copy())

    # 顯示結果
    assistant_msg = result['messages'][-1]
    content = assistant_msg.get('content', '')

    print('\n' + '=' * 70)
    print('AI 回應：')
    print('=' * 70)
    print(content)
    print('=' * 70)

    # 檢查檔案
    print('\n檢查檔案系統...')
    file_path = Path('/mnt/data/jokes_collection.docx')
    if file_path.exists():
        print(f"✅ 檔案已創建: {file_path}")
        print(f"   檔案大小: {file_path.stat().st_size:,} bytes")
        print(f"   絕對路徑: {file_path.absolute()}")
    else:
        print(f"❌ 檔案未找到: {file_path}")
        print('\n嘗試搜尋...')
        for p in Path('/mnt/data').glob('*.docx'):
            print(f"   找到: {p}")

    return True


if __name__ == '__main__':
    success = asyncio.run(test_real_usage())
    sys.exit(0 if success else 1)
