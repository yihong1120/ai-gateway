"""
title: 我的今日額度
author: Visionnaire
version: 1.0.0
required_open_webui_version: 0.11.1
description: Shows only the authenticated user's current-day LiteLLM usage and remaining daily budget.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from datetime import datetime
from datetime import time
from datetime import timedelta
from datetime import timezone
from decimal import Decimal
from decimal import InvalidOperation
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request
from urllib.request import urlopen
from zoneinfo import ZoneInfo


class Pipe:
    """A no-model, server-side view of the signed-in user's own AI usage.

    The browser never supplies the identity or the LiteLLM reader credential.
    The function obtains the verified Keycloak `sub` from Open WebUI's trusted
    `__user__` object and sends it as an exact LiteLLM spend-log filter.
    """

    DAILY_BUDGET_USD = Decimal('4.00')
    TIMEZONE = ZoneInfo('Asia/Taipei')
    PAGE_SIZE = 1000
    MAX_PAGES = 10
    LOCAL_MODEL_NAMES = {'qwen3.5-35b-a3b', 'gpt-oss-20b'}

    def __init__(self) -> None:
        self.name = '我的今日額度（不使用模型）'

    @staticmethod
    def _subject(user: dict[str, Any] | None) -> str | None:
        oauth = (user or {}).get('oauth')
        identity = oauth.get('oidc') if isinstance(oauth, dict) else None
        subject = identity.get('sub') if isinstance(identity, dict) else None
        return subject.strip() if isinstance(subject, str) and subject.strip() else None

    @staticmethod
    def _decimal(value: Any) -> Decimal:
        try:
            return Decimal(str(value or 0))
        except (InvalidOperation, ValueError):
            return Decimal('0')

    @classmethod
    def _is_local(cls, row: dict[str, Any]) -> bool:
        model = str(row.get('model_group') or row.get('model') or '')
        provider = str(row.get('custom_llm_provider') or '').lower()
        api_base = str(row.get('api_base') or '').lower()
        return (
            model in cls.LOCAL_MODEL_NAMES
            or provider.startswith('ollama')
            or api_base.startswith(('http://127.0.0.1', 'http://localhost', 'http://[::1]'))
        )

    @staticmethod
    def _get_json(url: str, api_key: str) -> dict[str, Any]:
        request = Request(
            url,
            headers={
                'Authorization': f"Bearer {api_key}",
                'Accept': 'application/json',
            },
            method='GET',
        )
        with urlopen(request, timeout=15) as response:  # nosec B310: fixed loopback URL
            return json.loads(response.read().decode('utf-8'))

    async def _fetch_today(self, subject: str, api_key: str, now: datetime) -> tuple[list[dict[str, Any]], bool]:
        start = datetime.combine(now.date(), time.min, tzinfo=self.TIMEZONE)
        end = datetime.combine(
            now.date() + timedelta(days=1),
            time.min, tzinfo=self.TIMEZONE,
        ) - timedelta(seconds=1)
        base_url = 'http://127.0.0.1:4000/spend/logs/v2'
        rows: list[dict[str, Any]] = []
        truncated = False

        for page in range(1, self.MAX_PAGES + 1):
            params = {
                'start_date': start.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                'end_date': end.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                'end_user': subject,
                'page': page,
                'page_size': self.PAGE_SIZE,
                'sort_by': 'startTime',
                'sort_order': 'asc',
            }
            payload = await asyncio.to_thread(self._get_json, f"{base_url}?{urlencode(params)}", api_key)
            page_rows = payload.get('data') or []
            if not isinstance(page_rows, list):
                raise RuntimeError('LiteLLM returned an unexpected response')
            rows.extend(row for row in page_rows if isinstance(row, dict))

            total_pages = int(payload.get('total_pages') or 1)
            if page >= total_pages:
                break
            if page == self.MAX_PAGES:
                truncated = True

        return rows, truncated

    async def pipe(self, body: dict[str, Any], __user__: dict[str, Any] | None = None) -> str:
        """Return private daily usage without invoking an LLM or exposing logs."""
        del body  # Selecting this Pipe is sufficient; no prompt content is inspected.
        subject = self._subject(__user__)
        if subject is None:
            return '無法確認你的 Keycloak 身分，因此無法安全讀取個人額度。請登出後以 Keycloak 重新登入。'

        reader_key = os.getenv('LITELLM_USAGE_READER_KEY')
        if not reader_key:
            return '個人額度查詢尚未完成伺服器端設定；請聯絡管理員。'

        now = datetime.now(self.TIMEZONE)
        try:
            rows, truncated = await self._fetch_today(subject, reader_key, now)
        except Exception:
            return '目前無法讀取個人額度，請稍後再試；若持續發生請聯絡管理員。'

        total_spend = sum(
            (
                self._decimal(row.get('spend'))
                for row in rows
            ), Decimal('0'),
        )
        cloud_spend = Decimal('0')
        cloud_tokens = 0
        local_calls = 0
        local_tokens = 0
        by_model: dict[str, dict[str, Any]] = defaultdict(
            lambda: {'calls': 0, 'tokens': 0, 'spend': Decimal('0'), 'local': True},
        )

        for row in rows:
            model = str(row.get('model_group') or row.get('model') or '未知模型')
            local = self._is_local(row)
            tokens = int(row.get('total_tokens') or 0)
            spend = self._decimal(row.get('spend'))
            entry = by_model[model]
            entry['calls'] += 1
            entry['tokens'] += tokens
            entry['spend'] += spend
            entry['local'] = entry['local'] and local
            if local:
                local_calls += 1
                local_tokens += tokens
            else:
                cloud_tokens += tokens
                cloud_spend += spend

        remaining = max(Decimal('0'), self.DAILY_BUDGET_USD - total_spend)
        reset = datetime.combine(
            now.date() + timedelta(days=1), time.min, tzinfo=self.TIMEZONE,
        )
        lines = [
            '## 我的今日 AI 額度',
            f"- 統計區間：{now:%Y-%m-%d} 00:00–現在（Asia/Taipei）",
            f"- 已使用額度：**US${total_spend:.6f} / US${self.DAILY_BUDGET_USD:.2f}**",
            f"- 剩餘額度：**US${remaining:.6f}**",
            f"- 下次重設：{reset:%Y-%m-%d %H:%M}（Asia/Taipei）",
            f"- 雲端模型：{len(rows) - local_calls} 次、{cloud_tokens:,} tokens、US${cloud_spend:.6f}",
            f"- 本地模型：{local_calls} 次、{local_tokens:,} tokens、US$0.000000",
            '',
            '### 模型明細',
        ]

        if by_model:
            for model, entry in sorted(by_model.items(), key=lambda item: item[1]['spend'], reverse=True):
                tag = '本地' if entry['local'] else '雲端'
                lines.append(
                    f"- {model}（{tag}）：{entry['calls']} 次、{entry['tokens']:,} tokens、US${entry['spend']:.6f}",
                )
        else:
            lines.append('- 今天尚無可歸屬於你的模型請求。')

        if truncated:
            lines.extend(
                ['', '> 今日紀錄超過 10,000 筆，畫面僅顯示前 10,000 筆；請聯絡管理員取得完整稽核報表。'],
            )
        lines.extend(
            ['', '> 這個入口只讀取目前登入者的 Keycloak 身分，不會顯示聊天內容、OIDC subject 或其他使用者資料。'],
        )
        return '\n'.join(lines)
