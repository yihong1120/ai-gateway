"""Accounts ORM contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .legal import UserConsent
    from .permissions import Group
    from .sites import FcmDeviceToken, Notification, Site, SiteNotificationPreference
    from .tenancy import Tenant

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from examples.identity.database import Base

from examples.identity.accounts.passwords import hash_password, verify_password
from .associations import user_sites_table
from .common import USER_STATUS_ACTIVE, utc_now


class UserProfile(Base):
    """Stores contact and profile details for a user account."""

    __tablename__ = 'user_profiles'

    user_id: Mapped[int] = mapped_column(
        ForeignKey('users.id', ondelete='CASCADE'),
        primary_key=True,
    )
    family_name: Mapped[str] = mapped_column(String(50), nullable=False)
    middle_name: Mapped[str | None] = mapped_column(String(50))
    given_name: Mapped[str] = mapped_column(String(50), nullable=False)
    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
    )
    mobile_number: Mapped[str | None] = mapped_column(String(20), unique=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text('CURRENT_TIMESTAMP'),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    # One-to-one relationship.
    user: Mapped[User] = relationship(
        'User',
        back_populates='profile',
        uselist=False,
    )


@event.listens_for(UserProfile.email, 'set', retval=True)
def _normalise_profile_email(
    _target: UserProfile,
    value: str | None,
    _oldvalue: object,
    _initiator: object,
) -> str | None:
    """Store e-mail addresses in one canonical form for indexed lookups."""
    return value.strip().lower() if isinstance(value, str) else value


class User(Base):
    """Represents a user in the system, including login credentials, role-based
    access, and relationships to assigned construction sites.

    Attributes:
        id (int): Primary key.
        username (str): Unique login identifier.
        password_hash (str): Hashed user password.
        role (str): Access level (e.g., admin, user, guest).
        status (str): Account lifecycle status.
        created_at (datetime): Timestamp of creation.
        updated_at (datetime): Timestamp of last update.
        sites (list[Site]): Sites the user has access to.
    """

    __tablename__ = 'users'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(
        String(80),
        unique=True,
        nullable=False,
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        String(20),
        default='user',
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default=USER_STATUS_ACTIVE,
        nullable=False,
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey('tenants.id', ondelete='RESTRICT'),
        nullable=False,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    # group_info.id
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey('group_info.id', ondelete='SET NULL'),
        nullable=True,
    )

    # One-to-many relationship to Group
    # This is a foreign key to the group_info table
    group: Mapped[Group | None] = relationship(
        'Group',
        back_populates='users',
        lazy='joined',
    )

    tenant: Mapped[Tenant] = relationship('Tenant', back_populates='users')

    # Many-to-many relationship to Site
    # This is an association table linking users to sites
    sites: Mapped[list[Site]] = relationship(
        'Site',
        secondary=user_sites_table,
        back_populates='users',
    )

    profile: Mapped[UserProfile] = relationship(
        'UserProfile',
        back_populates='user',
        uselist=False,
        lazy='selectin',
        cascade='all, delete-orphan',
        passive_deletes=True,
    )

    identities: Mapped[list[UserIdentity]] = relationship(
        'UserIdentity',
        back_populates='user',
        cascade='all, delete-orphan',
        passive_deletes=True,
    )

    notification_preferences: Mapped[list[SiteNotificationPreference]] = relationship(
        'SiteNotificationPreference',
        back_populates='user',
        cascade='all, delete-orphan',
    )

    notifications: Mapped[list[Notification]] = relationship(
        'Notification',
        back_populates='user',
        cascade='all, delete-orphan',
    )

    fcm_device_tokens: Mapped[list[FcmDeviceToken]] = relationship(
        'FcmDeviceToken',
        back_populates='user',
        cascade='all, delete-orphan',
    )

    consents: Mapped[list[UserConsent]] = relationship(
        'UserConsent',
        back_populates='user',
        cascade='all, delete-orphan',
    )

    def set_password(self, password: str) -> None:
        """Hash and store the user's password securely.

        Args:
            password (str): The plain-text password to be hashed.
        """
        self.password_hash = hash_password(password)

    async def check_password(self, password: str) -> bool:
        """Verify whether a given password matches the stored hash.

        This is executed in a thread-safe, asynchronous manner.

        Args:
            password (str): The plain-text password to verify.

        Returns:
            bool: True if the password matches, otherwise False.
        """
        return await verify_password(password, str(self.password_hash))

    def to_dict(self) -> dict:
        """Convert user attributes to a dictionary (e.g., for Redis caching).

        Returns:
            dict: A serialisable dictionary of user information.
        """
        return {
            'id': self.id,
            'username': self.username,
            'role': self.role,
            'status': self.status,
            'tenant_id': str(self.tenant_id),
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


class UserIdentity(Base):
    """External identity provider account linked to a local user."""

    __tablename__ = 'user_identities'
    __table_args__ = (
        UniqueConstraint(
            'provider',
            'provider_user_id',
            name='uq_user_identities_provider_user_id',
        ),
        UniqueConstraint(
            'user_id',
            'provider',
            name='uq_user_identities_user_provider',
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email_verified: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    display_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    raw_profile: Mapped[dict[str, object] | None] = mapped_column(
        JSON,
        nullable=True,
    )
    raw_email_is_private: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text('CURRENT_TIMESTAMP'),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    user: Mapped[User] = relationship(
        'User',
        back_populates='identities',
    )
