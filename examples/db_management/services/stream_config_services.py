from __future__ import annotations

from datetime import datetime
from datetime import timezone

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from examples.db_management.schemas.stream_config import SiteStreamConfigUpsert
from examples.db_management.schemas.stream_config import StreamConfigCreate
from examples.db_management.schemas.stream_config import StreamConfigRead
from examples.db_management.schemas.stream_config import StreamConfigUpdate
from examples.identity.dependencies import _site_permission
from examples.identity.dependencies import ensure_admin_with_group
from examples.identity.dependencies import is_super_admin
from examples.identity.models import Group
from examples.identity.models import Site
from examples.identity.models import StreamConfig
from examples.identity.models import User


async def _get_site_or_404(site_id: int, db: AsyncSession) -> Site:
    """Load a site or raise a not-found response.

    Args:
        site_id: Identifier of the site to load.
        db: Database session used to query the site.

    Returns:
        Loaded site.

    Raises:
        HTTPException: If no site has the supplied identifier.
    """
    site = await db.get(Site, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail='Site not found.')
    return site


def _site_group_ids(site: Site) -> set[int]:
    """Return identifiers of groups assigned to a site.

    Args:
        site: Site whose group membership is inspected.

    Returns:
        Set of assigned group identifiers.
    """
    return {group.id for group in site.groups}


def _primary_site_group_id(site: Site) -> int:
    """Return the deterministic primary group identifier for a site.

    Args:
        site: Site whose configured groups are inspected.

    Returns:
        Lowest assigned group identifier.

    Raises:
        HTTPException: If the site is not assigned to any group.
    """
    group_ids = sorted(_site_group_ids(site))
    if not group_ids:
        raise HTTPException(
            status_code=400,
            detail='Site must have a group before configuring streams.',
        )
    return group_ids[0]


def _resolve_stream_group_id(
    site: Site,
    me: User,
    requested_group_id: int | None = None,
) -> int:
    """Resolve the group authorised to own a stream-configuration change.

    Args:
        site: Site whose group membership constrains the change.
        me: Authenticated administrator performing the change.
        requested_group_id: Optional group requested by a super administrator.

    Returns:
        Authorised group identifier for the configuration.

    Raises:
        HTTPException: If the selected group is not assigned to the site.
    """
    if is_super_admin(me):
        group_id = requested_group_id or _primary_site_group_id(site)
    else:
        ensure_admin_with_group(me)
        group_id = me.group_id
    if group_id not in _site_group_ids(site):
        raise HTTPException(
            status_code=403,
            detail='Group is not associated with this site.',
        )
    return group_id


def _stream_config_to_read(
    cfg: StreamConfig,
    limits: tuple[int, int],
) -> StreamConfigRead:
    """Serialise only response fields, using the already loaded group limits."""
    current, maximum = limits
    return StreamConfigRead(
        **{
            name: getattr(cfg, name) for name in StreamConfigRead.model_fields
            if name not in {'total_stream_in_group', 'max_allowed_streams'}
        },
        total_stream_in_group=current,
        max_allowed_streams=maximum,
    )


async def _list_site_stream_config_reads(
    site_id: int,
    db: AsyncSession,
    me: User,
) -> list[StreamConfigRead]:
    """Return stream configurations visible to the requesting administrator.

    Args:
        site_id: Identifier of the site whose configurations are requested.
        db: Database session used to load site and stream data.
        me: Authenticated user whose site and group scope is enforced.

    Returns:
        Response models for configurations visible within the user's scope.

    Raises:
        HTTPException: If the site is missing or outside the user's scope.
    """
    site = await _get_site_or_404(site_id, db)
    _site_permission(me, site=site)
    visible_group_id = None
    if not is_super_admin(me):
        ensure_admin_with_group(me)
        visible_group_id = me.group_id
    configs = await list_stream_configs(site_id, db, group_id=visible_group_id)
    limits = await _group_stream_limits({cfg.group_id for cfg in configs}, db)
    return [_stream_config_to_read(cfg, limits[cfg.group_id]) for cfg in configs]


async def list_stream_configs(
    site_id: int,
    db: AsyncSession,
    group_id: int | None = None,
) -> list[StreamConfig]:
    """Retrieve a list of StreamConfig objects associated with a specific site.

    Args:
        site_id (int): The ID of the site.
        db (AsyncSession): The asynchronous database session.

    Returns:
        List[StreamConfig]: A list of StreamConfig instances.
    """
    query = select(StreamConfig).where(StreamConfig.site_id == site_id)
    if group_id is not None:
        query = query.where(StreamConfig.group_id == group_id)

    result = await db.execute(query)
    return list(result.scalars().all())


async def _lock_stream_groups(group_ids: set[int], db: AsyncSession) -> None:
    # Every capacity-reserving writer locks its groups before changing streams.
    # ID ordering also covers super-admin batches spanning multiple groups.
    await db.execute(
        select(Group.id).where(Group.id.in_(group_ids))
        .order_by(Group.id).with_for_update(),
    )


async def _require_stream_capacity(group_id: int, additions: int, db: AsyncSession) -> None:
    current, limit = await get_group_stream_limit(group_id, db)
    if current + additions > limit:
        raise HTTPException(status_code=403, detail='Stream limit reached for group.')


async def create_stream_config(
    payload: StreamConfigCreate,
    db: AsyncSession,
) -> StreamConfig:
    """Reserve group capacity and insert one stream in the same transaction."""
    try:
        group_id = payload.group_id
        if group_id is None:
            raise HTTPException(status_code=404, detail='Group not found')
        await _lock_stream_groups({group_id}, db)
        await _require_stream_capacity(group_id, 1, db)
        cfg = StreamConfig(**payload.model_dump())
        db.add(cfg)
        await db.flush()
        await db.commit()
        return cfg
    except HTTPException:
        await db.rollback()
        raise
    except Exception as error:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f'Database error: {error}') from error


async def upsert_site_stream_configs(
    site_id: int,
    payload: SiteStreamConfigUpsert,
    db: AsyncSession,
    *,
    group_id: int,
    visible_group_id: int | None,
) -> None:
    """Validate and commit the authorised site's upserts under ordered group locks.

    Existing items keep their group. New items consume the selected group's
    capacity; omitted items are retained, matching the existing HTTP operation.
    """
    try:
        existing_ids = {item.id for item in payload.streams if item.id is not None}
        group_ids = {group_id}
        if existing_ids:
            groups = select(StreamConfig.group_id).where(
                StreamConfig.site_id == site_id, StreamConfig.id.in_(existing_ids),
            )
            if visible_group_id is not None:
                groups = groups.where(StreamConfig.group_id == visible_group_id)
            group_ids.update((await db.scalars(groups)).all())
        await _lock_stream_groups(group_ids, db)

        names = [item.stream_name for item in payload.streams]
        if len(names) != len(set(names)):
            raise HTTPException(status_code=400, detail='Duplicate stream names are not allowed.')
        # Re-read after waiting for the locks; an earlier writer may have
        # changed names or values in this session's identity map meanwhile.
        site_configs = list((
            await db.scalars(
                select(StreamConfig).where(StreamConfig.site_id == site_id)
                .execution_options(populate_existing=True),
            )
        ).all())
        existing = {
            cfg.id: cfg for cfg in site_configs
            if visible_group_id is None or cfg.group_id == visible_group_id
        }
        owners = {cfg.stream_name: cfg.id for cfg in site_configs}
        for item in payload.streams:
            if item.id is not None and item.id not in existing:
                raise HTTPException(status_code=404, detail='Stream configuration not found.')
            owner_id = owners.get(item.stream_name)
            if owner_id is not None and owner_id != item.id:
                raise HTTPException(status_code=400, detail='Stream name already exists in site.')

        additions = sum(item.id is None for item in payload.streams)
        if additions:
            await _require_stream_capacity(group_id, additions, db)
        for item in payload.streams:
            values = item.model_dump(exclude={'id'})
            if item.id is None:
                db.add(StreamConfig(site_id=site_id, group_id=group_id, **values))
            else:
                for name, value in values.items():
                    setattr(existing[item.id], name, value)
        await db.flush()
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except IntegrityError as error:
        await db.rollback()
        raise HTTPException(status_code=409, detail='Stream configuration update conflicted.') from error
    except Exception as error:
        await db.rollback()
        raise HTTPException(status_code=500, detail='Unable to update stream configurations.') from error


async def update_stream_config(
    cfg: StreamConfig,
    updates: StreamConfigUpdate,
    db: AsyncSession,
) -> None:
    """Update an existing StreamConfig object with new data.

    Args:
        cfg (StreamConfig): The existing StreamConfig instance to update.
        updates (StreamConfigUpdate): A dictionary containing updated values.
        db (AsyncSession): The asynchronous database session.

    Raises:
        HTTPException: If a database error occurs during updating.
    """
    # Apply updates to the configuration object
    for key, value in updates.model_dump(exclude_unset=True).items():
        setattr(cfg, key, value)

    # Update the timestamp to the current time
    cfg.updated_at = datetime.now(timezone.utc)

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


async def delete_stream_config(
    cfg: StreamConfig,
    db: AsyncSession,
) -> None:
    """Delete an existing StreamConfig from the database.

    Args:
        cfg (StreamConfig): The StreamConfig instance to delete.
        db (AsyncSession): The asynchronous database session.

    Raises:
        HTTPException: If a database error occurs during deletion.
    """
    await db.delete(cfg)

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


async def _group_stream_limits(
    group_ids: set[int],
    db: AsyncSession,
) -> dict[int, tuple[int, int]]:
    """Load group-wide counts and configured limits in one query."""
    if not group_ids:
        return {}
    rows = await db.execute(
        select(Group.id, func.count(StreamConfig.id), Group.max_allowed_streams)
        .outerjoin(StreamConfig, StreamConfig.group_id == Group.id)
        .where(Group.id.in_(group_ids))
        .group_by(Group.id, Group.max_allowed_streams),
    )
    limits = {identifier: (current, maximum) for identifier, current, maximum in rows}
    if group_ids - limits.keys():
        raise HTTPException(status_code=404, detail='Group not found')
    return limits


async def get_group_stream_limit(
    group_id: int,
    db: AsyncSession,
) -> tuple[int, int]:
    """Return the current count and capacity for one existing group."""
    return (await _group_stream_limits({group_id}, db))[group_id]
