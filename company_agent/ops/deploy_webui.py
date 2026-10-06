"""One explicit, backed-up deployment of the WebUI model, Pipe and navigation."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path


ENTRIES = {
    'company-account-connections': ('帳號與連線', '**[帳號與連線：管理登入帳號、使用者、公司權限與 EIP／NAS 綁定](/connections)**　管理員可由此進入完整使用者管理。'),
    'ai-gateway-eip-workspace': ('EIP 公文工作台', '**[EIP 公文工作台：處理狀態與下載](/eip)**　公文排程、PDF／Word／CSV 與私人行事曆。'),
    'company-agent-workspace': ('公司工作助理', '**[公司助理工作台：查看進度與下載成果](/agents)**　模型選單選擇「公司工作助理」，依本人每日額度計費。'),
}


def updated_banners(banners):
    owned = {b.get('id'): b for b in banners if b.get('id') in ENTRIES}
    entries = [
        {
            **owned.get(key, {}), 'id': key, 'type': 'info', 'title': title, 'content': content,
            'url': '', 'dismissible': False, 'timestamp': int(time.time()),
        } for key, (title, content) in ENTRIES.items()
    ]
    return [b for b in banners if b.get('id') not in ENTRIES]+entries


def function_source():
    """Read the repository-owned WebUI entry before any deployment writes."""
    return (Path(__file__).resolve().parents[2] / 'openwebui/functions/company_agent_pipe.py').read_text(encoding='utf-8')


async def deploy():
    content = function_source()
    # Match the installed-package launcher before importing Open WebUI paths.
    os.environ['FROM_INIT_PY'] = 'true'
    from open_webui.models.functions import Functions, FunctionForm, FunctionMeta
    from open_webui.models.models import Models, ModelForm, ModelMeta, ModelParams
    from open_webui.models.users import Users
    from open_webui.models.config import Config
    from open_webui.env import DATA_DIR
    creator = await Users.get_first_user()
    if not creator or creator.role != 'admin':
        raise RuntimeError('需要既有管理員來部署入口。')
    key = 'company_work_assistant'
    old = await Functions.get_function_by_id(key)
    old_model = await Models.get_model_by_id(key)
    banners = await Config.get('ui.banners') or []
    backup = Path(DATA_DIR)/'company_agent_deploy_backups'
    backup.mkdir(mode=0o700, exist_ok=True)
    with open(backup/(str(time.time_ns())+'.json'), 'x') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(
            {
                'function': old.model_dump() if old else None, 'model': old_model.model_dump()
                if old_model else None, 'ui.banners': banners,
            }, stream, ensure_ascii=False,
        )
    form = FunctionForm(
        id=key, name='公司工作助理（文件／Excel／CAD）', content=content,
        meta=FunctionMeta(description='所有已啟用使用者可使用；扣本人既有每日額度。'),
    )
    if old:
        item = await Functions.update_function_by_id(key, {**form.model_dump(exclude={'id'}), 'is_active': False})
    else:
        item = await Functions.insert_new_function(creator.id, 'pipe', form)
    if not item:
        raise RuntimeError('無法保存公司助理 Function。')
    model_form = ModelForm(
        id=key, name=form.name, params=ModelParams(),
        meta=ModelMeta(
            description='文件、圖片語意、Excel 原簿修改／計算報表、EIP 公文與真正行事曆查詢、準備行程並由本人確認寫入、Tavily、CAD。成果至 /agents；模型扣本人額度。EIP 不簽核；CAD 不重存 DWG。',
            capabilities={'file_context': False, 'vision': True, 'web_search': False, 'code_interpreter': False},
            suggestion_prompts=[
                {'content': '請製作一份工程週報 Word 草稿，缺少資料請先問我。'},
                {'content': '請分析我附上的 Excel，列出數值統計並產生工作簿。'},
                {'content': '請讀取我附上的 DWG，列出圖層、相關文字與不支援物件，不要修改原圖。'},
                {'content': '幫我查詢這個月的 EIP 行事曆，列出會議與行程。'},
            ],
        ),
        access_grants=[{'principal_type': 'user', 'principal_id': '*', 'permission': 'read'}],
    )
    if old_model:
        result = await Models.update_model_by_id(key, model_form)
    else:
        result = await Models.insert_new_model(model_form, creator.id)
    if not result:
        raise RuntimeError('無法保存公司助理模型入口。')
    await Config.upsert({'ui.banners': updated_banners(banners)})
    item = await Functions.update_function_by_id(key, {'is_active': True})
    if not item:
        raise RuntimeError('無法啟用公司助理入口。')
    print(
        json.dumps(
            {
                'function_id': key, 'active': item.is_active, 'all_authenticated_users': True,
                'updated_entry_ids': list(ENTRIES), 'backup': str(backup),
            }, ensure_ascii=False,
        ),
    )


if __name__ == '__main__':
    asyncio.run(deploy())
