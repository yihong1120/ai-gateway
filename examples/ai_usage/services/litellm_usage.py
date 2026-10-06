"""Read and safely aggregate a single end user's LiteLLM spend logs."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from datetime import time
from datetime import timedelta
from datetime import timezone
from decimal import Decimal
from decimal import InvalidOperation
from typing import Any
from typing import cast
from typing import TypedDict

import httpx

from examples.ai_usage.config import AiUsageSettings
from examples.ai_usage.schemas.usage import DailyUsageResponse
from examples.ai_usage.schemas.usage import ModelUsage
from examples.ai_usage.schemas.usage import UsageBudget
from examples.ai_usage.schemas.usage import UsagePeriod
from examples.ai_usage.schemas.usage import UsageRecord
from examples.ai_usage.schemas.usage import UsageTotals


class LiteLLMUsageError(RuntimeError):
    """Base class for a non-sensitive LiteLLM usage read failure."""


class LiteLLMUsageUnavailable(LiteLLMUsageError):
    """LiteLLM cannot currently serve the restricted usage query."""


class IncompleteUsageData(LiteLLMUsageError):
    """The configured log-page bound prevents an exact daily total."""


@dataclass(frozen=True, slots=True)
class DailyLogWindow:
    """A timezone-aware local-day window expressed for LiteLLM queries."""

    now: datetime
    started_at: datetime
    reset_at: datetime


@dataclass(slots=True)
class _UsageAccumulator:
    """Mutable totals kept private until a public schema is constructed."""

    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_input_tokens: int = 0
    total_tokens: int = 0
    spend_usd: Decimal = Decimal('0')

    def add(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cache_read_input_tokens: int,
        total_tokens: int,
        spend_usd: Decimal,
    ) -> None:
        """Add one sanitised spend-log row to this aggregate."""
        self.requests += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.cache_read_input_tokens += cache_read_input_tokens
        self.total_tokens += total_tokens
        self.spend_usd += spend_usd


class LiteLLMUsageClient:
    """Restricted client for LiteLLM's v2 spend-log endpoint.

    This client deliberately accepts only an end-user ID derived by the auth
    dependency.  Its callers never pass a provider key or LiteLLM master key.
    """

    def __init__(
        self,
        settings: AiUsageSettings,
        client: httpx.AsyncClient,
    ) -> None:
        """Bind the shared HTTP client to validated service settings."""
        self._settings = settings
        self._client = client

    def current_window(self, now: datetime | None = None) -> DailyLogWindow:
        """Return the current accounting day in the configured timezone."""
        local_now = (now or datetime.now(self._settings.timezone)).astimezone(
            self._settings.timezone,
        )
        started_at = datetime.combine(
            local_now.date(),
            time.min,
            tzinfo=self._settings.timezone,
        )
        reset_at = started_at + timedelta(days=1)
        return DailyLogWindow(
            now=local_now,
            started_at=started_at,
            reset_at=reset_at,
        )

    async def fetch_current_day(
        self,
        end_user: str,
        *,
        now: datetime | None = None,
    ) -> tuple[DailyLogWindow, list[dict[str, Any]]]:
        """Fetch all available current-day rows for one immutable OIDC sub.

        A partial result is deliberately rejected: returning a too-large
        remaining budget would be worse than asking the caller to retry after
        the server's safe page cap is raised by an administrator.
        """
        window = self.current_window(now)
        rows: list[dict[str, Any]] = []
        total_pages: int | None = None

        for page in range(1, self._settings.max_log_pages + 1):
            params = {
                'start_date': self._litellm_timestamp(window.started_at),
                'end_date': self._litellm_timestamp(
                    window.reset_at - timedelta(seconds=1),
                ),
                'end_user': end_user,
                'page': page,
                'page_size': self._settings.log_page_size,
                'sort_by': 'startTime',
                'sort_order': 'asc',
            }
            payload = await self._get_spend_log_page(params)
            raw_rows = payload.get('data')
            if not isinstance(raw_rows, list):
                raise LiteLLMUsageUnavailable('invalid_usage_response')
            for row in raw_rows:
                if not isinstance(row, dict):
                    continue
                # LiteLLM is queried with ``end_user`` above, but do not rely
                # solely on an upstream filter when protecting one person's
                # dashboard.  A missing ownership field is treated as an
                # invalid response rather than silently showing a false zero
                # balance; an unexpected owner is safely discarded.
                returned_end_user = row.get('end_user')
                if returned_end_user is None:
                    raise LiteLLMUsageUnavailable('missing_end_user')
                if returned_end_user != end_user:
                    continue
                rows.append(row)

            advertised_pages = _nonnegative_int(payload.get('total_pages'))
            if advertised_pages is not None:
                total_pages = max(1, advertised_pages)
                if total_pages > self._settings.max_log_pages:
                    raise IncompleteUsageData('usage_log_page_cap_reached')
                if page >= total_pages:
                    return window, rows
                continue

            # Older LiteLLM versions may omit pagination metadata.  A short
            # page is conclusive; a full final permitted page is not.
            if len(raw_rows) < self._settings.log_page_size:
                return window, rows

        if total_pages is None or total_pages > self._settings.max_log_pages:
            raise IncompleteUsageData('usage_log_page_cap_reached')
        return window, rows

    async def _get_spend_log_page(
        self,
        params: Mapping[str, object],
    ) -> dict[str, Any]:
        """Call LiteLLM and normalise all upstream failures to a safe error."""
        try:
            response = await self._client.get(
                '/spend/logs/v2',
                params=params,
                headers={
                    'Authorization': (
                        f'Bearer {self._settings.litellm_reader_key}'
                    ),
                    'Accept': 'application/json',
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (
            httpx.HTTPError,
            ValueError,
        ) as exc:
            raise LiteLLMUsageUnavailable('litellm_usage_unavailable') from exc
        if not isinstance(payload, dict):
            raise LiteLLMUsageUnavailable('invalid_usage_response')
        return payload

    @staticmethod
    def _litellm_timestamp(value: datetime) -> str:
        """Format a UTC timestamp in LiteLLM's established v2 API format."""
        return value.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


class _UsageFields(TypedDict):
    prompt_tokens: int
    completion_tokens: int
    cache_read_input_tokens: int
    total_tokens: int
    spend_usd: Decimal


def build_daily_usage_response(
    *,
    settings: AiUsageSettings,
    window: DailyLogWindow,
    rows: list[dict[str, Any]],
    record_limit: int,
) -> DailyUsageResponse:
    """Return a privacy-safe daily dashboard from already filtered log rows."""
    total = _UsageAccumulator()
    cloud = _UsageAccumulator()
    local = _UsageAccumulator()
    model_totals: dict[tuple[str, str, bool], _UsageAccumulator] = defaultdict(
        _UsageAccumulator,
    )
    records: list[UsageRecord] = []

    for row in rows:
        model = _model_name(row)
        provider = _provider_name(row)
        is_local = _is_local(row, settings)
        prompt_tokens = _nonnegative_int(row.get('prompt_tokens')) or 0
        completion_tokens = _nonnegative_int(row.get('completion_tokens')) or 0
        cache_tokens = (
            _nonnegative_int(row.get('cache_read_input_tokens')) or 0
        )
        reported_total_tokens = _nonnegative_int(row.get('total_tokens'))
        total_tokens = (
            reported_total_tokens
            if reported_total_tokens is not None
            else prompt_tokens + completion_tokens
        )
        spend_usd = _nonnegative_decimal(row.get('spend'))

        fields: _UsageFields = {
            'prompt_tokens': prompt_tokens,
            'completion_tokens': completion_tokens,
            'cache_read_input_tokens': cache_tokens,
            'total_tokens': total_tokens,
            'spend_usd': spend_usd,
        }
        total.add(**fields)
        target = local if is_local else cloud
        target.add(**fields)
        model_totals[(model, provider, is_local)].add(**fields)
        records.append(
            UsageRecord(
                started_at=_timestamp(
                    row.get('startTime') or row.get('start_time'),
                ),
                model=model,
                provider=provider,
                is_local=is_local,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cache_read_input_tokens=cache_tokens,
                total_tokens=total_tokens,
                spend_usd=_money_float(spend_usd),
            ),
        )

    models = [
        ModelUsage(
            model=model,
            provider=provider,
            is_local=is_local,
            requests=aggregate.requests,
            prompt_tokens=aggregate.prompt_tokens,
            completion_tokens=aggregate.completion_tokens,
            cache_read_input_tokens=aggregate.cache_read_input_tokens,
            total_tokens=aggregate.total_tokens,
            spend_usd=_money_float(aggregate.spend_usd),
        )
        for (model, provider, is_local), aggregate in model_totals.items()
    ]
    models.sort(
        key=lambda item: (
            -item.spend_usd,
            -item.requests,
            item.model.casefold(),
            item.provider.casefold(),
        ),
    )
    records.sort(
        key=lambda item: item.started_at or datetime.min.replace(
            tzinfo=timezone.utc,
        ),
        reverse=True,
    )
    returned_records = records[:record_limit]

    remaining = max(Decimal('0'), settings.daily_budget_usd - total.spend_usd)
    return DailyUsageResponse(
        period=UsagePeriod(
            date=window.now.date(),
            timezone=settings.timezone_name,
            started_at=window.started_at,
            reset_at=window.reset_at,
        ),
        budget=UsageBudget(
            limit_usd=_money_float(settings.daily_budget_usd),
            used_usd=_money_float(total.spend_usd),
            remaining_usd=_money_float(remaining),
            is_exhausted=total.spend_usd >= settings.daily_budget_usd,
        ),
        totals=UsageTotals(
            requests=total.requests,
            cloud_requests=cloud.requests,
            local_requests=local.requests,
            prompt_tokens=total.prompt_tokens,
            completion_tokens=total.completion_tokens,
            cache_read_input_tokens=total.cache_read_input_tokens,
            total_tokens=total.total_tokens,
            spend_usd=_money_float(total.spend_usd),
        ),
        models=models,
        records=returned_records,
        record_count=len(records),
        records_truncated=len(returned_records) < len(records),
    )


def _model_name(row: Mapping[str, object]) -> str:
    """Select a stable display name without exposing arbitrary metadata."""
    value = row.get('model_group') or row.get('model')
    if not isinstance(value, str) or not value.strip():
        return 'unknown'
    return value.strip()[:200]


def _provider_name(row: Mapping[str, object]) -> str:
    """Select LiteLLM's provider label, falling back to a safe value."""
    value = row.get('custom_llm_provider') or row.get('provider')
    if not isinstance(value, str) or not value.strip():
        return 'unknown'
    return value.strip()[:80]


def _is_local(
    row: Mapping[str, object],
    settings: AiUsageSettings,
) -> bool:
    """Classify known local backends without using model metadata as input."""
    model = _model_name(row)
    provider = _provider_name(row).casefold()
    api_base = str(row.get('api_base') or '').casefold()
    return (
        model in settings.local_model_names
        or provider.startswith(('ollama', 'tabby', 'exllama'))
        or api_base.startswith(
            (
                'http://127.0.0.1',
                'http://localhost',
                'http://[::1]',
            ),
        )
    )


def _nonnegative_int(value: object) -> int | None:
    """Convert an upstream token/page value to a non-negative integer."""
    if isinstance(value, bool):
        return None
    try:
        result = int(cast(Any, value))
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _nonnegative_decimal(value: object) -> Decimal:
    """Convert an upstream cost to a safe non-negative decimal value."""
    try:
        result = Decimal(str(value if value is not None else '0'))
    except (InvalidOperation, ValueError):
        return Decimal('0')
    return result if result >= 0 else Decimal('0')


def _money_float(value: Decimal) -> float:
    """Return stable micro-dollar precision as a JSON numeric value."""
    return float(value.quantize(Decimal('0.000001')))


def _timestamp(value: object) -> datetime | None:
    """Parse LiteLLM's timestamp formats while keeping invalid data private."""
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str) and value.strip():
        try:
            result = datetime.fromisoformat(
                value.strip().replace('Z', '+00:00'),
            )
        except ValueError:
            return None
    else:
        return None
    return (
        result.replace(tzinfo=timezone.utc)
        if result.tzinfo is None
        else result.astimezone(timezone.utc)
    )
