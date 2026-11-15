"""Public contracts for tenant-scoped administrator usage dashboards."""
from __future__ import annotations

from datetime import date as CalendarDate
from typing import Literal

from pydantic import BaseModel
from pydantic import Field

from examples.ai_usage.schemas.usage import ModelUsage
from examples.ai_usage.schemas.usage import SourceUsage
from examples.ai_usage.schemas.usage import UnattributedCategory
from examples.ai_usage.schemas.usage import UnattributedCategoryUsage
from examples.ai_usage.schemas.usage import UsageBudget
from examples.ai_usage.schemas.usage import UsagePeriod
from examples.ai_usage.schemas.usage import UsageRecord
from examples.ai_usage.schemas.usage import UsageSource
from examples.ai_usage.schemas.usage import UsageTotals


class UsageCapabilities(BaseModel):
    """UI hints; every data request is independently authorised."""

    can_view_admin_usage: bool
    scope: Literal['self', 'own_group', 'all_groups']
    managed_group_id: int | None
    can_filter_groups: bool
    timezone: str
    period_mode: Literal['today'] = 'today'
    supports_date_range: bool = True
    default_period: Literal['today'] = 'today'
    max_range_days: int = Field(ge=1)
    can_view_complete_ledger: bool = False
    group_attribution: Literal['current_membership'] = 'current_membership'


class UsageGroup(BaseModel):
    """One group visible to the caller."""

    group_id: int
    group_name: str


class UsageGroupsResponse(BaseModel):
    """Authorised choices for the group selector."""

    items: list[UsageGroup]


class UsageSelection(BaseModel):
    """Server-resolved selection used to build a response."""

    scope: Literal['own_group', 'all_groups']
    group_id: int | None
    user_id: int | None
    source: UsageSource | None = None
    group_attribution: Literal['current_membership'] = 'current_membership'


class UsageUser(BaseModel):
    """Local display identity; external accounting subjects stay private."""

    user_id: int
    username: str
    display_name: str
    group_id: int | None
    group_name: str | None


class UsageRangePeriod(UsagePeriod):
    """Inclusive local dates; reset_at is the exclusive upper time boundary."""

    start_date: CalendarDate
    end_date: CalendarDate
    is_today: bool


class AdminUserUsage(UsageUser):
    """One current member's selected usage; daily budget only for today."""

    account_status: str
    ai_identity_linked: bool
    budget: UsageBudget | None
    totals: UsageTotals
    user_totals: UsageTotals
    service_totals: UsageTotals
    sources: list[SourceUsage]


class AdminUsageRecord(UsageRecord, UsageUser):
    """Accounting details with a local user and current group label."""


class UnattributedUsageRecord(UsageRecord):
    """Unallocated accounting, never a fabricated local user identity."""

    accounting_id: str
    attribution_reason: Literal['missing_identity', 'identity_not_linked']
    category: UnattributedCategory


class UsageCoverage(BaseModel):
    """The exact ledger coverage, independent of display pagination."""

    scope: Literal['gateway_ledger', 'attributed_users']
    includes_unattributed: bool
    attributed_record_count: int = Field(ge=0)
    unattributed_record_count: int = Field(ge=0)


class AdminUsageSummary(BaseModel):
    """Full selection totals, independent of displayed page sizes."""

    period: UsageRangePeriod
    selection: UsageSelection
    user_count: int = Field(ge=0)
    linked_user_count: int = Field(ge=0)
    unlinked_user_count: int = Field(ge=0)
    totals: UsageTotals
    models: list[ModelUsage]
    user_totals: UsageTotals
    service_totals: UsageTotals
    sources: list[SourceUsage]
    attributed_totals: UsageTotals
    unattributed_totals: UsageTotals
    coverage: UsageCoverage
    unattributed_categories: list[UnattributedCategoryUsage]


class UsagePage(BaseModel):
    """Live offset pagination; refresh from page one when filters change."""

    period: UsageRangePeriod
    selection: UsageSelection
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total: int = Field(ge=0)
    has_more: bool
    coverage: UsageCoverage
    unattributed_totals: UsageTotals


class AdminUsageUsersResponse(UsagePage):
    """Users ordered by ascending local user ID."""

    items: list[AdminUserUsage]


class AdminUsageRecordsResponse(UsagePage):
    """Details ordered by timestamp descending, then local user ID."""

    items: list[AdminUsageRecord]


class UnattributedUsageRecordsResponse(UsagePage):
    """Gateway-only details, separate from existing user-row contracts."""

    items: list[UnattributedUsageRecord]
    category: UnattributedCategory | None = None
