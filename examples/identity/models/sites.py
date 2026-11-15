"""Sites ORM contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .accounts import User
    from .permissions import Group
    from .violations import Violation

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from examples.identity.database import Base

from .associations import site_groups_table, user_sites_table
from .common import utc_now


class Site(Base):
    """Represents a construction site, including its name and associated users
    and safety violations.

    Attributes:
        id (int): Primary key.
        name (str): Name of the site.
        created_at (datetime): Creation timestamp.
        updated_at (datetime): Last update timestamp.
        users (list[User]): Users assigned to this site.
        violations (list[Violation]): Safety violations detected at the site.
    """

    __tablename__ = 'sites'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(
        String(80),
        unique=True,
        nullable=False,
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

    # Many-to-many relationship to Group
    groups: Mapped[list[Group]] = relationship(
        'Group',
        secondary=site_groups_table,
        back_populates='sites',
        lazy='joined',
    )

    # Many-to-many relationship to User
    # This is an association table linking users to sites
    users: Mapped[list[User]] = relationship(
        'User',
        secondary=user_sites_table,
        back_populates='sites',
    )

    notification_preferences: Mapped[list[SiteNotificationPreference]] = relationship(
        'SiteNotificationPreference',
        back_populates='site',
        cascade='all, delete-orphan',
    )

    # One-to-many relationship to Violation
    # This is a foreign key to the violations table
    violations: Mapped[list[Violation]] = relationship(
        'Violation',
        back_populates='site_obj',
    )

    stream_configs: Mapped[list[StreamConfig]] = relationship(
        'StreamConfig',
        back_populates='site',
        cascade='all, delete-orphan',
    )


class StreamConfig(Base):
    """Represents the configuration for a video stream, including detection
    capabilities and scheduling.

    Attributes:
        id (int): Primary key.
        group_id (int): Foreign key to the group_info table.
        site_id (int): Foreign key to the sites table.
        stream_name (str): Name of the video stream.
        video_url (str): URL of the video stream.
        model_key (str): Key for the detection model.
        detect_no_safety_vest_or_helmet (bool): Detection capability.
        detect_near_machinery_or_vehicle (bool): Detection capability.
        detect_in_restricted_area (bool): Detection capability.
        detect_in_utility_pole_restricted_area (bool): Detection capability.
        detect_machinery_close_to_pole (bool): Detection capability.
        recognition_enabled (bool): Whether recognition processing is enabled.
        expire_date (datetime | None): Expiration date for the configuration.
        work_start_hour (int): Start hour for work scheduling.
        work_end_hour (int): End hour for work scheduling.
        created_at (datetime): Timestamp of creation.
        updated_at (datetime): Timestamp of last update.
    """

    __tablename__ = 'stream_configs'

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    group_id: Mapped[int] = mapped_column(
        ForeignKey('group_info.id', ondelete='CASCADE'),
        nullable=False,
    )
    site_id: Mapped[int] = mapped_column(
        ForeignKey('sites.id', ondelete='CASCADE'),
        nullable=False,
    )

    stream_name: Mapped[str] = mapped_column(String(80), nullable=False)
    video_url: Mapped[str] = mapped_column(String(255), nullable=False)
    model_key: Mapped[str] = mapped_column(String(80), nullable=False)

    # Detection capabilities
    # These fields are used to determine the types of violations
    detect_no_safety_vest_or_helmet: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )
    detect_near_machinery_or_vehicle: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )
    detect_in_restricted_area: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )
    detect_in_utility_pole_restricted_area: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )
    detect_machinery_close_to_pole: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )

    recognition_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text('TRUE'),
        nullable=False,
    )
    expire_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    work_start_hour: Mapped[int] = mapped_column(Integer)
    work_end_hour: Mapped[int] = mapped_column(Integer)

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

    # Foreign key to the group_info table
    group: Mapped[Group] = relationship(
        'Group',
        back_populates='stream_configs',
    )
    site: Mapped[Site] = relationship(
        'Site',
        back_populates='stream_configs',
    )

    __table_args__ = (
        # Ensure that the combination of site_id and stream_name is unique
        # across the table to prevent duplicate stream configurations
        UniqueConstraint('site_id', 'stream_name', name='uq_sc_site_stream'),
    )


class SiteNotificationPreference(Base):
    """Per-user notification preference for a specific site."""

    __tablename__ = 'site_notification_preferences'

    user_id: Mapped[int] = mapped_column(
        ForeignKey('users.id', ondelete='CASCADE'),
        primary_key=True,
    )
    site_id: Mapped[int] = mapped_column(
        ForeignKey('sites.id', ondelete='CASCADE'),
        primary_key=True,
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )

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

    user: Mapped[User] = relationship(
        'User',
        back_populates='notification_preferences',
    )
    site: Mapped[Site] = relationship(
        'Site',
        back_populates='notification_preferences',
    )


class Notification(Base):
    """In-app notification record for notification center history."""

    __tablename__ = 'notifications'
    __table_args__ = (
        CheckConstraint(
            "type IN ('signature', 'violation', 'document', 'site_alert', 'system')",
            name='chk_notifications_type',
        ),
        Index(
            'idx_notifications_user_created_id',
            'user_id',
            text('created_at DESC'),
            text('id DESC'),
        ),
        Index(
            'idx_notifications_user_read_created_id',
            'user_id',
            'is_read',
            text('created_at DESC'),
            text('id DESC'),
        ),
        Index(
            'idx_notifications_user_type_created_id',
            'user_id',
            'type',
            text('created_at DESC'),
            text('id DESC'),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
    )
    type: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    deep_link: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_read: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    metadata_json: Mapped[dict[str, object]] = mapped_column(
        'metadata',
        JSON,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    user: Mapped[User] = relationship(
        'User',
        back_populates='notifications',
    )


class FcmDeviceToken(Base):
    """Push-notification device token stored as encrypted source of truth."""

    __tablename__ = 'fcm_device_tokens'
    __table_args__ = (
        UniqueConstraint(
            'device_token_hash',
            name='uq_fcm_device_tokens_token_hash',
        ),
        CheckConstraint(
            "platform IN ('android', 'ios', 'web', 'unknown')",
            name='chk_fcm_device_tokens_platform',
        ),
        Index(
            'idx_fcm_device_tokens_user_active',
            'user_id',
            'disabled_at',
        ),
        Index(
            'idx_fcm_device_tokens_user_seen',
            'user_id',
            text('last_seen_at DESC'),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
    )
    device_token_encrypted: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    device_token_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    platform: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default='unknown',
    )
    device_lang: Mapped[str] = mapped_column(String(20), nullable=False)
    permission_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default='unknown',
    )
    app_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    web_vapid_key_available: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
    )
    web_service_worker_registered: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_failure_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    failure_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    disabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
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

    user: Mapped[User] = relationship(
        'User',
        back_populates='fcm_device_tokens',
    )


class SiteMediaCleanupJob(Base):
    """Durable post-commit cleanup task for site-owned evidence files."""

    __tablename__ = 'site_media_cleanup_jobs'
    __table_args__ = (
        Index(
            'idx_site_media_cleanup_jobs_pending',
            'completed_at',
            'lease_expires_at',
            'id',
            postgresql_where=text('completed_at IS NULL'),
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    path: Mapped[str] = mapped_column(
        String(1024),
        nullable=False,
        unique=True,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
