"""Permissions ORM contracts."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .accounts import User
    from .sites import Site, StreamConfig

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from examples.identity.database import Base

from .associations import group_features_table
from .common import utc_now


class Feature(Base):
    """Represents a feature in the system, such as safety detection
    capabilities. This model is linked to the Group model through a many-to-
    many relationship.

    Attributes:
        id (int): Primary key.
        feature_name (str): Unique name of the feature.
        description (str | None): Description of the feature.
        created_at (datetime): Timestamp of creation.
        updated_at (datetime): Timestamp of last update.
        groups (list[Group]): Groups that have access to this feature.

    Methods:
        __repr__(): String representation of the Feature object.
    """

    __tablename__ = 'features'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feature_name: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

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

    # Many-to-many relationship to Group
    # related to this feature
    groups: Mapped[list[Group]] = relationship(
        'Group',
        secondary='group_features',
        back_populates='features',
        lazy='joined',
    )

    def __repr__(self) -> str:
        """Perform repr.

        Returns:
            The callable result.
        """
        return f'<Feature id={self.id} name={self.feature_name}>'


class Group(Base):
    """Represents a group of users, including their access permissions and
    associated construction sites.

    Attributes:
        id (int): Primary key.
        name (str): Name of the group.
        uniform_number (str): Unique identifier for the group.
        max_allowed_streams (int): Maximum number of streams allowed.
        created_at (datetime): Timestamp of creation.
        updated_at (datetime): Timestamp of last update.
        sites (list[Site]): Sites associated with this group.
        users (list[User]): Users belonging to this group.
        features (list[Feature]): Features available to this group.

    Methods:
        __repr__(): String representation of the Group object.
    """

    __tablename__ = 'group_info'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    uniform_number: Mapped[str] = mapped_column(
        String(8),
        unique=True,
        nullable=False,
        comment='統一編號',
    )

    max_allowed_streams: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=8,
    )

    sites: Mapped[list[Site]] = relationship(
        'Site',
        secondary='site_groups',
        back_populates='groups',
        lazy='joined',
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

    # One-to-many relationship to User
    users: Mapped[list[User]] = relationship(
        'User',
        back_populates='group',
    )

    # Many-to-many relationship to Feature
    features: Mapped[list[Feature]] = relationship(
        'Feature',
        secondary=group_features_table,
        back_populates='groups',
        lazy='joined',
    )

    stream_configs: Mapped[list[StreamConfig]] = relationship(
        'StreamConfig',
        back_populates='group',
        cascade='all, delete-orphan',
    )

    def __repr__(self) -> str:
        """Perform repr.

        Returns:
            The callable result.
        """
        return f'<Group id={self.id} name={self.name}>'
