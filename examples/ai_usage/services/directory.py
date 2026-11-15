"""Read the authoritative local directory without loading the auth service.

Queries are parameterised SELECTs so the dedicated database account can have
read-only grants. Groups have no tenant column: visibility is established via
their users' tenant, never by trusting a caller-selected group identifier.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from examples.ai_usage.schemas.admin_usage import UsageGroup
from examples.ai_usage.schemas.admin_usage import UsageUser
from examples.identity.permissions import is_super_admin


def can_view_complete_ledger(actor: UsageActor, tenant_id: str) -> bool:
    """Only the configured gateway tenant's super admin sees unallocated logs."""
    return actor.is_super_admin and bool(tenant_id) and str(actor.tenant_id) == tenant_id


@dataclass
class UsageActor:
    """Active, linked local caller and current permissions."""

    user_id: int
    username: str
    role: str
    tenant_id: object
    group_id: int | None

    @property
    def is_super_admin(self) -> bool:
        """Use the same policy as the existing management API."""
        return is_super_admin(self)

    @property
    def can_manage(self) -> bool:
        """Group administrators need a current group assignment."""
        return self.is_super_admin or (
            self.role == 'admin' and self.group_id is not None
        )


@dataclass(frozen=True)
class DirectoryUser:
    """Private mapping from a local display identity to LiteLLM ownership."""

    identity: UsageUser
    account_status: str
    end_user: str | None


async def load_actor(
    db: AsyncSession,
    *,
    subject: str,
    provider: str,
) -> UsageActor:
    """Resolve only explicitly linked, active accounts in active tenants."""
    result = await db.execute(
        text(
            'SELECT u.id, u.username, u.role, u.tenant_id, u.group_id '
            'FROM users u '
            'JOIN user_identities i ON i.user_id = u.id '
            'JOIN tenants t ON t.id = u.tenant_id '
            'WHERE i.provider = :provider AND i.provider_user_id = :subject '
            "AND u.status = 'active' AND t.status = 'active'",
        ),
        {'provider': provider, 'subject': subject},
    )
    row = result.mappings().one_or_none()
    if row is None:
        raise HTTPException(
            status_code=403,
            detail={'code': 'ai_usage_account_unavailable'},
        )
    return UsageActor(
        user_id=row['id'],
        username=row['username'],
        role=row['role'],
        tenant_id=row['tenant_id'],
        group_id=row['group_id'],
    )


async def list_visible_groups(
    db: AsyncSession,
    actor: UsageActor,
) -> list[UsageGroup]:
    """Return only groups with current members in the caller's tenant."""
    conditions = ['u.tenant_id = :tenant_id']
    params: dict[str, object] = {'tenant_id': actor.tenant_id}
    if not actor.is_super_admin:
        conditions.append('g.id = :group_id')
        params['group_id'] = actor.group_id
    result = await db.execute(
        text(
            'SELECT DISTINCT g.id, g.name FROM group_info g '
            'JOIN users u ON u.group_id = g.id WHERE '
            + ' AND '.join(conditions) + ' ORDER BY g.id',
        ),
        params,
    )
    return [
        UsageGroup(group_id=row['id'], group_name=row['name'])
        for row in result.mappings()
    ]


async def list_selected_users(
    db: AsyncSession,
    actor: UsageActor,
    *,
    provider: str,
    group_id: int | None,
    user_id: int | None,
) -> tuple[int | None, list[DirectoryUser]]:
    """Resolve and constrain selections before reading any accounting logs."""
    if not actor.can_manage:
        raise HTTPException(
            status_code=403,
            detail={'code': 'ai_usage_admin_required'},
        )
    if not actor.is_super_admin:
        if group_id is not None and group_id != actor.group_id:
            raise HTTPException(
                status_code=403,
                detail={'code': 'ai_usage_scope_forbidden'},
            )
        group_id = actor.group_id

    if group_id is not None:
        groups = await list_visible_groups(db, actor)
        if group_id not in {group.group_id for group in groups}:
            raise HTTPException(
                status_code=404,
                detail={'code': 'ai_usage_selection_not_found'},
            )

    conditions = ['u.tenant_id = :tenant_id']
    params: dict[str, object] = {
        'tenant_id': actor.tenant_id,
        'provider': provider,
    }
    if group_id is not None:
        conditions.append('u.group_id = :group_id')
        params['group_id'] = group_id
    if user_id is not None:
        conditions.append('u.id = :user_id')
        params['user_id'] = user_id
    result = await db.execute(
        text(
            'SELECT u.id, u.username, u.status, u.group_id, g.name AS group_name, '
            'p.family_name, p.middle_name, p.given_name, '
            'i.provider_user_id AS end_user '
            'FROM users u '
            'LEFT JOIN group_info g ON g.id = u.group_id '
            'LEFT JOIN user_profiles p ON p.user_id = u.id '
            'LEFT JOIN user_identities i ON i.user_id = u.id '
            'AND i.provider = :provider WHERE '
            + ' AND '.join(conditions) + ' ORDER BY u.id',
        ),
        params,
    )
    users = []
    for row in result.mappings():
        display_name = ''.join(
            row[key] or '' for key in ('family_name', 'middle_name', 'given_name')
        ).strip() or row['username']
        users.append(
            DirectoryUser(
                identity=UsageUser(
                    user_id=row['id'],
                    username=row['username'],
                    display_name=display_name,
                    group_id=row['group_id'],
                    group_name=row['group_name'],
                ),
                account_status=row['status'],
                end_user=row['end_user'] or None,
            ),
        )
    if user_id is not None and not users:
        # A nonexistent user and an out-of-scope user have the same response.
        raise HTTPException(
            status_code=404,
            detail={'code': 'ai_usage_selection_not_found'},
        )
    return group_id, users


async def foreign_subjects(db: AsyncSession, actor: UsageActor, provider: str) -> frozenset[str]:
    """Never reclassify another known tenant's user as an unallocated caller."""
    result = await db.execute(
        text(
            'SELECT i.provider_user_id FROM user_identities i '
            'JOIN users u ON u.id = i.user_id '
            'WHERE i.provider = :provider AND u.tenant_id <> :tenant_id',
        ),
        {'provider': provider, 'tenant_id': actor.tenant_id},
    )
    return frozenset(result.scalars())
