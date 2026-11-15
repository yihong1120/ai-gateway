from __future__ import annotations

from typing import Annotated
from typing import cast

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import examples.identity.runtime as _runtime_imports
from examples.identity.accounts.access import invalidate_effective_site_cache
from examples.identity.accounts.management import approve_signup_user
from examples.identity.accounts.management import ensure_user_management_scope
from examples.identity.accounts.management import get_group_or_404
from examples.identity.accounts.management import list_all_users_for_operator
from examples.identity.accounts.management import list_users_for_operator
from examples.identity.accounts.management import load_user_read
from examples.identity.accounts.management import pending_user_review_read
from examples.identity.accounts.management import register_signup_user
from examples.identity.accounts.management import resolve_target_group_id
from examples.identity.accounts.password_policy import validate_password_minimum
from examples.identity.accounts.users import create_oidc_managed_user
from examples.identity.accounts.users import create_or_update_profile
from examples.identity.accounts.users import create_user
from examples.identity.accounts.users import delete_user
from examples.identity.accounts.users import ensure_keycloak_subject_for_user
from examples.identity.accounts.users import get_user_by_id
from examples.identity.accounts.users import set_user_status
from examples.identity.accounts.users import update_password
from examples.identity.accounts.users import update_username
from examples.identity.authorization.sites import list_site_ids_for_group
from examples.identity.authorization.sites import seed_site_notification_preferences
from examples.identity.database import get_db
from examples.identity.dependencies import _NamedRoleUser
from examples.identity.dependencies import ensure_not_super
from examples.identity.dependencies import get_current_user
from examples.identity.dependencies import is_super_admin
from examples.identity.dependencies import require_admin
from examples.identity.models import User
from examples.identity.models import USER_STATUS_PENDING_ADMIN_APPROVAL
from examples.identity.models import USER_STATUS_REJECTED
from examples.identity.providers.keycloak_users import delete_keycloak_user
from examples.identity.providers.keycloak_users import provision_keycloak_user
from examples.identity.providers.keycloak_users import reset_keycloak_password
from examples.identity.providers.keycloak_users import set_keycloak_user_enabled
from examples.identity.providers.keycloak_users import update_keycloak_user
from examples.identity.providers.mode import require_local_password_management
from examples.identity.redis_pool import get_redis_pool
from examples.identity.runtime import get_settings
from examples.identity.schemas.user import AdminUserApproval
from examples.identity.schemas.user import ApproveUserSignup
from examples.identity.schemas.user import PendingUserReviewPage
from examples.identity.schemas.user import SetUserStatus
from examples.identity.schemas.user import UpdateMyPassword
from examples.identity.schemas.user import UpdatePassword
from examples.identity.schemas.user import UpdatePasswordById
from examples.identity.schemas.user import UpdateUserGroup
from examples.identity.schemas.user import UpdateUsername
from examples.identity.schemas.user import UpdateUsernameById
from examples.identity.schemas.user import UpdateUserRole
from examples.identity.schemas.user import UserCreate
from examples.identity.schemas.user import UserDelete
from examples.identity.schemas.user import UserManagementCapabilities
from examples.identity.schemas.user import UserPage
from examples.identity.schemas.user import UserProfileUpdate
from examples.identity.schemas.user import UserRead
from examples.identity.schemas.user import UserSignup
# Token revocation is independent of the profile snapshot.

router = APIRouter(tags=['user-mgmt'])


def _keycloak_manages_passwords() -> bool:
    """Return whether administrator actions must update Keycloak identities."""
    settings = get_settings()
    return (
        settings.oidc_enabled
        and settings.oidc_passwords_managed_externally
    )


def _ensure_role_can_be_assigned(role: str, operator: User) -> None:
    """Keep the singleton platform super-admin role out of normal workflows."""
    if role == 'super_admin':
        raise HTTPException(403, 'The super admin role is reserved.')
    if role == 'admin' and not is_super_admin(cast(_NamedRoleUser, operator)):
        raise HTTPException(403, 'Only super admin can assign admin role.')


@router.post(
    '/add_user',
    response_model=UserRead,
    dependencies=[Depends(require_admin)],
)
async def add_user(
    payload: UserCreate,
    db: AsyncSession = Depends(get_db),
    me: User = Depends(get_current_user),
) -> UserRead:
    """Create a new user.

    Args:
        payload: Data for the new user.
        db: Async database session.
        me: The currently authenticated admin user.

    Returns:
        Newly created user's details.
    """
    target_group_id = resolve_target_group_id(
        payload.group_id,
        me,
        default_to_operator_group=True,
    )
    _ensure_role_can_be_assigned(payload.role, me)

    if _keycloak_manages_passwords():
        if payload.profile is None:
            raise HTTPException(
                400,
                'A profile with email and name is required for Keycloak.',
            )
        validate_password_minimum(payload.password)
        profile = payload.profile.model_dump()
        subject = await provision_keycloak_user(
            username=payload.username,
            password=payload.password,
            email=str(profile['email']),
            given_name=str(profile['given_name']),
            family_name=str(profile['family_name']),
            force_password_change=payload.force_password_change,
        )
        try:
            new_user = await create_oidc_managed_user(
                username=payload.username,
                role=payload.role,
                group_id=target_group_id,
                tenant_id=me.tenant_id,
                keycloak_subject=subject,
                profile=profile,
                db=db,
            )
        except HTTPException:
            # Keycloak was provisioned first so a failed local transaction
            # must be compensated before returning control to the operator.
            await delete_keycloak_user(subject, suppress_errors=True)
            raise
    else:
        new_user = await create_user(
            username=payload.username,
            password=payload.password,
            role=payload.role,
            group_id=target_group_id,
            db=db,
            tenant_id=getattr(me, 'tenant_id', None),
            profile=payload.profile.model_dump() if payload.profile else None,
        )

    if target_group_id:
        site_ids = await list_site_ids_for_group(target_group_id, db)
        await seed_site_notification_preferences(
            user_ids=[new_user.id],
            site_ids=site_ids,
            db=db,
        )
        if site_ids:
            await db.commit()
    invalidate_effective_site_cache()
    return await load_user_read(new_user.id, db)


@router.post('/signup', response_model=UserRead, status_code=201)
async def signup_user(
    payload: UserSignup,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis_pool),
) -> UserRead:
    """Create an email-unverified account for the public signup flow.

    Args:
        payload: Registration details, profile, and legal consents.
        request: HTTP request used to capture consent audit metadata.
        db: Database session used to create the account.
        redis: Redis connection used to create verification-token state.

    Returns:
        Newly created account in its email-unverified state.

    Raises:
        HTTPException: If the username or email is unavailable, legal consent
            is
            invalid, or verification delivery cannot be initiated.
    """
    return await register_signup_user(payload, request, db, redis)


@router.get(
    '/list_pending_users',
    response_model=PendingUserReviewPage,
    dependencies=[Depends(require_admin)],
)
async def list_pending_users(
    db: AsyncSession = Depends(get_db),
    cursor: Annotated[int | None, Query(ge=0)] = None,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> PendingUserReviewPage:
    """List email-verified signups awaiting administrator approval.

    Args:
        db: Database session used to load pending accounts and their relations.

    Returns:
        Pending account records suitable for an administrator review queue.
    """
    query = (
        select(User)
        .options(
            selectinload(User.group),
            selectinload(User.profile),
            selectinload(User.consents),
            selectinload(User.identities),
        )
        .where(
            User.role == 'user',
            User.status == USER_STATUS_PENDING_ADMIN_APPROVAL,
            User.email_verified_at.is_not(None),
            User.group_id.is_(None),
        )
    )
    if cursor is not None:
        query = query.where(User.id > cursor)
    result = await db.execute(query.order_by(User.id).limit(page_size + 1))
    users = list(result.scalars().all())
    has_more = len(users) > page_size
    page = users[:page_size]
    return PendingUserReviewPage(
        items=[pending_user_review_read(user) for user in page],
        next_cursor=page[-1].id if has_more and page else None,
    )


@router.put('/approve_user_signup', response_model=UserRead)
async def approve_user_signup(
    payload: ApproveUserSignup,
    db: AsyncSession = Depends(get_db),
    me: User = Depends(require_admin),
) -> UserRead:
    """Approve a pending signup by assigning a group and activating it.

    Args:
        payload: Pending user and optional group assignment to approve.
        db: Database session used to update the pending account.
        me: Authenticated administrator performing the approval.

    Returns:
        Activated user details.

    Raises:
        HTTPException: If the account is not awaiting approval or is outside
            the
            administrator's scope.
    """
    user = await get_user_by_id(payload.user_id, db)
    ensure_not_super(user)

    if (
        user.role != 'user'
        or user.status != USER_STATUS_PENDING_ADMIN_APPROVAL
        or user.email_verified_at is None
        or user.group_id is not None
    ):
        raise HTTPException(400, 'User is not awaiting signup approval.')

    return await approve_signup_user(user, payload.group_id, db, me)


@router.patch('/admin/users/{user_id}/approval', response_model=UserRead)
async def review_user_signup(
    user_id: int,
    payload: AdminUserApproval,
    db: AsyncSession = Depends(get_db),
    me: User = Depends(require_admin),
) -> UserRead:
    """Approve or reject an email-verified signup request.

    Args:
        user_id: Identifier of the pending account to review.
        payload: Approval decision, optional group, and audit note.
        db: Database session used to update the pending account.
        me: Authenticated administrator performing the review.

    Returns:
        Updated user details after the decision is recorded.

    Raises:
        HTTPException: If the account is not awaiting approval or is outside
            the
            administrator's scope.
    """
    user = await get_user_by_id(user_id, db)
    ensure_not_super(user)

    if (
        user.role != 'user'
        or user.status != USER_STATUS_PENDING_ADMIN_APPROVAL
        or user.email_verified_at is None
        or user.group_id is not None
    ):
        raise HTTPException(400, 'User is not awaiting signup approval.')

    if payload.decision == 'rejected':
        user.status = USER_STATUS_REJECTED
        await db.commit()
        invalidate_effective_site_cache()
        return await load_user_read(user.id, db)

    return await approve_signup_user(user, payload.group_id, db, me)


@router.get(
    '/list_users',
    response_model=list[UserRead],
)
async def list_users(
    db: AsyncSession = Depends(get_db),
    me: User = Depends(require_admin),
) -> list[UserRead]:
    """Return the scoped user array required by deployed clients.

    Pagination lives at ``/admin/users`` so this established endpoint never
    changes its JSON top-level type.
    """
    return await list_all_users_for_operator(me, db)


@router.get('/admin/users', response_model=UserPage)
async def list_users_page(
    db: AsyncSession = Depends(get_db),
    me: User = Depends(require_admin),
    cursor: Annotated[int | None, Query(ge=0)] = None,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> UserPage:
    """Return one cursor-based, administrator-scoped user page.

    New management clients should use this endpoint and preserve
    ``next_cursor`` when loading later pages.
    """
    return await list_users_for_operator(
        me,
        db,
        cursor=cursor,
        page_size=page_size,
    )


@router.get(
    '/admin/capabilities',
    response_model=UserManagementCapabilities,
)
async def user_management_capabilities(
    me: User = Depends(require_admin),
) -> UserManagementCapabilities:
    """Return UI capabilities without trusting client-side role inference.

    The backend still authorises every mutation independently.  This endpoint
    merely prevents Flutter from showing ChangDar or a group administrator a
    misleading read-only management screen.
    """
    super_admin = is_super_admin(cast(_NamedRoleUser, me))
    return UserManagementCapabilities(
        scope='all_groups' if super_admin else 'own_group',
        managed_group_id=None if super_admin else me.group_id,
        can_create_users=True,
        can_reset_passwords=True,
        can_suspend_users=True,
        can_delete_users=True,
        can_manage_groups=super_admin,
        can_manage_group_features=super_admin,
        can_assign_group_admins=super_admin,
    )


@router.delete('/delete_user', dependencies=[Depends(require_admin)])
async def remove_user(
    payload: UserDelete,
    db: AsyncSession = Depends(get_db),
    me: User = Depends(require_admin),
) -> dict[str, str]:
    """Delete a user by user ID.

    Args:
        payload: Dictionary containing 'user_id'.
        db: Async database session.

    Returns:
        Confirmation message.
    """
    user = await get_user_by_id(payload.user_id, db)
    ensure_not_super(user)
    ensure_user_management_scope(user, me)
    if _keycloak_manages_passwords():
        subject = await ensure_keycloak_subject_for_user(user, db)
        await delete_keycloak_user(subject)
    await delete_user(user, db)
    invalidate_effective_site_cache()
    return {'message': 'User deleted successfully.'}


@router.put('/admin_update_password', dependencies=[Depends(require_admin)])
async def admin_update_pwd(
    payload: UpdatePassword,
    db: AsyncSession = Depends(get_db),
    me: User = Depends(require_admin),
) -> dict[str, str]:
    """Admin update user's password by username.

    Args:
        payload: Contains username and new password.
        db: Async database session.

    Returns:
        Confirmation message.
    """
    user = (
        await db.execute(select(User).where(User.username == payload.username))
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(404, 'User not found.')
    ensure_not_super(user)
    ensure_user_management_scope(user, me)
    if _keycloak_manages_passwords():
        validate_password_minimum(payload.new_password)
        subject = await ensure_keycloak_subject_for_user(user, db)
        await reset_keycloak_password(
            subject,
            password=payload.new_password,
            temporary=True,
        )
        await db.commit()
    else:
        require_local_password_management()
        await update_password(user, payload.new_password, db)
    return {'message': 'Password updated successfully.'}


@router.put(
    '/admin_update_password_userid',
    dependencies=[Depends(require_admin)],
)
async def admin_update_pwd_by_id(
    payload: UpdatePasswordById,
    db: AsyncSession = Depends(get_db),
    me: User = Depends(require_admin),
) -> dict[str, str]:
    """Admin update user's password by user ID.

    Args:
        payload: Contains user_id and new password.
        db: Async database session.

    Returns:
        Confirmation message.
    """
    user = await get_user_by_id(payload.user_id, db)
    ensure_not_super(user)
    ensure_user_management_scope(user, me)
    if _keycloak_manages_passwords():
        validate_password_minimum(payload.new_password)
        subject = await ensure_keycloak_subject_for_user(user, db)
        await reset_keycloak_password(
            subject,
            password=payload.new_password,
            temporary=True,
        )
        await db.commit()
    else:
        require_local_password_management()
        await update_password(user, payload.new_password, db)
    return {'message': 'Password updated successfully by user ID.'}


@router.put('/update_my_password', dependencies=[Depends(get_current_user)])
async def update_my_pwd(
    payload: UpdateMyPassword,
    db: AsyncSession = Depends(get_db),
    redis_pool: Redis = Depends(get_redis_pool),
    me: User = Depends(get_current_user),
) -> dict[str, str]:
    """Allow users to update their own password.

    Args:
        payload: Contains old and new passwords.
        db: Async database session.
        redis_pool: Redis connection.
        me: Currently authenticated user.

    Returns:
        Message indicating password change success.
    """
    require_local_password_management()
    if not await me.check_password(payload.old_password):
        raise HTTPException(401, 'Old password incorrect.')

    await update_password(me, payload.new_password, db)
    await _runtime_imports.get_identity().local_sessions.revoke_all(redis_pool, me.username)

    return {'message': 'Password changed successfully, please log in again.'}


@router.put('/update_username', dependencies=[Depends(require_admin)])
async def change_username(
    payload: UpdateUsername,
    db: AsyncSession = Depends(get_db),
    me: User = Depends(require_admin),
) -> dict[str, str]:
    """Change username by providing old and new usernames.

    Args:
        payload: Old and new usernames.
        db: Async database session.

    Returns:
        Confirmation message.
    """
    user = (
        await db.execute(
            select(User).where(User.username == payload.old_username),
        )
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(404, 'User not found.')
    ensure_not_super(user)
    ensure_user_management_scope(user, me)
    if _keycloak_manages_passwords():
        subject = await ensure_keycloak_subject_for_user(user, db)
        await update_keycloak_user(subject, username=payload.new_username)
    await update_username(user, payload.new_username, db)
    return {'message': 'Username updated successfully.'}


@router.put('/update_username_id', dependencies=[Depends(require_admin)])
async def change_username_by_id(
    payload: UpdateUsernameById,
    db: AsyncSession = Depends(get_db),
    me: User = Depends(require_admin),
) -> dict[str, str]:
    """Change username by user ID.

    Args:
        payload: User ID and new username.
        db: Async database session.

    Returns:
        Confirmation message.
    """
    user = await get_user_by_id(payload.user_id, db)
    ensure_not_super(user)
    ensure_user_management_scope(user, me)
    if _keycloak_manages_passwords():
        subject = await ensure_keycloak_subject_for_user(user, db)
        await update_keycloak_user(subject, username=payload.new_username)
    await update_username(user, payload.new_username, db)
    return {'message': 'Username updated successfully.'}


@router.put('/set_user_status', dependencies=[Depends(require_admin)])
async def update_user_status(
    payload: SetUserStatus,
    db: AsyncSession = Depends(get_db),
    me: User = Depends(require_admin),
) -> dict[str, str]:
    """Set a user's account status.

    Args:
        payload: User ID and target status.
        db: Async database session.

    Returns:
        Confirmation message.
    """
    user = await get_user_by_id(payload.user_id, db)
    ensure_not_super(user)
    ensure_user_management_scope(user, me)
    if _keycloak_manages_passwords():
        subject = await ensure_keycloak_subject_for_user(user, db)
        await set_keycloak_user_enabled(
            subject,
            enabled=payload.status == 'active',
        )
    await set_user_status(user, payload.status, db)
    return {'message': 'User status updated successfully.'}


@router.put('/update_user_role', dependencies=[Depends(require_admin)])
async def change_role(
    payload: UpdateUserRole,
    db: AsyncSession = Depends(get_db),
    me: User = Depends(require_admin),
) -> dict[str, str]:
    """Update a user's role (admin or user).

    Args:
        payload: User ID and new role.
        db: Async database session.
        me: Currently authenticated admin.

    Returns:
        Confirmation message.
    """
    user = await get_user_by_id(payload.user_id, db)
    ensure_not_super(user)
    ensure_user_management_scope(user, me)
    _ensure_role_can_be_assigned(payload.new_role, me)

    user.role = payload.new_role
    await db.commit()
    return {'message': 'User role updated successfully.'}


@router.put('/update_user_group', dependencies=[Depends(require_admin)])
async def change_group(
    payload: UpdateUserGroup,
    db: AsyncSession = Depends(get_db),
    me: User = Depends(require_admin),
) -> dict[str, str]:
    """Update the user's group membership.

    Args:
        payload: User ID and new group ID.
        db: Async database session.

    Returns:
        Confirmation message.
    """
    user = await get_user_by_id(payload.user_id, db)
    ensure_not_super(user)
    ensure_user_management_scope(user, me)
    target_group_id = resolve_target_group_id(payload.new_group_id, me)
    if target_group_id is None:
        raise HTTPException(400, 'group_id is required.')
    await get_group_or_404(target_group_id, db)
    user.group_id = target_group_id
    await db.commit()
    invalidate_effective_site_cache()
    return {'message': 'User group updated successfully.'}


@router.put('/update_user_profile', dependencies=[Depends(require_admin)])
async def update_profile(
    payload: UserProfileUpdate,
    db: AsyncSession = Depends(get_db),
    me: User = Depends(require_admin),
) -> dict[str, str]:
    """Update contact-profile details for a user.

    Args:
        payload: Partial profile fields for the target user.
        db: Database session used to persist the profile.
        me: Authenticated administrator performing the update.

    Returns:
        Confirmation message after the profile is updated.

    Raises:
        HTTPException: If the user is unavailable, protected, or outside the
            administrator's management scope.
    """
    user = await get_user_by_id(payload.user_id, db)
    ensure_not_super(user)
    ensure_user_management_scope(user, me)
    profile_data = payload.model_dump(
        exclude={'user_id'},
        exclude_none=True,
    )
    if _keycloak_manages_passwords() and profile_data:
        subject = await ensure_keycloak_subject_for_user(user, db)
        await update_keycloak_user(
            subject,
            email=(
                str(profile_data['email'])
                if 'email' in profile_data
                else None
            ),
            given_name=(
                str(profile_data['given_name'])
                if 'given_name' in profile_data
                else None
            ),
            family_name=(
                str(profile_data['family_name'])
                if 'family_name' in profile_data
                else None
            ),
        )
    await create_or_update_profile(
        user,
        data=profile_data,
        db=db,
    )
    return {'message': 'User profile updated successfully.'}
