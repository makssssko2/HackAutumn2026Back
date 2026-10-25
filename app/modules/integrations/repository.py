from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import IntegrationConnection, IntegrationInbox


def list_connections(db: Session) -> list[IntegrationConnection]:
    return list(db.scalars(select(IntegrationConnection).order_by(IntegrationConnection.created_at.desc())))


def get_connection(db: Session, connection_id: str) -> IntegrationConnection | None:
    return db.get(IntegrationConnection, connection_id)


def claim_inbox_event(db: Session, connection_id: str, external_event_id: str, event_type: str, payload: dict) -> IntegrationInbox | None:
    existing = db.scalar(select(IntegrationInbox).where(
        IntegrationInbox.connection_id == connection_id,
        IntegrationInbox.external_event_id == external_event_id,
    ))
    if existing:
        return None
    row = IntegrationInbox(
        connection_id=connection_id,
        external_event_id=external_event_id,
        event_type=event_type,
        payload=payload,
    )
    db.add(row)
    db.flush()
    return row
