"""
title: 公司工作助理
author: Changdar
version: 1.6.0
required_open_webui_version: 0.11.1
description: LangGraph 公司工作助理；文件、圖片語意、來源 Excel 修改、EIP 附件處理與本人確認行事曆、CAD 結構，扣本人既有每日額度。
"""
from __future__ import annotations


class Pipe:
    def __init__(self):
        self.name = '公司工作助理（文件／Excel／CAD）'

    async def pipe(
        self, body: dict, __user__: dict = None, __metadata__: dict = None,
        __event_emitter__=None, __task__=None,
    ):
        from company_agent.api.pipe import run_pipe
        return await run_pipe(body, __user__, __metadata__, __event_emitter__, __task__)
