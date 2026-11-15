"""Administrator date-range usage APIs with local directory authorisation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as CalendarDate
from typing import Annotated
from typing import Any
from typing import Literal

import httpx
from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from sqlalchemy.ext.asyncio import AsyncSession

from examples.ai_usage.config import AiUsageSettings
from examples.ai_usage.deps import get_admin_db
from examples.ai_usage.deps import get_current_claims
from examples.ai_usage.deps import get_litellm_http_client
from examples.ai_usage.deps import get_settings
from examples.ai_usage.schemas.admin_usage import AdminUsageRecordsResponse
from examples.ai_usage.schemas.admin_usage import AdminUsageSummary
from examples.ai_usage.schemas.admin_usage import AdminUsageUsersResponse
from examples.ai_usage.schemas.admin_usage import UnattributedUsageRecordsResponse
from examples.ai_usage.schemas.admin_usage import UsageCapabilities
from examples.ai_usage.schemas.admin_usage import UsageGroupsResponse
from examples.ai_usage.schemas.admin_usage import UsageSelection
from examples.ai_usage.schemas.usage import UnattributedCategory
from examples.ai_usage.schemas.usage import UsageSource
from examples.ai_usage.schemas.usage import UsageTotals
from examples.ai_usage.services.admin_usage import AdminUsageData
from examples.ai_usage.services.admin_usage import build_admin_usage_data
from examples.ai_usage.services.directory import can_view_complete_ledger
from examples.ai_usage.services.directory import DirectoryUser
from examples.ai_usage.services.directory import foreign_subjects
from examples.ai_usage.services.directory import list_selected_users
from examples.ai_usage.services.directory import list_visible_groups
from examples.ai_usage.services.directory import load_actor
from examples.ai_usage.services.directory import UsageActor
from examples.ai_usage.services.litellm_usage import DailyLogWindow
from examples.ai_usage.services.litellm_usage import IncompleteUsageData
from examples.ai_usage.services.litellm_usage import LiteLLMUsageClient
from examples.ai_usage.services.litellm_usage import LiteLLMUsageUnavailable


router = APIRouter(prefix='/v1/admin/usage', tags=['ai-usage-admin'])


@dataclass
class AdminUsageQuery:
    """An authorised selection; fetch only after endpoint validation passes."""

    settings: AiUsageSettings
    http_client: httpx.AsyncClient
    selection: UsageSelection
    users: list[DirectoryUser]
    window: DailyLogWindow
    include_unattributed: bool
    excluded_subjects: frozenset[str]

    async def load(self) -> AdminUsageData:
        """Read a complete day and normalise upstream failures."""
        client = LiteLLMUsageClient(self.settings, self.http_client)
        try:
            window, rows = await client.fetch_scoped_window(
                frozenset(user.end_user for user in self.users if user.end_user),
                self.window,
                include_unattributed=self.include_unattributed,
                excluded_subjects=self.excluded_subjects,
            )
        except IncompleteUsageData as exc:
            raise HTTPException(
                status_code=503,
                detail={'code': 'ai_usage_data_incomplete'},
            ) from exc
        except LiteLLMUsageUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={'code': 'ai_usage_upstream_unavailable'},
            ) from exc
        return build_admin_usage_data(
            settings=self.settings,
            window=window,
            selection=self.selection,
            users=self.users,
            rows=rows,
            include_unattributed=self.include_unattributed,
            excluded_subjects=self.excluded_subjects,
        )


async def get_usage_actor(
    claims: dict[str, Any] = Depends(get_current_claims),
    db: AsyncSession = Depends(get_admin_db),
    settings: AiUsageSettings = Depends(get_settings),
) -> UsageActor:
    """Ignore client role/group claims; use the current local account instead."""
    return await load_actor(
        db,
        subject=str(claims['sub']).strip(),
        provider=settings.oidc_identity_provider,
    )


def require_usage_admin(
    actor: UsageActor = Depends(get_usage_actor),
) -> UsageActor:
    """Deny ordinary and unassigned accounts before any data query."""
    if not actor.can_manage:
        raise HTTPException(
            status_code=403,
            detail={'code': 'ai_usage_admin_required'},
        )
    return actor


@router.get('/capabilities', response_model=UsageCapabilities)
async def get_usage_capabilities(
    actor: UsageActor = Depends(get_usage_actor),
    settings: AiUsageSettings = Depends(get_settings),
) -> UsageCapabilities:
    """Allow active ordinary users to discover that only personal UI is visible."""
    scope: Literal['self', 'own_group', 'all_groups'] = (
        'all_groups' if actor.is_super_admin
        else 'own_group' if actor.can_manage else 'self'
    )
    return UsageCapabilities(
        can_view_admin_usage=actor.can_manage,
        scope=scope,
        managed_group_id=actor.group_id if scope == 'own_group' else None,
        can_filter_groups=actor.is_super_admin,
        timezone=settings.timezone_name,
        max_range_days=settings.max_range_days,
        can_view_complete_ledger=can_view_complete_ledger(actor, settings.gateway_tenant_id),
    )


@router.get('/groups', response_model=UsageGroupsResponse)
async def get_usage_groups(
    actor: UsageActor = Depends(require_usage_admin),
    db: AsyncSession = Depends(get_admin_db),
) -> UsageGroupsResponse:
    """Return scoped dropdown choices, never the global directory."""
    return UsageGroupsResponse(items=await list_visible_groups(db, actor))


async def get_admin_usage_query(
    group_id: Annotated[int | None, Query(gt=0)] = None,
    user_id: Annotated[int | None, Query(gt=0)] = None,
    source: Annotated[UsageSource | None, Query()] = None,
    start_date: Annotated[CalendarDate | None, Query()] = None,
    end_date: Annotated[CalendarDate | None, Query()] = None,
    actor: UsageActor = Depends(require_usage_admin),
    db: AsyncSession = Depends(get_admin_db),
    settings: AiUsageSettings = Depends(get_settings),
    http_client: httpx.AsyncClient = Depends(get_litellm_http_client),
) -> AdminUsageQuery:
    """Resolve directory filters before making a bounded upstream query."""
    try:
        window = LiteLLMUsageClient(settings, http_client).range_window(start_date, end_date)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={'code': str(exc), 'max_range_days': settings.max_range_days},
        ) from exc
    resolved_group, users = await list_selected_users(
        db,
        actor,
        provider=settings.oidc_identity_provider,
        group_id=group_id,
        user_id=user_id,
    )
    include_unattributed = (
        can_view_complete_ledger(actor, settings.gateway_tenant_id)
        and resolved_group is None and user_id is None
    )
    excluded = await foreign_subjects(db, actor, settings.oidc_identity_provider) if include_unattributed else frozenset()
    return AdminUsageQuery(
        settings=settings,
        http_client=http_client,
        selection=UsageSelection(
            scope='all_groups' if actor.is_super_admin else 'own_group',
            group_id=resolved_group,
            user_id=user_id,
            source=source,
        ),
        users=users,
        window=window,
        include_unattributed=include_unattributed,
        excluded_subjects=excluded,
    )


@router.get('/summary', response_model=AdminUsageSummary)
async def get_usage_summary(
    query: AdminUsageQuery = Depends(get_admin_usage_query),
) -> AdminUsageSummary:
    """Return complete totals without applying any display page limit."""
    return (await query.load()).summary


@router.get('/users', response_model=AdminUsageUsersResponse)
async def get_usage_users(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
    query: AdminUsageQuery = Depends(get_admin_usage_query),
) -> AdminUsageUsersResponse:
    """Page over current members, including linked accounts with zero usage."""
    data = await query.load()
    start = (page - 1) * page_size
    return AdminUsageUsersResponse(
        period=data.summary.period,
        selection=data.summary.selection,
        page=page,
        page_size=page_size,
        total=len(data.users),
        has_more=start + page_size < len(data.users),
        items=data.users[start:start + page_size],
        coverage=data.summary.coverage,
        unattributed_totals=data.summary.unattributed_totals,
    )


@router.get('/records', response_model=AdminUsageRecordsResponse)
async def get_usage_records(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
    query: AdminUsageQuery = Depends(get_admin_usage_query),
) -> AdminUsageRecordsResponse:
    """Page over safe accounting details with local display identities."""
    data = await query.load()
    start = (page - 1) * page_size
    return AdminUsageRecordsResponse(
        period=data.summary.period,
        selection=data.summary.selection,
        page=page,
        page_size=page_size,
        total=data.record_count,
        has_more=start + page_size < data.record_count,
        items=data.records_page(start, page_size),
        coverage=data.summary.coverage,
        unattributed_totals=data.summary.unattributed_totals,
    )


def require_unattributed_query(
    query: AdminUsageQuery = Depends(get_admin_usage_query),
) -> AdminUsageQuery:
    """Unallocated records cannot be scoped to a guessed user or group."""
    if not query.include_unattributed:
        raise HTTPException(status_code=403, detail={'code': 'ai_usage_unattributed_forbidden'})
    return query


@router.get('/unattributed-records', response_model=UnattributedUsageRecordsResponse)
async def get_unattributed_records(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
    category: Annotated[UnattributedCategory | None, Query()] = None,
    query: AdminUsageQuery = Depends(require_unattributed_query),
) -> UnattributedUsageRecordsResponse:
    """View every unallocated accounting event without fabricating an owner."""
    data = await query.load()
    count, records = data.unattributed_page((page - 1) * page_size, page_size, category)
    totals = data.summary.unattributed_totals
    if category is not None:
        totals = next(
            (item.totals for item in data.summary.unattributed_categories if item.category == category),
            UsageTotals.model_validate({key: 0 for key in totals.model_dump()}),
        )
    start = (page - 1) * page_size
    return UnattributedUsageRecordsResponse(
        period=data.summary.period, selection=data.summary.selection,
        page=page, page_size=page_size, total=count,
        has_more=start + page_size < count,
        items=records,
        coverage=data.summary.coverage, unattributed_totals=totals, category=category,
    )
