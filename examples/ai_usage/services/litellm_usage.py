"""Read and safely aggregate a single end user's LiteLLM spend logs."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from datetime import date as CalendarDate
from decimal import Decimal, InvalidOperation
from typing import Any, TypedDict, cast

import httpx

from examples.ai_usage.config import UsageQuerySettings
from examples.ai_usage.schemas.usage import (
    DailyUsageResponse,
    ModelUsage,
    UsageBudget,
    UsagePeriod,
    UsageRecord,
    UsageTotals,
)
from examples.ai_usage.services.accounting import (
    accounting_owner,
    is_user_billed,
    usage_source,
)


class LiteLLMUsageError(RuntimeError):
    """Base class for a non-sensitive LiteLLM usage read failure."""


class LiteLLMUsageUnavailable(LiteLLMUsageError):
    """LiteLLM cannot currently serve the restricted usage query."""


class IncompleteUsageData(LiteLLMUsageError):
    """The configured log-page bound prevents an exact daily total."""


@dataclass(slots=True)
class _PageBudget:
    """Bound every request, including probes needed to split capped ranges."""

    remaining: int


@dataclass(frozen=True, slots=True)
class DailyLogWindow:
    """A timezone-aware day or administrator date range for LiteLLM queries."""

    now: datetime
    started_at: datetime
    reset_at: datetime

    @property
    def is_today(self) -> bool:
        """Daily budget displays are meaningful only for exactly today."""
        return (
            self.started_at.date() == self.now.date()
            and self.reset_at == self.started_at + timedelta(days=1)
        )


class LiteLLMUsageClient:
    """Restricted client for LiteLLM's v2 spend-log endpoint.

    This client deliberately accepts only an end-user ID derived by the auth
    dependency.  Its callers never pass a provider key or LiteLLM master key.
    """

    def __init__(
        self,
        settings: UsageQuerySettings,
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

    def range_window(
        self,
        start_date: CalendarDate | None,
        end_date: CalendarDate | None,
        *,
        now: datetime | None = None,
    ) -> DailyLogWindow:
        """Validate inclusive local dates once, before directory/upstream I/O."""
        current = self.current_window(now)
        if start_date is None and end_date is None:
            return current
        if start_date is None or end_date is None:
            raise ValueError('ai_usage_date_range_required')
        if start_date > end_date:
            raise ValueError('ai_usage_invalid_date_range')
        if end_date > current.now.date():
            raise ValueError('ai_usage_future_date_not_allowed')
        if (end_date - start_date).days + 1 > self._settings.max_range_days:
            raise ValueError('ai_usage_date_range_too_long')
        return DailyLogWindow(
            now=current.now,
            started_at=datetime.combine(
                start_date, time.min, tzinfo=self._settings.timezone
            ),
            reset_at=datetime.combine(
                end_date + timedelta(days=1), time.min, tzinfo=self._settings.timezone
            ),
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
        return window, await self._fetch_day_rows(
            window,
            frozenset({end_user}),
        )

    async def daily_usage(
        self, subject: str, record_limit: int = 0, now: datetime | None = None,
    ) -> DailyUsageResponse:
        """Return exact daily totals; only detail rendering is bounded by the limit."""
        window, rows = await self.fetch_current_day(subject, now=now)
        return build_daily_usage_response(
            settings=self._settings, window=window, rows=rows, record_limit=record_limit,
        )

    async def fetch_scoped_day(
        self,
        end_users: frozenset[str],
        *,
        now: datetime | None = None,
    ) -> tuple[DailyLogWindow, list[dict[str, Any]]]:
        """Read a verified directory selection in one paginated scan.

        A bounded day scan includes service-paid document rows whose verified
        actor is stored in metadata instead of the budget-charged end_user.
        Caller input must first be resolved through the local directory.
        """
        window = self.current_window(now)
        return await self.fetch_scoped_window(end_users, window)

    async def fetch_scoped_window(
        self,
        end_users: frozenset[str],
        window: DailyLogWindow,
        *,
        include_unattributed: bool = False,
        excluded_subjects: frozenset[str] = frozenset(),
    ) -> tuple[DailyLogWindow, list[dict[str, Any]]]:
        """Read a server-validated date window with complete scope filtering."""
        if not end_users and not include_unattributed:
            return window, []
        budget = _PageBudget(self._settings.max_log_pages)
        return window, await self._fetch_scoped_parts(
            window,
            end_users,
            budget,
            include_unattributed,
            excluded_subjects,
        )

    async def _fetch_scoped_parts(
        self,
        window: DailyLogWindow,
        end_users: frozenset[str],
        budget: _PageBudget,
        include_unattributed: bool = False,
        excluded_subjects: frozenset[str] = frozenset(),
    ) -> list[dict[str, Any]]:
        """Bisect upstream-capped ranges without double counting midnight."""
        try:
            return await self._fetch_day_rows(
                window,
                end_users,
                include_service=True,
                budget=budget,
                include_unattributed=include_unattributed,
                excluded_subjects=excluded_subjects,
            )
        except IncompleteUsageData as exc:
            if str(exc) != 'upstream_usage_total_capped':
                raise
            seconds = int((window.reset_at - window.started_at).total_seconds())
            if seconds <= 1:
                raise
        middle = window.started_at + timedelta(seconds=seconds // 2)
        first = DailyLogWindow(window.now, window.started_at, middle)
        second = DailyLogWindow(window.now, middle, window.reset_at)
        rows = await self._fetch_scoped_parts(
            first, end_users, budget, include_unattributed, excluded_subjects
        )
        rows.extend(
            await self._fetch_scoped_parts(
                second, end_users, budget, include_unattributed, excluded_subjects
            )
        )
        return rows

    async def _fetch_day_rows(
        self,
        window: DailyLogWindow,
        end_users: frozenset[str],
        *,
        include_service: bool = False,
        budget: _PageBudget | None = None,
        include_unattributed: bool = False,
        excluded_subjects: frozenset[str] = frozenset(),
    ) -> list[dict[str, Any]]:
        """Collect a complete bounded day and enforce returned ownership."""
        rows: list[dict[str, Any]] = []
        total_pages: int | None = None

        for page in range(1, self._settings.max_log_pages + 1):
            params: dict[str, str | int] = {
                'start_date': self._litellm_timestamp(window.started_at),
                'end_date': self._litellm_timestamp(window.reset_at),
                'page': page,
                'page_size': self._settings.log_page_size,
                'sort_by': 'startTime',
                'sort_order': 'asc',
            }
            if len(end_users) == 1 and not include_service:
                params['end_user'] = next(iter(end_users))
            if budget is not None:
                if budget.remaining <= 0:
                    raise IncompleteUsageData('usage_log_page_cap_reached')
                budget.remaining -= 1
            payload = await self._get_spend_log_page(params)
            if payload.get('total_is_capped') is True:
                raise IncompleteUsageData('upstream_usage_total_capped')
            raw_rows = payload.get('data')
            if not isinstance(raw_rows, list):
                raise LiteLLMUsageUnavailable('invalid_usage_response')
            for row in raw_rows:
                if not isinstance(row, dict):
                    continue
                # Do not rely solely on upstream filters. A missing owner is an
                # invalid response rather than silently showing a false zero
                # balance; an unexpected owner is safely discarded.
                returned_end_user = (
                    accounting_owner(row, self._settings)
                    if include_service
                    else row.get('end_user')
                )
                if (
                    isinstance(returned_end_user, str)
                    and returned_end_user in excluded_subjects
                ):
                    continue
                if not isinstance(returned_end_user, str) and not include_unattributed:
                    if include_service or len(end_users) > 1:
                        # Global day scans can include service traffic without
                        # an end user. It cannot belong to a directory member.
                        continue
                    raise LiteLLMUsageUnavailable('missing_end_user')
                if returned_end_user not in end_users and not include_unattributed:
                    continue
                started = _timestamp(row.get('startTime') or row.get('start_time'))
                if started is None:
                    raise LiteLLMUsageUnavailable('missing_usage_timestamp')
                # The upstream end bound is inclusive. Query next midnight
                # then discard it locally, retaining the last microsecond.
                if not window.started_at <= started < window.reset_at:
                    continue
                rows.append(row)

            advertised_pages = _nonnegative_int(payload.get('total_pages'))
            if advertised_pages is not None:
                total_pages = max(1, advertised_pages)
                if total_pages > self._settings.max_log_pages:
                    raise IncompleteUsageData('usage_log_page_cap_reached')
                if page >= total_pages:
                    return rows
                continue

            # Older LiteLLM versions may omit pagination metadata.  A short
            # page is conclusive; a full final permitted page is not.
            if len(raw_rows) < self._settings.log_page_size:
                return rows

        if total_pages is None or total_pages > self._settings.max_log_pages:
            raise IncompleteUsageData('usage_log_page_cap_reached')
        return rows

    async def _get_spend_log_page(
        self,
        params: Mapping[str, str | int],
    ) -> dict[str, Any]:
        """Call LiteLLM and normalise all upstream failures to a safe error."""
        try:
            response = await self._client.get(
                '/spend/logs/v2',
                params=params,
                headers={
                    'Authorization': (f'Bearer {self._settings.litellm_reader_key}'),
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
        return value.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')


class _UsageFields(TypedDict):
    prompt_tokens: int
    completion_tokens: int
    cache_read_input_tokens: int
    total_tokens: int
    spend_usd: Decimal


def _usage_fields(row: Mapping[str, Any]) -> _UsageFields:
    prompt = _nonnegative_int(row.get('prompt_tokens')) or 0
    completion = _nonnegative_int(row.get('completion_tokens')) or 0
    reported = _nonnegative_int(row.get('total_tokens'))
    return {
        'prompt_tokens': prompt,
        'completion_tokens': completion,
        'cache_read_input_tokens': _nonnegative_int(row.get('cache_read_input_tokens'))
        or 0,
        'total_tokens': reported if reported is not None else prompt + completion,
        'spend_usd': _nonnegative_decimal(row.get('spend')),
    }


@dataclass(frozen=True, slots=True)
class UsageFact:
    started_at: datetime | None
    model: str
    provider: str
    is_local: bool
    fields: _UsageFields
    source: str
    billed: bool

    @classmethod
    def from_row(cls, row, settings):
        return cls(
            _timestamp(row.get('startTime') or row.get('start_time')),
            _model_name(row),
            _provider_name(row),
            _is_local(row, settings),
            _usage_fields(row),
            usage_source(row, settings),
            is_user_billed(row),
        )

    @property
    def spend_usd(self):
        return _money_float(self.fields['spend_usd'])

    def record(self):
        return UsageRecord(
            started_at=self.started_at,
            model=self.model,
            provider=self.provider,
            is_local=self.is_local,
            **{**self.fields, 'spend_usd': _money_float(self.fields['spend_usd'])},
            source=self.source,
            billing_scope='user' if self.billed else 'service',
        )

    def reference_data(self):
        # Match the public record's JSON representation without constructing a
        # response model for every unallocated row on a summary request.
        return dict(
            started_at=self.started_at.isoformat().replace('+00:00', 'Z')
            if self.started_at
            else None,
            model=self.model,
            provider=self.provider,
            is_local=self.is_local,
            **{**self.fields, 'spend_usd': _money_float(self.fields['spend_usd'])},
            source=self.source,
            billing_scope='user' if self.billed else 'service',
        )


@dataclass(slots=True)
class UsageTotalsAccumulator:
    """Exact totals shared by owner, source and model aggregation."""

    requests: int = 0
    cloud_requests: int = 0
    local_requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_input_tokens: int = 0
    total_tokens: int = 0
    spend_usd: Decimal = Decimal(0)

    def add(self, fact):
        self.requests += 1
        self.local_requests += int(fact.is_local)
        self.cloud_requests += int(not fact.is_local)
        self.prompt_tokens += fact.fields['prompt_tokens']
        self.completion_tokens += fact.fields['completion_tokens']
        self.cache_read_input_tokens += fact.fields['cache_read_input_tokens']
        self.total_tokens += fact.fields['total_tokens']
        self.spend_usd += fact.fields['spend_usd']

    def schema(self):
        return UsageTotals(
            requests=self.requests,
            cloud_requests=self.cloud_requests,
            local_requests=self.local_requests,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens,
            total_tokens=self.total_tokens,
            spend_usd=_money_float(self.spend_usd),
        )


class UsageAggregate(UsageTotalsAccumulator):
    def __init__(self):
        super().__init__()
        self.by_model = defaultdict(UsageTotalsAccumulator)

    def add(self, fact):
        super().add(fact)
        self.by_model[(fact.model, fact.provider, fact.is_local)].add(fact)

    def models(self):
        models = [
            ModelUsage(
                model=model,
                provider=provider,
                is_local=local,
                requests=total.requests,
                prompt_tokens=total.prompt_tokens,
                completion_tokens=total.completion_tokens,
                cache_read_input_tokens=total.cache_read_input_tokens,
                total_tokens=total.total_tokens,
                spend_usd=_money_float(total.spend_usd),
            )
            for (model, provider, local), total in self.by_model.items()
        ]
        return sorted(
            models,
            key=lambda item: (
                -item.spend_usd,
                -item.requests,
                item.model.casefold(),
                item.provider.casefold(),
            ),
        )


def usage_budget(settings, spend):
    return UsageBudget(
        limit_usd=_money_float(settings.daily_budget_usd),
        used_usd=_money_float(spend),
        remaining_usd=_money_float(
            max(Decimal(0), settings.daily_budget_usd - spend)
        ),
        is_exhausted=spend >= settings.daily_budget_usd,
    )


def build_daily_usage_response(
    *,
    settings: UsageQuerySettings,
    window: DailyLogWindow,
    rows: list[dict[str, Any]],
    record_limit: int,
) -> DailyUsageResponse:
    import heapq

    aggregate = UsageAggregate()

    def facts():
        for row in rows:
            fact = UsageFact.from_row(row, settings)
            aggregate.add(fact)
            yield fact

    if record_limit > 0:
        selected = heapq.nlargest(
            record_limit,
            facts(),
            key=lambda fact: (
                fact.started_at or datetime.min.replace(tzinfo=UTC)
            ),
        )
    else:
        for _ in facts():
            pass
        selected = []
    return DailyUsageResponse(
        period=UsagePeriod(
            date=window.now.date(),
            timezone=settings.timezone_name,
            started_at=window.started_at,
            reset_at=window.reset_at,
        ),
        budget=usage_budget(settings, aggregate.spend_usd),
        totals=aggregate.schema(),
        models=aggregate.models(),
        records=[fact.record() for fact in selected],
        record_count=aggregate.requests,
        records_truncated=len(selected) < aggregate.requests,
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
    settings: UsageQuerySettings,
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
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def _nonnegative_decimal(value: object) -> Decimal:
    """Convert an upstream cost to a safe non-negative decimal value."""
    try:
        result = Decimal(str(value if value is not None else '0'))
    except (InvalidOperation, ValueError):
        return Decimal(0)
    return result if result.is_finite() and result >= 0 else Decimal(0)


def _money_float(value: Decimal) -> float:
    """Return stable micro-dollar precision as a JSON numeric value."""
    return float(value.quantize(Decimal('0.000001')))


def _timestamp(value: object) -> datetime | None:
    """Parse LiteLLM's timestamp formats while keeping invalid data private."""
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str) and value.strip():
        try:
            result = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    return (
        result.replace(tzinfo=UTC)
        if result.tzinfo is None
        else result.astimezone(UTC)
    )
