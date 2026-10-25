"""Compatibility exports; definitions are owned by their domain modules."""

from app.modules.auth.schemas import LoginIn, PasswordChange
from app.modules.users.schemas import ProfilePatch, UserIn, UserPatch
from app.modules.payments.schemas import RecurringIn, OneTimeIn
from app.modules.invoices.schemas import OccurrencePatch, PublicInvoiceView
from app.modules.channels.schemas import ChannelIn
from app.modules.integrations.schemas import IntegrationConnectionIn, IntegrationConnectionPatch
from app.modules.groups.schemas import GroupIn
from app.modules.attachments.schemas import LinkIn

__all__ = ['LoginIn', 'PasswordChange', 'ProfilePatch', 'UserIn', 'UserPatch', 'RecurringIn', 'OneTimeIn', 'OccurrencePatch', 'PublicInvoiceView', 'ChannelIn', 'IntegrationConnectionIn', 'IntegrationConnectionPatch', 'GroupIn', 'LinkIn']
