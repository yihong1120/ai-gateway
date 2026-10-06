"""Public, non-sensitive response contracts for personal usage data."""
from __future__ import annotations

from datetime import date as CalendarDate
from datetime import datetime

from pydantic import BaseModel
from pydantic import Field


class UsagePeriod(BaseModel):
    """The Taiwan-local accounting period represented by this response."""

    date: CalendarDate
    timezone: str
    started_at: datetime
    reset_at: datetime


class UsageBudget(BaseModel):
    """Daily cloud spend budget for the authenticated user."""

    limit_usd: float = Field(ge=0)
    used_usd: float = Field(ge=0)
    remaining_usd: float = Field(ge=0)
    is_exhausted: bool


class UsageTotals(BaseModel):
    """Aggregate request, token, and cost totals for the current day."""

    requests: int = Field(ge=0)
    cloud_requests: int = Field(ge=0)
    local_requests: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    cache_read_input_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    spend_usd: float = Field(ge=0)


class ModelUsage(BaseModel):
    """One model's current-day usage without request or prompt contents."""

    model: str
    provider: str
    is_local: bool
    requests: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    cache_read_input_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    spend_usd: float = Field(ge=0)


class UsageRecord(BaseModel):
    """A single non-sensitive accounting event for the signed-in user."""

    started_at: datetime | None
    model: str
    provider: str
    is_local: bool
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    cache_read_input_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    spend_usd: float = Field(ge=0)


class DailyUsageResponse(BaseModel):
    """Private dashboard payload for the caller's current local day."""

    period: UsagePeriod
    budget: UsageBudget
    totals: UsageTotals
    models: list[ModelUsage]
    records: list[UsageRecord]
    record_count: int = Field(ge=0)
    records_truncated: bool
