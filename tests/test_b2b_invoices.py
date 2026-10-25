from datetime import date

from fastapi.testclient import TestClient

from app.modules.b2b import router as b2b, admin_router as incoming_invoices
from app.core.config import settings
from app.core.partner_security import digest_partner_api_key_secret, generate_partner_api_key, parse_partner_api_key
from app.main import app
from app.modules.b2b.models import (BusinessPartner, BuyerOrganization, PartnerApiKey,
                            PartnerBankAccount, PartnerBuyerAccess)


def test_legacy_partner_can_attach_optional_document_and_repeat_submit(db_factory, tmp_path, monkeypatch):
    # Given: a B2B-enabled partner with access to one buyer and bank account.
    monkeypatch.setattr(settings, 'b2b_enabled', True)
    monkeypatch.setattr(settings, 'b2b_api_key_pepper', 'test-pepper')
    monkeypatch.setattr(settings, 'b2b_storage_path', tmp_path / 'private')
    app.include_router(b2b.router)
    app.include_router(incoming_invoices.router)
    app.include_router(incoming_invoices.admin_router)
    raw_key = generate_partner_api_key()
    parsed = parse_partner_api_key(raw_key)
    with db_factory() as db:
        partner = BusinessPartner(name='Supplier', inn='1234567890')
        buyer = BuyerOrganization(name='Buyer', inn='0987654321')
        db.add_all([partner, buyer])
        db.flush()
        account = PartnerBankAccount(partner_id=partner.id, account_number='40702810000000000001', bank_name='Bank', bank_bic='044525225', verified=True)
        db.add_all([account, PartnerBuyerAccess(partner_id=partner.id, buyer_id=buyer.id)])
        db.flush()
        db.add(PartnerApiKey(partner_id=partner.id, environment='test', prefix=parsed.prefix, secret_digest=digest_partner_api_key_secret(parsed.secret, settings.b2b_api_key_pepper), scopes=['invoices:read', 'invoices:write', 'documents:read', 'documents:write']))
        db.commit()
        buyer_id, account_id = buyer.id, account.id

    def override():
        with db_factory() as db:
            yield db

    from app.core.db import get_db
    app.dependency_overrides[get_db] = override
    client = TestClient(app)
    headers = {'Authorization': f'Bearer {raw_key}', 'Idempotency-Key': 'create-1'}
    payload = {'external_id': 'supplier-1', 'buyer_id': buyer_id, 'bank_account_id': account_id, 'number': '1', 'issued_at': str(date.today()), 'due_date': str(date.today()), 'title': 'Services', 'amount': '10.00', 'currency': 'RUB'}

    # When: an existing integration creates a JSON invoice, attaches an optional
    # PDF, then calls the compatibility submit endpoint.
    created = client.post('/api/b2b/v1/invoices', json=payload, headers=headers)
    assert created.status_code == 201
    invoice = created.json()
    assert invoice['review_status'] == 'submitted'
    uploaded = client.post(f"/api/b2b/v1/invoices/{invoice['id']}/documents", data={'expected_revision': '1', 'document_type': 'invoice'}, files={'file': ('invoice.pdf', b'%PDF-1.4\ncontent', 'application/pdf')}, headers={'Authorization': f'Bearer {raw_key}', 'Idempotency-Key': 'upload-1'})
    assert uploaded.status_code == 201
    submitted = client.post(f"/api/b2b/v1/invoices/{invoice['id']}/submit", json={'expected_revision': 2}, headers={'Authorization': f'Bearer {raw_key}', 'Idempotency-Key': 'submit-1'})

    # Then: the invoice was already submitted by creation, and remains accessible.
    assert submitted.status_code == 200
    assert submitted.json()['review_status'] == 'submitted'
    assert client.get(f"/api/b2b/v1/invoices/{invoice['id']}/documents/{uploaded.json()['id']}/download", headers={'Authorization': f'Bearer {raw_key}'}).content.startswith(b'%PDF-')
    client.close()
    app.dependency_overrides.clear()
