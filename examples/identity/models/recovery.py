"""Account-action tokens are consumed in the transaction updating the account."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean
from sqlalchemy import CheckConstraint
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import text
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from examples.identity.database import Base


class AccountActionToken(Base):
    __tablename__ = 'account_action_tokens'
    __table_args__ = (
        CheckConstraint("purpose IN ('verify_email','reset_password')"),
        CheckConstraint("delivery IN ('prepared','sent','rejected','unknown')"),
        Index('account_action_tokens_expiry', 'expires_at'),
        Index(
            'account_action_tokens_revocations',
            'created_at',
            postgresql_where=text('revocation_pending'),
        ),
        Index(
            'account_action_tokens_active',
            'user_id',
            'purpose',
            unique=True,
            postgresql_where=text('consumed_at IS NULL'),
        ),
    )
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False,
    )
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text('CURRENT_TIMESTAMP'),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery: Mapped[str] = mapped_column(
        String(16), nullable=False, default='prepared',
    )
    revocation_pending: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text('FALSE'),
    )
