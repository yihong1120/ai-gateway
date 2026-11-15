"""Render a complete personal usage response without exposing raw ledger rows."""

from decimal import Decimal
from zoneinfo import ZoneInfo

from examples.ai_usage.schemas.usage import DailyUsageResponse


def render_daily_usage(usage: DailyUsageResponse) -> str:
    """Display the same totals, budget and model prices as the personal API."""
    cloud = [model for model in usage.models if not model.is_local]
    local = [model for model in usage.models if model.is_local]
    cloud_cost = sum((Decimal(str(model.spend_usd)) for model in cloud), Decimal(0))
    local_cost = sum((Decimal(str(model.spend_usd)) for model in local), Decimal(0))
    reset = usage.period.reset_at.astimezone(ZoneInfo(usage.period.timezone))
    lines = [
        '## 我的今日 AI 額度',
        f'- 統計區間：{usage.period.date:%Y-%m-%d} 00:00–現在（{usage.period.timezone}）',
        f'- 已使用額度：**US${usage.budget.used_usd:.6f} / US${usage.budget.limit_usd:.2f}**',
        f'- 剩餘額度：**US${usage.budget.remaining_usd:.6f}**',
        f'- 下次重設：{reset:%Y-%m-%d %H:%M}（{usage.period.timezone}）',
        f'- 雲端模型：{usage.totals.cloud_requests} 次、{sum(model.total_tokens for model in cloud):,} tokens、US${cloud_cost:.6f}',
        f'- 本地模型：{usage.totals.local_requests} 次、{sum(model.total_tokens for model in local):,} tokens、US${local_cost:.6f}',
        '',
        '### 模型明細',
    ]
    for model in usage.models:
        tag = '本地' if model.is_local else '雲端'
        lines.append(f'- {model.model}（{tag}）：{model.requests} 次、{model.total_tokens:,} tokens、US${model.spend_usd:.6f}')
    if not usage.models:
        lines.append('- 今天尚無可歸屬於你的模型請求。')
    lines.extend([
        '',
        '> 這個入口只讀取目前登入者的 Keycloak 身分，不會顯示聊天內容、OIDC subject 或其他使用者資料。',
    ])
    return '\n'.join(lines)
