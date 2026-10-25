"""Compatibility exports; definitions are owned by their domain modules."""

from app.modules.users.models import User
from app.modules.auth.models import AuthSession
from app.modules.payments.models import RecurringPayment
from app.modules.invoices.models import Occurrence, PaymentLink, InvoiceViewLink
from app.modules.receipts.models import Receipt
from app.modules.attachments.models import Attachment
from app.modules.channels.models import Channel, ChannelLink
from app.modules.groups.models import AudienceGroup
from app.modules.notifications.models import Delivery
from app.modules.audit.models import AuditLog
from app.modules.integrations.models import BotAction, IntegrationConnection, IntegrationInbox
from app.core.db import Base
from app.shared.model_utils import uid, utcnow

import app.modules.b2b.models  # Register the complete ORM metadata for legacy callers.

__all__ = ['User', 'AuthSession', 'RecurringPayment', 'Occurrence', 'PaymentLink', 'InvoiceViewLink', 'Receipt', 'Attachment', 'Channel', 'ChannelLink', 'AudienceGroup', 'Delivery', 'AuditLog', 'BotAction', 'IntegrationConnection', 'IntegrationInbox', 'Base', 'uid', 'utcnow']
