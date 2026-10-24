import logging
import time
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from app.core.db import SessionLocal
from app.integrations.actions import handle_callback
from app.integrations.base import IntegrationError, get_adapter
from app.models import IntegrationConnection, IntegrationInbox, utcnow

logger = logging.getLogger(__name__)


def _events(result: dict[str, Any]) -> list[dict[str, Any]]:
    events = result.get('events', [])
    return [event for event in events if isinstance(event, dict)] if isinstance(events, list) else []


def _event_id(event: dict[str, Any]) -> str:
    value = event.get('eventId') or event.get('id')
    return str(value) if isinstance(value, (str, int)) else ''


def poll_bitrix24() -> None:
    with SessionLocal() as db:
        connections = list(db.scalars(select(IntegrationConnection).where(
            IntegrationConnection.provider == 'bitrix24', IntegrationConnection.active.is_(True),
            IntegrationConnection.setup_status == 'ready', IntegrationConnection.external_bot_id.is_not(None),
        )))
        for connection in connections:
            _poll_connection(db, connection)


def _poll_connection(db, connection: IntegrationConnection) -> None:
    adapter = get_adapter(connection)
    offset = connection.server_revision
    try:
        while True:
            page = adapter.poll(connection, offset)
            for event in _events(page):
                external_id = _event_id(event)
                if not external_id:
                    continue
                inbox = db.scalar(select(IntegrationInbox).where(
                    IntegrationInbox.connection_id == connection.id,
                    IntegrationInbox.external_event_id == external_id,
                ))
                if inbox is None:
                    inbox = IntegrationInbox(
                        connection_id=connection.id,
                        external_event_id=external_id,
                        event_type=str(event.get('type') or 'unknown'),
                        payload=event,
                    )
                    db.add(inbox)
                    try:
                        if event.get('type') == 'ONIMBOTV2COMMANDADD':
                            result = handle_callback(db, connection, adapter.parse_callback(connection, event))
                            adapter.acknowledge_command(connection, event, str(result.get('text', '')))
                            inbox.result = result
                        else:
                            inbox.result = {'ignored': True}
                    except HTTPException as error:
                        inbox.result = {'error': str(error.detail)}
                    except IntegrationError as error:
                        inbox.result = {'error': error.code}
                    inbox.processed_at = utcnow()
                    connection.last_event_at = utcnow()
                    db.flush()
            next_offset = page.get('nextOffset')
            if isinstance(next_offset, (str, int)):
                connection.server_revision = str(next_offset)
            connection.health_status = 'healthy'
            connection.last_error_code = None
            db.commit()
            if not page.get('hasMore'):
                break
            offset = connection.server_revision
            time.sleep(2)
    except (IntegrationError, ValueError, httpx.HTTPError) as error:
        db.rollback()
        connection.health_status = 'error'
        connection.last_error_code = error.code if isinstance(error, IntegrationError) else type(error).__name__
        db.commit()
        logger.warning('Bitrix24 polling failed for %s (%s)', connection.id, type(error).__name__)
