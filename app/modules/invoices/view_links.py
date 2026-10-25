import re
import secrets

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.models import InvoiceViewLink, Occurrence, utcnow


def get_or_create_view_link(db, occurrence_id: str):
    link = db.scalar(select(InvoiceViewLink).where(InvoiceViewLink.occurrence_id == occurrence_id))
    if link:
        return link
    insert = pg_insert if db.bind.dialect.name == 'postgresql' else sqlite_insert
    db.execute(insert(InvoiceViewLink).values(
        token='iv_' + secrets.token_urlsafe(32), occurrence_id=occurrence_id, created_at=utcnow(),
    ).on_conflict_do_nothing(index_elements=['occurrence_id']))
    return db.scalar(select(InvoiceViewLink).where(InvoiceViewLink.occurrence_id == occurrence_id))


def get_view_occurrence(db, token: str):
    if not re.fullmatch(r'iv_[A-Za-z0-9_-]{43}', token):
        return None
    return db.scalar(select(Occurrence).join(InvoiceViewLink, InvoiceViewLink.occurrence_id == Occurrence.id).where(
        InvoiceViewLink.token == token, InvoiceViewLink.revoked_at.is_(None),
    ))


def revoke_view_link(db, occurrence_id: str):
    """Service operation; callers own the transaction. Never silently re-publish."""
    db.execute(update(InvoiceViewLink).where(
        InvoiceViewLink.occurrence_id == occurrence_id, InvoiceViewLink.revoked_at.is_(None),
    ).values(revoked_at=utcnow()))
