#!/usr/bin/env python3
"""
Test script for word_mcp_smart_pipeline.py
測試 Pipeline 是否能正確操作 /mnt/data 內的檔案
"""
from __future__ import annotations

import asyncio
import json
import sys
from importlib import import_module
from pathlib import Path

# 添加 pipelines 目錄到路徑
sys.path.insert(0, str(Path(__file__).parent.parent / 'pipelines'))
Pipeline = import_module('word_mcp_smart_pipeline').Pipeline


async def test_pipeline():
    """測試 Pipeline 的完整流程"""
    print('=' * 70)
    print('Word MCP Pipeline 測試')
    print('=' * 70)

    # 初始化 Pipeline
    pipeline = Pipeline()

    # 測試 1: 啟動並載入工具
    print('\n[測試 1] 啟動 Pipeline 並載入工具...')
    try:
        await pipeline.on_startup()
        if pipeline.tools_loaded:
            print(f"✅ 成功載入 {len(pipeline.available_tools)} 個工具")
            print(f"   MCP Server: {pipeline.valves.MCP_SERVER_URL}")
        else:
            print('❌ 工具載入失敗')
            return False
    except Exception as e:
        print(f"❌ 啟動失敗: {e}")
        return False

    # 測試 2: 測試 inlet (工具注入)
    print('\n[測試 2] 測試工具注入...')
    test_request = {
        'messages': [
            {'role': 'user', 'content': '請建立一個測試文檔'},
        ],
    }

    try:
        modified_request = await pipeline.inlet(test_request.copy())

        if 'tools' in modified_request:
            print(f"✅ 成功注入 {len(modified_request['tools'])} 個工具")
            print(f"   tool_choice: {modified_request.get('tool_choice')}")

            # 檢查是否注入了 System Prompt
            if modified_request['messages'][0]['role'] == 'system':
                print('✅ 成功注入 System Prompt')
                print(f"   Prompt 長度: {len(modified_request['messages'][0]['content'])} 字元")
            else:
                print('⚠️  未檢測到 System Prompt')
        else:
            print('❌ 工具注入失敗')
            return False
    except Exception as e:
        print(f"❌ inlet 測試失敗: {e}")
        return False

    # 測試 3: 模擬 LLM 回應並測試 outlet (工具執行)
    print('\n[測試 3] 測試工具執行...')

    # 模擬 LLM 的 tool_calls 回應
    simulated_response = {
        'messages': [
            {'role': 'user', 'content': '請建立測試文檔'},
            {
                'role': 'assistant',
                'content': '我將為您建立測試文檔',
                'tool_calls': [
                    {
                        'id': 'call_1',
                        'type': 'function',
                        'function': {
                            'name': 'create_document',
                            'arguments': json.dumps({
                                'file_path': '/mnt/data/pipeline_test.docx',
                                'title': 'Pipeline 測試文檔',
                            }),
                        },
                    },
                    {
                        'id': 'call_2',
                        'type': 'function',
                        'function': {
                            'name': 'add_paragraph',
                            'arguments': json.dumps({
                                'file_path': '/mnt/data/pipeline_test.docx',
                                'text': '這是透過 Word MCP Pipeline 自動生成的測試段落。',
                            }),
                        },
                    },
                ],
            },
        ],
    }

    try:
        result = await pipeline.outlet(simulated_response.copy())

        # 檢查回應
        assistant_msg = result['messages'][-1]
        content = assistant_msg.get('content', '')

        print('✅ outlet 執行完成')
        print('\n回應內容:')
        print('-' * 70)
        print(content)
        print('-' * 70)

        # 檢查是否移除了 tool_calls
        if 'tool_calls' not in assistant_msg:
            print('\n✅ 已正確移除 tool_calls')
        else:
            print('\n⚠️  tool_calls 未被移除')

        # 檢查檔案是否實際創建
        test_file = Path('/mnt/data/pipeline_test.docx')
        if test_file.exists():
            print(f"\n✅ 檔案已成功創建: {test_file}")
            print(f"   檔案大小: {test_file.stat().st_size} bytes")
        else:
            print(f"\n⚠️  檔案未找到: {test_file}")

    except Exception as e:
        print(f"❌ outlet 測試失敗: {e}")
        import traceback
        traceback.print_exc()
        return False

    # 測試 4: 讀取剛建立的文檔
    print('\n[測試 4] 讀取文檔內容...')

    read_response = {
        'messages': [
            {'role': 'user', 'content': '讀取測試文檔'},
            {
                'role': 'assistant',
                'content': '我將讀取文檔',
                'tool_calls': [
                    {
                        'id': 'call_3',
                        'type': 'function',
                        'function': {
                            'name': 'get_document_text',
                            'arguments': json.dumps({
                                'file_path': '/mnt/data/pipeline_test.docx',
                            }),
                        },
                    },
                ],
            },
        ],
    }

    try:
        result = await pipeline.outlet(read_response.copy())
        assistant_msg = result['messages'][-1]
        content = assistant_msg.get('content', '')

        print('✅ 讀取完成')
        print('\n讀取結果:')
        print('-' * 70)
        print(content)
        print('-' * 70)

    except Exception as e:
        print(f"❌ 讀取測試失敗: {e}")
        return False

    # 測試 5: 路徑正規化測試
    print('\n[測試 5] 測試路徑正規化...')

    path_test_cases = [
        ('/mnt/data/test.docx', 'X:\\test.docx'),
        ('test.docx', 'X:\\test.docx'),
        ('./relative/path.docx', 'X:\\relative/path.docx'),
        ('X:\\already_correct.docx', 'X:\\already_correct.docx'),
    ]

    from word_mcp_smart_pipeline import normalize_path

    for input_path, expected in path_test_cases:
        result = normalize_path(
            input_path,
            pipeline.valves.WINDOWS_PATH_PREFIX,
            pipeline.valves.LINUX_SHARE_DIR,
        )
        status = '✅' if result == expected else '❌'
        print(f"  {status} {input_path} -> {result}")
        if result != expected:
            print(f"      預期: {expected}")

    print('\n' + '=' * 70)
    print('測試完成！')
    print('=' * 70)

    # 清理測試檔案
    test_file = Path('/mnt/data/pipeline_test.docx')
    if test_file.exists():
        try:
            test_file.unlink()
            print(f"\n🧹 已清理測試檔案: {test_file}")
        except Exception as e:
            print(f"\n⚠️  清理測試檔案失敗: {e}")

    return True


if __name__ == '__main__':
    success = asyncio.run(test_pipeline())
    sys.exit(0 if success else 1)
