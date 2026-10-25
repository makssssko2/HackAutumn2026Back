import hashlib
import json
from datetime import timedelta

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as Session

from app.shared.model_utils import utcnow
from app.modules.b2b.models import PartnerIdempotencyRecord


def fingerprint(method: str, path: str, body: dict) -> str:
    payload = json.dumps({'method': method, 'path': path, 'body': body}, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


async def replay_or_conflict(db: Session, partner_id: str, environment: str, key: str, request_fingerprint: str) -> tuple[int, dict] | None:
    record = (await db.scalar(select(PartnerIdempotencyRecord).where(PartnerIdempotencyRecord.partner_id == partner_id, PartnerIdempotencyRecord.environment == environment, PartnerIdempotencyRecord.idempotency_key == key)))
    if record is None:
        return None
    if record.request_fingerprint != request_fingerprint:
        raise HTTPException(409, 'idempotency_conflict')
    return record.response_status, record.response_body


def save_response(db: Session, partner_id: str, environment: str, key: str, request_fingerprint: str, status: int, body: dict, ttl_days: int) -> None:
    db.add(PartnerIdempotencyRecord(partner_id=partner_id, environment=environment, idempotency_key=key, request_fingerprint=request_fingerprint, response_status=status, response_body=jsonable_encoder(body), expires_at=utcnow() + timedelta(days=ttl_days)))
