import pytest_asyncio
from datetime import date

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import settings
from app.core.db import get_db
from app.core.partner_security import digest_partner_api_key_secret, generate_partner_api_key, parse_partner_api_key
from app.modules.auth.router import router as auth_router, login_attempts
from app.modules.b2b import router as b2b, admin_router as incoming_invoices
from app.modules.b2b.models import (BusinessPartner, BuyerOrganization, IncomingInvoice,
                                   IncomingInvoiceHistory, IncomingInvoiceOutbox,
                                   PartnerApiKey, PartnerBankAccount, PartnerBuyerAccess)


@pytest_asyncio.fixture
async def partner_api(db_factory, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'b2b_enabled', True)
    monkeypatch.setattr(settings, 'b2b_api_key_pepper', 'test-pepper')
    monkeypatch.setattr(settings, 'b2b_storage_path', tmp_path / 'private')
    monkeypatch.setattr(settings, 'cookie_secure', False)
    login_attempts.clear()
    isolated = FastAPI()
    for router in (auth_router, b2b.router, incoming_invoices.router, incoming_invoices.admin_router):
        isolated.include_router(router)

    async def override():
        async with db_factory() as db:
            yield db
    isolated.dependency_overrides[get_db] = override
    raw_key = generate_partner_api_key()
    parsed = parse_partner_api_key(raw_key)
    async with db_factory() as db:
        partner = BusinessPartner(name='Supplier', inn='1234567890')
        buyer = BuyerOrganization(name='Buyer', inn='0987654321')
        db.add_all([partner, buyer])
        (await db.flush())
        account = PartnerBankAccount(partner_id=partner.id, account_number='40702810000000000001', bank_name='Bank', bank_bic='044525225', verified=True)
        db.add_all([account, PartnerBuyerAccess(partner_id=partner.id, buyer_id=buyer.id)])
        (await db.flush())
        db.add(PartnerApiKey(partner_id=partner.id, environment='test', prefix=parsed.prefix, secret_digest=digest_partner_api_key_secret(parsed.secret, settings.b2b_api_key_pepper), scopes=['invoices:read', 'invoices:write', 'documents:read', 'documents:write']))
        (await db.commit())
        payload = {'external_id': 'supplier-1', 'buyer_id': buyer.id, 'bank_account_id': account.id, 'number': 'INV-1', 'issued_at': str(date.today()), 'due_date': str(date.today()), 'title': 'Services', 'amount': '10.00', 'currency': 'RUB'}
    async with AsyncClient(transport=ASGITransport(app=isolated), base_url='http://testserver') as client:
        yield client, {'Authorization': f'Bearer {raw_key}', 'Idempotency-Key': 'create-1'}, payload


async def create_invoice(partner_api):
    client, headers, payload = partner_api
    response = (await client.post('/api/b2b/v1/invoices', json=payload, headers=headers))
    assert response.status_code == 201, response.text
    return response.json()


async def login(client, role='admin'):
    assert (await client.post('/api/v1/auth/login', json={'login': role, 'password': role + '12345'})).status_code == 200


@pytest.mark.asyncio
async def test_json_request_is_immediately_visible_payable_and_queued_without_pdf(partner_api, db_factory):
    client, _, _ = partner_api
    invoice = (await create_invoice(partner_api))
    assert invoice['review_status'] == 'submitted'
    assert invoice['revision'] == 1 and invoice['environment'] == 'test'
    async with db_factory() as db:
        row = (await db.get(IncomingInvoice, invoice['id']))
        assert row.supplier_snapshot['name'] == 'Supplier'
        assert row.bank_account_snapshot['account_number'] == '40702810000000000001'
        events = list((await db.scalars(select(IncomingInvoiceOutbox))))
        assert len(events) == 1 and events[0].event_type == 'incoming_invoice.submitted'
    (await login(client))
    result = (await client.get('/api/v1/incoming-invoices?page=1&page_size=12&environment=test')).json()
    assert result['total'] == 1 and result['items'][0]['supplier_name'] == 'Supplier'
    assert (await client.get('/api/v1/incoming-invoices?page=1&environment=live')).json()['total'] == 0
    detail = (await client.get(f"/api/v1/incoming-invoices/{invoice['id']}")).json()
    assert detail['documents'] == []
    response = (await client.post(f"/api/v1/incoming-invoices/{invoice['id']}/record-payment", json={'expected_revision': 1, 'amount': '10.00', 'paid_at': str(date.today()), 'reference': 'order-1'}))
    assert response.status_code == 200, response.text
    assert response.json()['payment_status'] == 'paid' and response.json()['review_status'] == 'submitted'
    detail = (await client.get(f"/api/v1/incoming-invoices/{invoice['id']}")).json()
    assert detail['payments'][0]['reference'] == 'order-1'
    reversed_payment = (await client.post(f"/api/v1/incoming-invoices/{invoice['id']}/reverse-payment", json={'expected_revision': 2, 'reason': 'Wrong payment reference'}))
    assert reversed_payment.status_code == 200 and reversed_payment.json()['payment_status'] == 'unpaid'


@pytest.mark.asyncio
async def test_replay_and_compatibility_submit_do_not_duplicate_events(partner_api, db_factory):
    client, headers, payload = partner_api
    invoice = (await create_invoice(partner_api))
    replay = (await client.post('/api/b2b/v1/invoices', json=payload, headers=headers))
    assert replay.status_code == 201 and replay.json() == invoice
    submitted = (await client.post(f"/api/b2b/v1/invoices/{invoice['id']}/submit", json={'expected_revision': 1}, headers={**headers, 'Idempotency-Key': 'submit-1'}))
    assert submitted.status_code == 200 and submitted.json()['revision'] == 1
    async with db_factory() as db:
        assert len(list((await db.scalars(select(IncomingInvoiceOutbox))))) == 1
        assert len(list((await db.scalars(select(IncomingInvoiceHistory))))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['approve', 'reject', 'cancel'])
async def test_administrator_cannot_moderate_invoices(partner_api, action):
    client, _, _ = partner_api
    invoice = (await create_invoice(partner_api))
    (await login(client))
    response = (await client.post(f"/api/v1/incoming-invoices/{invoice['id']}/{action}", json={'expected_revision': 1, 'reason': 'No moderation'}))
    assert response.status_code in {404, 405}
    assert (await client.get(f"/api/v1/incoming-invoices/{invoice['id']}")).json()['review_status'] == 'submitted'


@pytest.mark.asyncio
async def test_supplier_withdrawal_prevents_payment(partner_api):
    client, headers, _ = partner_api
    invoice = (await create_invoice(partner_api))
    cancelled = (await client.post(f"/api/b2b/v1/invoices/{invoice['id']}/cancel", json={'expected_revision': 1, 'reason': 'Supplier correction'}, headers={**headers, 'Idempotency-Key': 'cancel-1'}))
    assert cancelled.status_code == 200 and cancelled.json()['review_status'] == 'cancelled'
    (await login(client))
    response = (await client.post(f"/api/v1/incoming-invoices/{invoice['id']}/record-payment", json={'expected_revision': 2, 'amount': '10.00', 'paid_at': str(date.today()), 'reference': 'order'}))
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_stale_revision_and_wrong_amount_do_not_record_payment(partner_api):
    client, _, _ = partner_api
    invoice = (await create_invoice(partner_api))
    (await login(client))
    for revision, amount in ((2, '10.00'), (1, '9.00')):
        response = (await client.post(f"/api/v1/incoming-invoices/{invoice['id']}/record-payment", json={'expected_revision': revision, 'amount': amount, 'paid_at': str(date.today()), 'reference': 'order'}))
        assert response.status_code == 409


@pytest.mark.asyncio
async def test_registry_and_partner_configuration_require_admin(partner_api):
    client, _, _ = partner_api
    invoice = (await create_invoice(partner_api))
    for path in ['/incoming-invoices', '/business-partners', '/business-partners/buyers', f"/incoming-invoices/{invoice['id']}"]:
        assert (await client.get('/api/v1' + path)).status_code == 401
    (await login(client, 'user'))
    for path in ['/incoming-invoices', '/business-partners', '/business-partners/buyers', f"/incoming-invoices/{invoice['id']}"]:
        assert (await client.get('/api/v1' + path)).status_code == 403


@pytest.mark.asyncio
async def test_registry_search_and_partner_catalogue(partner_api):
    client, _, _ = partner_api
    (await create_invoice(partner_api))
    (await login(client))
    for q in ['Supplier', 'Buyer', 'Services', 'INV-1']:
        result = (await client.get('/api/v1/incoming-invoices', params={'page': 1, 'q': q})).json()
        assert result['total'] == 1
    assert (await client.get('/api/v1/incoming-invoices?page=1&q=missing')).json()['total'] == 0
    partners = (await client.get('/api/v1/business-partners')).json()['items']
    assert partners[0]['bank_accounts'][0]['verified'] and partners[0]['buyer_ids']
    assert (await client.get('/api/v1/business-partners/buyers')).json()['items'][0]['name'] == 'Buyer'


@pytest.mark.asyncio
async def test_paid_invoice_cannot_be_withdrawn(partner_api):
    client, headers, _ = partner_api
    invoice = (await create_invoice(partner_api))
    (await login(client))
    assert (await client.post(f"/api/v1/incoming-invoices/{invoice['id']}/record-payment", json={'expected_revision': 1, 'amount': '10.00', 'paid_at': str(date.today()), 'reference': 'order'})).status_code == 200
    response = (await client.post(f"/api/b2b/v1/invoices/{invoice['id']}/cancel", json={'expected_revision': 2, 'reason': 'Cannot withdraw'}, headers={**headers, 'Idempotency-Key': 'cancel-1'}))
    assert response.status_code == 409
