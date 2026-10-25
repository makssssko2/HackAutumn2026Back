from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as Session

from app.modules.integrations.models import IntegrationConnection, IntegrationInbox


async def list_connections(db: Session) -> list[IntegrationConnection]:
    return list((await db.scalars(select(IntegrationConnection).order_by(IntegrationConnection.created_at.desc()))))


async def get_connection(db: Session, connection_id: str) -> IntegrationConnection | None:
    return (await db.get(IntegrationConnection, connection_id))


async def claim_inbox_event(db: Session, connection_id: str, external_event_id: str, event_type: str, payload: dict) -> IntegrationInbox | None:
    existing = (await db.scalar(select(IntegrationInbox).where(
        IntegrationInbox.connection_id == connection_id,
        IntegrationInbox.external_event_id == external_event_id,
    )))
    if existing:
        return None
    row = IntegrationInbox(
        connection_id=connection_id,
        external_event_id=external_event_id,
        event_type=event_type,
        payload=payload,
    )
    db.add(row)
    (await db.flush())
    return row
