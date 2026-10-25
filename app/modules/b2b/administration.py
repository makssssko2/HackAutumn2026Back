from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as Session
from app.modules.b2b.presentation import invoice_out
from app.modules.b2b.security import digest_partner_api_key_secret, generate_partner_api_key, parse_partner_api_key
from app.core.config import settings
from app.modules.b2b.service import record_payment, reverse_payment
from app.modules.audit.service import audit
from app.modules.users.models import User
from app.shared.model_utils import utcnow
from app.modules.b2b.models import (BusinessPartner, BuyerOrganization, PartnerApiKey, PartnerBuyerAccess, IncomingInvoiceNotificationRecipient)
from app.modules.b2b.repository import invoice_or_404
from app.modules.b2b.schemas import (AccessIn, BuyerIn, KeyIssueIn,
                             NotificationRecipientIn, PartnerIn, PaymentIn, RevisionIn)


async def add_payment(invoice_id: str, data: PaymentIn, user: User, db: Session):
    invoice = (await invoice_or_404(db, invoice_id, for_update=True))
    (await record_payment(db, invoice, data.expected_revision, data.amount, data.paid_at, data.reference, user.id))
    audit(db, user.login, 'incoming_invoice.payment_recorded', invoice.id)
    (await db.commit())
    return invoice_out(invoice)


async def undo_payment(invoice_id: str, data: RevisionIn, user: User, db: Session):
    if not data.reason or not data.reason.strip():
        raise HTTPException(422, 'reason is required')
    invoice = (await invoice_or_404(db, invoice_id, for_update=True))
    (await reverse_payment(db, invoice, data.expected_revision, data.reason, user.id))
    audit(db, user.login, 'incoming_invoice.payment_reversed', invoice.id)
    (await db.commit())
    return invoice_out(invoice)


async def create_partner(data: PartnerIn, user: User, db: Session):
    if (await db.scalar(select(BusinessPartner.id).where(BusinessPartner.inn == data.inn, BusinessPartner.kpp == data.kpp))):
        raise HTTPException(409, 'Поставщик с таким ИНН и КПП уже существует')
    row = BusinessPartner(**data.model_dump())
    db.add(row)
    (await db.flush())
    audit(db, user.login, 'business_partner.created', row.id)
    (await db.commit())
    return {'id': row.id, 'name': row.name, 'status': row.status}


async def create_buyer(data: BuyerIn, user: User, db: Session):
    if (await db.scalar(select(BuyerOrganization.id).where(BuyerOrganization.inn == data.inn, BuyerOrganization.kpp == data.kpp))):
        raise HTTPException(409, 'Покупатель с таким ИНН и КПП уже существует')
    row = BuyerOrganization(**data.model_dump())
    db.add(row)
    (await db.flush())
    audit(db, user.login, 'buyer_organization.created', row.id)
    (await db.commit())
    return {'id': row.id, 'name': row.name, 'active': row.active}


async def create_notification_recipient(buyer_id: str, data: NotificationRecipientIn, user: User, db: Session):
    if (await db.get(BuyerOrganization, buyer_id)) is None:
        raise HTTPException(404, 'Buyer not found')
    recipient = IncomingInvoiceNotificationRecipient(buyer_id=buyer_id, **data.model_dump())
    db.add(recipient)
    (await db.flush())
    audit(db, user.login, 'incoming_invoice_notification_recipient.created', recipient.id)
    (await db.commit())
    return {'id': recipient.id, 'buyer_id': recipient.buyer_id, 'environment': recipient.environment, 'destination_type': recipient.destination_type, 'destination_id': recipient.destination_id, 'active': recipient.active}


async def grant_buyer_access(partner_id: str, data: AccessIn, user: User, db: Session):
    if (await db.get(BusinessPartner, partner_id)) is None or (await db.get(BuyerOrganization, data.buyer_id)) is None:
        raise HTTPException(404, 'Partner or buyer not found')
    existing = (await db.scalar(select(PartnerBuyerAccess).where(PartnerBuyerAccess.partner_id == partner_id, PartnerBuyerAccess.buyer_id == data.buyer_id)))
    if existing:
        return {'id': existing.id, 'partner_id': existing.partner_id, 'buyer_id': existing.buyer_id}
    row = PartnerBuyerAccess(partner_id=partner_id, buyer_id=data.buyer_id)
    db.add(row)
    (await db.flush())
    audit(db, user.login, 'business_partner.buyer_access_granted', row.id)
    (await db.commit())
    return {'id': row.id, 'partner_id': row.partner_id, 'buyer_id': row.buyer_id}


async def issue_key(partner_id: str, data: KeyIssueIn, user: User, db: Session):
    if (await db.get(BusinessPartner, partner_id)) is None:
        raise HTTPException(404, 'Partner not found')
    raw_key = generate_partner_api_key()
    parsed = parse_partner_api_key(raw_key)
    row = PartnerApiKey(partner_id=partner_id, environment=data.environment, prefix=parsed.prefix, secret_digest=digest_partner_api_key_secret(parsed.secret, settings.b2b_api_key_pepper), scopes=sorted(data.scopes), expires_at=data.expires_at)
    db.add(row)
    (await db.flush())
    audit(db, user.login, 'business_partner.api_key_issued', row.id)
    (await db.commit())
    return {'id': row.id, 'prefix': row.prefix, 'environment': row.environment, 'scopes': row.scopes, 'expires_at': row.expires_at, 'api_key': raw_key}


async def revoke_key(partner_id: str, key_id: str, user: User, db: Session):
    row = (await db.scalar(select(PartnerApiKey).where(PartnerApiKey.id == key_id, PartnerApiKey.partner_id == partner_id)))
    if row is None:
        raise HTTPException(404, 'API key not found')
    row.revoked_at = utcnow()
    audit(db, user.login, 'business_partner.api_key_revoked', row.id)
    (await db.commit())
    return {'ok': True}
