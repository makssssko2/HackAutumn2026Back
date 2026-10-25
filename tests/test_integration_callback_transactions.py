from datetime import date, timedelta
from decimal import Decimal
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.config import settings
from app.integrations import actions, bitrix24_polling
from app.integrations.base import Callback
from app.integrations.bitrix24 import Bitrix24Adapter
from app.integrations.vk_teams import VkTeamsAdapter
from app.modules.integrations.models import BotAction, IntegrationConnection, IntegrationInbox
from app.modules.channels.models import Channel
from app.modules.notifications.models import Delivery
from app.modules.invoices.models import Occurrence
from app.modules.payments.models import RecurringPayment
from app.modules.receipts.models import Receipt
from app.modules.notifications import service as notifications
from app.shared.model_utils import utcnow

pytestmark = pytest.mark.asyncio


async def prepare_callbacks(db_factory, provider):
    connection_ids, cases = [], []
    async with db_factory() as db:
        for index in range(2):
            connection = IntegrationConnection(provider=provider, name=f'Connection {index}', base_url='https://example.invalid', credentials_encrypted='unused', setup_status='ready', external_bot_id='bot', public_config={}, capabilities={})
            db.add(connection)
            await db.flush()
            connection_ids.append(connection.id)
            channel = Channel(name='chat', kind=provider, connection_id=connection.id, remote_chat_id='chat', config={})
            db.add(channel)
            await db.flush()
            for event_id in (('1', '2') if index == 0 else ('3',)):
                payment = RecurringPayment(title='Invoice', amount=Decimal('10'), currency='RUB', first_due_date=date.today())
                db.add(payment)
                await db.flush()
                occurrence = Occurrence(recurring_id=payment.id, title='Invoice', amount=Decimal('10'), currency='RUB', due_date=date.today(), cancelled=index == 0)
                db.add(occurrence)
                await db.flush()
                delivery = Delivery(occurrence_id=occurrence.id, channel_id=channel.id, event='created')
                db.add(delivery)
                await db.flush()
                token = f'token-{event_id}'
                db.add(BotAction(token=token, occurrence_id=occurrence.id, chat_id='chat', user_id='*', mode='pay', revision=occurrence.revision, expires_at=utcnow() + timedelta(hours=1), connection_id=connection.id, channel_id=channel.id, provider=provider, message_id='message', delivery_id=delivery.id))
                if provider == 'bitrix24':
                    event = {'eventId': event_id, 'type': 'ONIMBOTV2COMMANDADD', 'botId': 'bot', 'command': {'command': 'confirm', 'context': 'keyboard', 'params': json.dumps({'token': token, 'mode': 'pay'})}, 'message': {'id': 'message'}, 'chat': {'dialogId': 'chat'}, 'user': {'id': 'user'}}
                elif provider == 'mattermost':
                    event = {'context': {'token': token}, 'channel_id': 'chat', 'user_id': 'user', 'post_id': 'message'}
                else:
                    event = {'eventId': event_id, 'type': 'callbackQuery', 'payload': {'callbackData': token, 'chat': {'chatId': 'chat'}, 'from': {'userId': 'user'}, 'message': {'msgId': 'message'}}}
                cases.append((connection.id, token, occurrence.id, event))
        await db.commit()
    return connection_ids, cases


@pytest.mark.parametrize('mode', ['vk_poll', 'bitrix_poll', 'vk_http', 'mattermost_http'])
async def test_callback_conflicts_do_not_abort_following_events_or_connections(db_factory, client, monkeypatch, mode):
    provider = 'bitrix24' if mode == 'bitrix_poll' else 'mattermost' if mode == 'mattermost_http' else 'vk_teams'
    connection_ids, cases = await prepare_callbacks(db_factory, provider)
    polled = []
    if mode.endswith('poll'):
        async def poll(adapter, connection, *args, **kwargs):
            polled.append(connection.id)
            events = [case[3] for case in cases if case[0] == connection.id]
            return {'events': events, 'nextOffset': events[-1]['eventId'], 'hasMore': False} if provider == 'bitrix24' else events
        if provider == 'bitrix24':
            monkeypatch.setattr(Bitrix24Adapter, 'poll', poll)
            async def acknowledge(*args):
                pass
            monkeypatch.setattr(Bitrix24Adapter, 'acknowledge_command', acknowledge)
            monkeypatch.setattr(bitrix24_polling, 'SessionLocal', db_factory)
            await bitrix24_polling.poll_bitrix24()
        else:
            monkeypatch.setattr(VkTeamsAdapter, 'poll', poll)
            monkeypatch.setattr(notifications, 'SessionLocal', db_factory)
            await notifications.poll_vk_teams()
        assert set(polled) == set(connection_ids)
    else:
        for index, (connection_id, token, occurrence_id, event) in enumerate(cases):
            path = f'/api/v1/integrations/mattermost/{connection_id}/actions' if provider == 'mattermost' else f'/api/v1/integrations/vk-teams/{connection_id}/events'
            response = await client.post(path, json=event)
            assert response.status_code == (409 if provider == 'mattermost' and index < 2 else 200)
    async with db_factory() as db:
        for index, (connection_id, token, occurrence_id, event) in enumerate(cases):
            action = await db.get(BotAction, token)
            occurrence = await db.get(Occurrence, occurrence_id)
            assert occurrence.paid is (index == 2)
            assert (action.consumed_at is not None) is (index == 2)
            receipts = await db.scalar(select(func.count()).select_from(Receipt).where(Receipt.occurrence_id == occurrence_id))
            assert receipts == (1 if index == 2 else 0)
        if provider != 'mattermost':
            inbox = list(await db.scalars(select(IntegrationInbox).order_by(IntegrationInbox.external_event_id)))
            assert len(inbox) == 3
            assert all(row.processed_at is not None for row in inbox)
            assert 'error' in inbox[0].result and 'error' in inbox[1].result
            assert inbox[2].result['ok'] is True
            assert (await db.get(IntegrationConnection, connection_ids[0])).server_revision == '2'
            assert (await db.get(IntegrationConnection, connection_ids[1])).server_revision == '3'


async def test_callback_changes_remain_owned_by_caller_transaction(db_factory):
    connection_ids, cases = await prepare_callbacks(db_factory, 'mattermost')
    connection_id, token, occurrence_id, _ = cases[-1]
    async with db_factory() as db:
        connection = await db.get(IntegrationConnection, connection_id)
        await actions.handle_callback(db, connection, Callback(token, 'chat', 'user', 'message'))
        await db.rollback()
    async with db_factory() as db:
        assert not (await db.get(Occurrence, occurrence_id)).paid
        assert (await db.get(BotAction, token)).consumed_at is None
        assert await db.scalar(select(func.count()).select_from(Receipt).where(Receipt.occurrence_id == occurrence_id)) == 0


async def test_callback_failure_rolls_back_partial_payment_but_preserves_inbox(db_factory, monkeypatch):
    connection_ids, cases = await prepare_callbacks(db_factory, 'mattermost')
    connection_id, token, occurrence_id, _ = cases[-1]
    confirm = actions.confirm_occurrence
    async def reject_after_payment(*args, **kwargs):
        await confirm(*args, **kwargs)
        raise HTTPException(409, 'Concurrent change')
    monkeypatch.setattr(actions, 'confirm_occurrence', reject_after_payment)
    async with db_factory() as db:
        connection = await db.get(IntegrationConnection, connection_id)
        inbox = IntegrationInbox(connection_id=connection_id, external_event_id='partial', event_type='test', payload={})
        db.add(inbox)
        with pytest.raises(HTTPException):
            await actions.handle_callback(db, connection, Callback(token, 'chat', 'user', 'message'))
        inbox.result = {'error': 'Concurrent change'}
        await db.commit()
    async with db_factory() as db:
        assert await db.scalar(select(IntegrationInbox.id).where(IntegrationInbox.external_event_id == 'partial'))
        assert not (await db.get(Occurrence, occurrence_id)).paid
        assert (await db.get(BotAction, token)).consumed_at is None
        assert await db.scalar(select(func.count()).select_from(Receipt).where(Receipt.occurrence_id == occurrence_id)) == 0
        assert await db.scalar(select(func.count()).select_from(Delivery).where(Delivery.occurrence_id == occurrence_id, Delivery.event != 'created')) == 0
