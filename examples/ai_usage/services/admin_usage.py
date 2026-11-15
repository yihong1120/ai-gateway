"""Aggregate an authorised ledger once; construct detail models only for a page."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from examples.ai_usage.config import AiUsageSettings
from examples.ai_usage.services.directory import DirectoryUser

from examples.ai_usage.schemas.admin_usage import (
    AdminUsageRecord,
    AdminUsageSummary,
    AdminUserUsage,
    UnattributedUsageRecord,
    UsageCoverage,
    UsageRangePeriod,
    UsageSelection,
)
from examples.ai_usage.schemas.usage import SourceUsage, UnattributedCategoryUsage
from examples.ai_usage.services.accounting import (
    accounting_owner,
    unattributed_category,
)
from examples.ai_usage.services.litellm_usage import (
    UsageAggregate,
    UsageFact,
    UsageTotalsAccumulator,
    DailyLogWindow,
    usage_budget,
)

_MIN_TIME = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class _Record:
    fact: UsageFact
    identity: dict | None = None
    accounting_id: str | None = None
    reason: str | None = None
    category: str | None = None

    def model(self):
        record = self.fact.record().model_dump()
        if self.identity is not None:
            return AdminUsageRecord(**record, **self.identity)
        return UnattributedUsageRecord(
            **record,
            accounting_id=self.accounting_id,
            attribution_reason=self.reason,
            category=self.category,
        )


@dataclass
class AdminUsageData:
    summary: AdminUsageSummary
    users: list[AdminUserUsage]
    _records: list[_Record] = field(repr=False)
    _unattributed: list[_Record] = field(repr=False)

    @property
    def record_count(self):
        return len(self._records)

    def records_page(self, start, size):
        return [entry.model() for entry in self._records[start : start + size]]

    def unattributed_page(self, start, size, category=None):
        matching = (
            self._unattributed
            if category is None
            else [entry for entry in self._unattributed if entry.category == category]
        )
        return len(matching), [
            entry.model() for entry in matching[start : start + size]
        ]



class _OwnerTotals:
    def __init__(self):
        self.total = UsageTotalsAccumulator()
        self.user = UsageTotalsAccumulator()
        self.service = UsageTotalsAccumulator()
        self.sources = defaultdict(UsageTotalsAccumulator)

    def add(self, fact):
        self.total.add(fact)
        (self.user if fact.billed else self.service).add(fact)
        self.sources[fact.source].add(fact)

    def source_models(self):
        return [
            SourceUsage(source=source, totals=self.sources[source].schema())
            for source in sorted(self.sources)
        ]


def build_admin_usage_data(
    *,
    settings: AiUsageSettings,
    window: DailyLogWindow,
    selection: UsageSelection,
    users: list[DirectoryUser],
    rows: list[dict[str, Any]],
    include_unattributed: bool = False,
    excluded_subjects: frozenset[str] = frozenset(),
) -> AdminUsageData:
    allowed = defaultdict(list)
    for user in users:
        if user.end_user:
            allowed[user.end_user].append(user.identity.model_dump())
    aggregate, selected = UsageAggregate(), _OwnerTotals()
    attributed, unattributed = UsageTotalsAccumulator(), UsageTotalsAccumulator()
    by_owner = defaultdict(_OwnerTotals)
    budgets = defaultdict(UsageTotalsAccumulator)
    categories = defaultdict(UsageTotalsAccumulator)
    records, unallocated = [], []
    for row in rows:
        owner = accounting_owner(row, settings)
        if owner in excluded_subjects or (
            not include_unattributed and owner not in allowed
        ):
            continue
        fact = UsageFact.from_row(row, settings)
        # User budgets include billed costs from every source, even when the
        # display selection filters a single application.
        if fact.billed:
            budgets[row['end_user']].add(fact)
        if selection.source is not None and fact.source != selection.source:
            continue
        aggregate.add(fact)
        selected.add(fact)
        if owner in allowed:
            attributed.add(fact)
            by_owner[owner].add(fact)
            records.extend(_Record(fact, identity) for identity in allowed[owner])
        else:
            unattributed.add(fact)
            category = unattributed_category(row, settings)
            categories[category].add(fact)
            reference = str(
                row.get('request_id')
                or json.dumps(fact.reference_data(), sort_keys=True)
            )
            unallocated.append(
                _Record(
                    fact,
                    accounting_id=hashlib.sha256(reference.encode()).hexdigest()[:24],
                    reason='identity_not_linked' if owner else 'missing_identity',
                    category=category,
                )
            )
    records.sort(
        key=lambda entry: (
            entry.identity['user_id'],
            entry.fact.model,
            entry.fact.provider,
            entry.fact.fields['total_tokens'],
            entry.fact.spend_usd,
        )
    )
    records.sort(key=lambda entry: entry.fact.started_at or _MIN_TIME, reverse=True)
    unallocated.sort(key=lambda entry: entry.accounting_id)
    unallocated.sort(key=lambda entry: entry.fact.started_at or _MIN_TIME, reverse=True)
    user_items = []
    for user in users:
        totals = by_owner[user.end_user or '']
        user_items.append(
            AdminUserUsage(
                **user.identity.model_dump(),
                account_status=user.account_status,
                ai_identity_linked=user.end_user is not None,
                budget=usage_budget(
                    settings, budgets[user.end_user or ''].spend_usd
                )
                if user.end_user and window.is_today
                else None,
                totals=totals.total.schema(),
                user_totals=totals.user.schema(),
                service_totals=totals.service.schema(),
                sources=totals.source_models(),
            )
        )
    linked = sum(user.end_user is not None for user in users)
    return AdminUsageData(
        AdminUsageSummary(
            period=UsageRangePeriod(
                date=window.started_at.date(),
                start_date=window.started_at.date(),
                end_date=(window.reset_at - timedelta(days=1)).date(),
                is_today=window.is_today,
                timezone=settings.timezone_name,
                started_at=window.started_at,
                reset_at=window.reset_at,
            ),
            selection=selection,
            user_count=len(users),
            linked_user_count=linked,
            unlinked_user_count=len(users) - linked,
            totals=aggregate.schema(),
            models=aggregate.models(),
            user_totals=selected.user.schema(),
            service_totals=selected.service.schema(),
            sources=selected.source_models(),
            attributed_totals=attributed.schema(),
            unattributed_totals=unattributed.schema(),
            coverage=UsageCoverage(
                scope='gateway_ledger' if include_unattributed else 'attributed_users',
                includes_unattributed=include_unattributed,
                attributed_record_count=attributed.requests,
                unattributed_record_count=unattributed.requests,
            ),
            unattributed_categories=[
                UnattributedCategoryUsage(
                    category=category, totals=categories[category].schema()
                )
                for category in sorted(categories)
            ],
        ),
        sorted(user_items, key=lambda user: user.user_id),
        records,
        unallocated,
    )
