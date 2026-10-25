from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class OIDCProvider(Base):
    __tablename__ = 'oidc_provider'
    __table_args__ = (CheckConstraint('id = 1', name='ck_oidc_provider_singleton'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    name: Mapped[str] = mapped_column(String(120), default='Корпоративный вход')
    discovery_url: Mapped[str] = mapped_column(String(2048), default='')
    issuer: Mapped[str] = mapped_column(String(2048), default='')
    client_id: Mapped[str] = mapped_column(String(255), default='')
    credentials_encrypted: Mapped[str] = mapped_column(Text, default='')
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_provision: Mapped[bool] = mapped_column(Boolean, default=False)
    session_minutes: Mapped[int] = mapped_column(Integer, default=60)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class OIDCPendingLogin(Base):
    __tablename__ = 'oidc_pending_logins'
    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    browser_hash: Mapped[str] = mapped_column(String(64))
    nonce: Mapped[str] = mapped_column(String(128))
    verifier: Mapped[str] = mapped_column(String(128))
    revision: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    native_redirect_uri: Mapped[str | None] = mapped_column(String(128), nullable=True)
    native_challenge: Mapped[str | None] = mapped_column(String(43), nullable=True)
    native_state: Mapped[str | None] = mapped_column(String(128), nullable=True)


class OIDCHandoff(Base):
    __tablename__ = 'oidc_handoffs'
    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'))
    revision: Mapped[int] = mapped_column(Integer)
    redirect_uri: Mapped[str] = mapped_column(String(128))
    challenge: Mapped[str] = mapped_column(String(43))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class OIDCIdentity(Base):
    __tablename__ = 'oidc_identities'
    issuer: Mapped[str] = mapped_column(String(2048), primary_key=True)
    subject: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), unique=True)
