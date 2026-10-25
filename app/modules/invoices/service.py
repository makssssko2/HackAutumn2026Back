from app.core.config import settings
from app.modules.invoices.view_links import get_or_create_view_link


def invoice_view_url(db, occurrence_id: str):
    if not settings.public_invoice_view_enabled:
        return None
    link = get_or_create_view_link(db, occurrence_id)
    if link.revoked_at is not None:
        return None
    return settings.public_frontend_url + '/invoice/' + link.token
