import asyncio

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.bootstrap import bootstrap
from app.config import settings
from app.db import SessionLocal
from app.models import Channel, ChannelLink, Delivery, Occurrence, Receipt, utcnow
from app.notifications import telegram_buttons
from app.receipts import receipt_html
from app.services import audit, confirm_occurrence

dispatcher = Dispatcher()


@dispatcher.message(Command('start', 'help'))
async def help_message(message: Message):
    await message.answer('Invoice Keeper\n\nДобавьте бота в группу и привяжите чат командой /connect из панели администратора. Оплату счета может подтвердить любой участник подключенного чата. Все оплаты демонстрационные.')


@dispatcher.message(Command('connect'))
async def connect(message: Message, bot: Bot):
    if message.chat.type not in ('group', 'supergroup') or not message.from_user:
        await message.answer('Выполните команду в группе, где вы администратор.')
        return
    member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    if member.status not in ('creator', 'administrator'):
        await message.answer('Привязать чат может его администратор.')
        return
    parts = (message.text or '').split(maxsplit=1)
    with SessionLocal() as db:
        channel_id = db.scalar(delete(ChannelLink).where(
            ChannelLink.token == (parts[1].strip() if len(parts) == 2 else ''),
            ChannelLink.expires_at >= utcnow(),
        ).returning(ChannelLink.channel_id))
        if not channel_id:
            await message.answer('Код недействителен или истек. Получите новый в панели.')
            return
        channel = db.get(Channel, channel_id)
        if not channel or channel.kind != 'telegram':
            await message.answer('Telegram-канал недоступен. Получите новый код в панели.')
            return
        channel.config = {**channel.config, 'chat_id': str(message.chat.id)}
        channel.active = True
        audit(db, f'telegram:{message.from_user.id}', 'channel.connected', channel.id, chat_id=str(message.chat.id))
        db.commit()
    await message.answer('Чат подключен. Здесь будут появляться счета и напоминания.')


def allowed_occurrence(db, query: CallbackQuery, occurrence_id: str):
    if not isinstance(query.message, Message):
        return None
    channels = list(db.scalars(select(Channel).where(Channel.kind == 'telegram', Channel.active.is_(True))))
    channel_ids = [c.id for c in channels if c.config.get('chat_id') == str(query.message.chat.id)]
    if not channel_ids:
        return None
    delivered = db.scalar(select(Delivery.id).where(Delivery.occurrence_id == occurrence_id, Delivery.channel_id.in_(channel_ids), Delivery.remote_message_id.is_not(None)))
    return db.get(Occurrence, occurrence_id) if delivered else None


@dispatcher.callback_query(F.data)
async def handle_action(query: CallbackQuery, bot: Bot):
    parts = (query.data or '').split(':')
    if len(parts) < 2:
        await query.answer('Неизвестное действие')
        return
    action, occurrence_id = parts[:2]
    with SessionLocal() as db:
        row = allowed_occurrence(db, query, occurrence_id)
        if not row:
            await query.answer('Этот счет недоступен в данном чате', show_alert=True)
            return
        if action == 'receipt':
            receipt = db.scalar(select(Receipt).where(Receipt.occurrence_id == row.id, Receipt.voided.is_(False)).order_by(Receipt.created_at.desc()))
            if not receipt:
                await query.answer('Демонстрационный чек не создан. Внешний чек можно прикрепить в панели.', show_alert=True)
                return
            document = BufferedInputFile(receipt_html(receipt, row).encode('utf-8'), filename=f'{receipt.number}.html')
            await query.answer()
            await bot.send_document(query.message.chat.id, document, caption='Демонстрационный чек · денежные средства не списывались.')
            return
        if row.cancelled:
            await query.answer('Счет отменен', show_alert=True)
            return
        if row.paid:
            await query.answer('Счет уже оплачен', show_alert=True)
            return
        if action == 'pay':
            await query.answer()
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Подтвердить демооплату', callback_data=f'confirm:{row.id}:{query.from_user.id}')]])
            await bot.send_message(query.message.chat.id, f'{query.from_user.full_name}, подтвердите демонстрационную оплату:\n{row.title}\n{row.amount:.2f} {row.currency}\n\nДенежные средства не списываются.', reply_markup=keyboard)
            return
        if action == 'confirm' and (len(parts) != 3 or parts[2] != str(query.from_user.id)):
            await query.answer('Подтверждение предназначено другому участнику', show_alert=True)
            return
        if action not in ('confirm', 'mark'):
            await query.answer('Неизвестное действие')
            return
        actor = f'telegram:{query.from_user.id} ({query.from_user.full_name})'[:200]
        try:
            row, changed = confirm_occurrence(db, row.id, actor, paid=True, demo=action == 'confirm', expected_revision=row.revision)
            db.commit()
        except HTTPException as exc:
            db.rollback()
            await query.answer(str(exc.detail), show_alert=True)
            return
        await query.answer('Оплата подтверждена' if changed else 'Счет уже оплачен', show_alert=True)
        if action == 'confirm':
            await query.message.edit_reply_markup(reply_markup=None)
            receipt = db.scalar(select(Receipt).where(Receipt.occurrence_id == row.id, Receipt.voided.is_(False)).order_by(Receipt.created_at.desc()))
            if receipt:
                await bot.send_document(query.message.chat.id, BufferedInputFile(receipt_html(receipt, row).encode('utf-8'), filename=f'{receipt.number}.html'), caption='Демонстрационная оплата подтверждена. Чек также сохранен в панели.')


async def main():
    if not settings.telegram_bot_token:
        raise SystemExit('Укажите TELEGRAM_BOT_TOKEN в .env')
    bootstrap()
    async with Bot(settings.telegram_bot_token) as bot:
        await dispatcher.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())
