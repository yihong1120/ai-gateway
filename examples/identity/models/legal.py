"""Legal ORM contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .accounts import User

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from examples.identity.database import Base

from .common import utc_now


class LegalDocument(Base):
    """Versioned legal document content shown to users during signup."""

    __tablename__ = 'legal_documents'
    __table_args__ = (
        UniqueConstraint(
            'type',
            'version',
            'locale',
            name='uq_legal_documents_type_version_locale',
        ),
        CheckConstraint(
            "type IN ('terms', 'privacy', 'ai_terms')",
            name='chk_legal_documents_type',
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    locale: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default='zh-TW',
        index=True,
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class UserConsent(Base):
    """Recorded user consent snapshot for legal and notification terms."""

    __tablename__ = 'user_consents'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    terms_version: Mapped[str] = mapped_column(String(40), nullable=False)
    privacy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    ai_terms_version: Mapped[str] = mapped_column(String(40), nullable=False)
    accepted_terms: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    notification_consent: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    ai_terms_accepted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        index=True,
    )
    ai_terms_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    notification_consent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    ip_address: Mapped[str | None] = mapped_column(
        String(45),
        nullable=True,
    )
    user_agent: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    user: Mapped[User] = relationship('User', back_populates='consents')
