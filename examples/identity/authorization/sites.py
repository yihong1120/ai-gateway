"""Account/group site entitlements and notification defaults on the shared metadata."""
from __future__ import annotations

from typing import Final

from sqlalchemy import column
from sqlalchemy import Integer
from sqlalchemy import literal
from sqlalchemy import values
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from examples.identity.models import site_groups_table
from examples.identity.models import SiteNotificationPreference

_bulk_insert_chunk_size: Final[int] = 250


def _chunks(values_: list[int], size: int) -> list[list[int]]:
    """Split identifiers into bounded SQL VALUES inputs."""
    return [
        values_[start: start + size] for start in range(0, len(values_), size)
    ]


async def list_site_ids_for_group(
    group_id: int,
    db: AsyncSession,
) -> list[int]:
    """Load all site identifiers linked to a group.

    Args:
        group_id: Identifier of the group.
        db: Database session used to query site membership.

    Returns:
        Site identifiers accessible to the group.
    """
    result = await db.execute(
        select(site_groups_table.c.site_id).where(
            site_groups_table.c.group_id == group_id,
        ),
    )
    return list(result.scalars().all())


async def seed_site_notification_preferences(
    user_ids: list[int],
    site_ids: list[int],
    db: AsyncSession,
) -> None:
    """Insert default-enabled notification preferences in bulk.

    Args:
        user_ids: Users that should receive default preferences.
        site_ids: Sites for which preferences should be created.
        db: Database session used for bulk insertion.
    """
    if not user_ids or not site_ids:
        return

    # Let PostgreSQL construct the Cartesian product.  Materialising it in
    # Python used O(users * sites) application memory before any SQL ran.
    unique_user_ids = list(dict.fromkeys(user_ids))
    unique_site_ids = list(dict.fromkeys(site_ids))
    for user_chunk in _chunks(unique_user_ids, _bulk_insert_chunk_size):
        user_values = values(
            column('user_id', Integer),
            name='notification_preference_user_ids',
        ).data([(user_id,) for user_id in user_chunk])
        for site_chunk in _chunks(
            unique_site_ids,
            _bulk_insert_chunk_size,
        ):
            site_values = values(
                column('site_id', Integer),
                name='notification_preference_site_ids',
            ).data([(site_id,) for site_id in site_chunk])
            rows = select(
                user_values.c.user_id,
                site_values.c.site_id,
                literal(True),
            )
            await db.execute(
                pg_insert(SiteNotificationPreference)
                .from_select(['user_id', 'site_id', 'is_enabled'], rows)
                .on_conflict_do_nothing(
                    index_elements=['user_id', 'site_id'],
                ),
            )
