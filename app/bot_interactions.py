import calendar
import secrets
from datetime import date, timedelta, timezone
from html import escape

from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.core.config import today
from app.domain.services import confirm_occurrence
from app.models import BotAction, Receipt, RecurringPayment, utcnow
from app.presentation.message_templates import amount_label
from app.presentation.receipts import receipt_html

MONTHS = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь', 'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь']


def button(text, callback, style=None):
    return InlineKeyboardButton(text=text, callback_data=callback, style=style)


def calendar_markup(action, month):
    token = action.token
    rows = [[button(f'{MONTHS[month.month - 1]} {month.year}', 'noop')],
            [button(label, 'noop') for label in ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']]]
    for week in calendar.monthcalendar(month.year, month.month):
        rows.append([button(str(day), f'bd:{token}:{month.year:04}{month.month:02}{day:02}',
                            'primary' if date(month.year, month.month, day) == getattr(action, 'selected_date', None) else None)
                     if day and date(month.year, month.month, day) <= today() else button('·', 'noop') for day in week])
    previous = (month - timedelta(days=1)).replace(day=1)
    following = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    navigation = []
    if previous.year >= 2000:
        navigation.append(button('‹ Предыдущий', f'bm:{token}:{previous:%Y%m}'))
    if following <= today().replace(day=1):
        navigation.append(button('Следующий ›', f'bm:{token}:{following:%Y%m}'))
    if navigation:
        rows.append(navigation)
    rows += [[button('Сегодня · ' + today().strftime('%d.%m'), f'bd:{token}:{today():%Y%m%d}')],
             [button('Закрыть', f'bn:{token}')]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def calendar_text(row, description=''):
    description_line = f'\n<i>{escape(description)}</i>' if description else ''
    return f'<b>Когда счет был оплачен?</b>\n\n{escape(row.title)}{description_line}\n<b>{amount_label(row)}</b>\n\nВыберите фактическую дату в календаре. Срок счета {row.due_date:%d.%m.%Y} сохранится, просрочка пересчитается.'


def confirmation(row, action, description=''):
    description_line = f'\n<i>{escape(description)}</i>' if description else ''
    if action.mode == 'undo':
        return (f'<b>Отменить отметку оплаты?</b>\n\n{escape(row.title)}{description_line}\n\nСчет снова станет неоплаченным, фактическая дата очистится, демонстрационный чек будет аннулирован.',
                InlineKeyboardMarkup(inline_keyboard=[[button('Отменить отметку', f'bc:{action.token}', 'danger')], [button('Оставить как есть', f'bn:{action.token}')]]))
    delay = max(0, (action.selected_date - row.due_date).days)
    text = f'<b>{"Исправление даты" if row.paid else "Подтверждение оплаты"}</b>\n\n{escape(row.title)}{description_line}\n<b>{amount_label(row)}</b>\n\nДата оплаты: <b>{action.selected_date:%d.%m.%Y}</b>\n' + (f'Просрочка: {delay} дн.' if delay else 'Оплачено вовремя.')
    text += '\n\n<i>Демонстрационный режим. Денежные средства не списываются.</i>'
    return text, InlineKeyboardMarkup(inline_keyboard=[
        [button('Сохранить дату' if row.paid else 'Подтвердить оплату', f'bc:{action.token}', 'success')],
        [button('Выбрать другую дату', f'bm:{action.token}:{action.selected_date:%Y%m}')],
        [button('Отмена', f'bn:{action.token}')]])


async def open_action(db, query, bot, row, mode):
    db.execute(delete(BotAction).where(BotAction.expires_at < utcnow()))
    action = BotAction(token=secrets.token_urlsafe(12), occurrence_id=row.id,
                       chat_id=str(query.message.chat.id), user_id=str(query.from_user.id),
                       mode=mode, revision=row.revision, selected_date=row.paid_at or today(),
                       expires_at=utcnow() + timedelta(minutes=15))
    db.add(action)
    db.commit()
    payment = db.get(RecurringPayment, row.recurring_id)
    description = payment.description
    if mode == 'date':
        text, markup = calendar_text(row, description), calendar_markup(action, action.selected_date.replace(day=1))
    else:
        text, markup = confirmation(row, action, description)
    await query.answer()
    await bot.send_message(query.message.chat.id, text, parse_mode='HTML', reply_markup=markup)


async def handle_action(db, query, bot, allowed_occurrence):
    parts = (query.data or '').split(':')
    action = db.get(BotAction, parts[1]) if len(parts) >= 2 else None
    if not action or action.expires_at.replace(tzinfo=timezone.utc) < utcnow():
        await query.answer('Выбор даты истек. Откройте действие заново в сообщении счета.', show_alert=True)
        return
    if action.user_id != str(query.from_user.id) or action.chat_id != str(query.message.chat.id):
        await query.answer('Эти кнопки предназначены участнику, который открыл действие.', show_alert=True)
        return
    row = allowed_occurrence(db, query, action.occurrence_id)
    if not row or row.cancelled or row.revision != action.revision:
        await query.answer('Счет уже изменен. Откройте действие заново.', show_alert=True)
        return
    command = parts[0]
    payment = db.get(RecurringPayment, row.recurring_id)
    description = payment.description
    if command == 'bn':
        db.delete(action)
        db.commit()
        await query.answer('Действие отменено')
        await query.message.edit_text('Действие отменено. Счет не изменен.')
        return
    if command in ('bm', 'bd'):
        try:
            raw = parts[2]
            chosen = date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]) if command == 'bd' else 1)
            if len(raw) != (8 if command == 'bd' else 6) or chosen.year < 2000 or chosen > today():
                raise ValueError()
        except (ValueError, IndexError):
            await query.answer('Выберите дату не позднее сегодняшней.', show_alert=True)
            return
        if command == 'bm':
            text, markup = calendar_text(row, description), calendar_markup(action, chosen)
        else:
            action.selected_date = chosen
            db.commit()
            text, markup = confirmation(row, action, description)
        await query.answer()
        try:
            await query.message.edit_text(text, parse_mode='HTML', reply_markup=markup)
        except TelegramAPIError as error:
            if 'message is not modified' not in str(error):
                raise
        return
    if command != 'bc':
        await query.answer('Неизвестное действие')
        return
    mode, selected, revision = action.mode, action.selected_date, action.revision
    claimed = db.execute(delete(BotAction).where(BotAction.token == action.token))
    if not claimed.rowcount:
        await query.answer('Это действие уже подтверждено', show_alert=True)
        return
    actor = f'telegram:{query.from_user.id} ({query.from_user.full_name})'[:200]
    try:
        row, _ = confirm_occurrence(db, row.id, actor, paid=mode != 'undo',
                                    paid_at=selected if mode != 'undo' else None,
                                    demo=not row.paid and mode != 'undo', expected_revision=revision)
        db.commit()
    except HTTPException as error:
        db.rollback()
        await query.answer(str(error.detail), show_alert=True)
        return
    await query.answer('Отметка отменена' if mode == 'undo' else 'Оплата сохранена', show_alert=True)
    try:
        await query.message.edit_text('Отметка оплаты отменена.' if mode == 'undo' else f'Оплата сохранена. Фактическая дата: {row.paid_at:%d.%m.%Y}.')
    except TelegramAPIError:
        pass
    if mode != 'undo':
        receipt = db.scalar(select(Receipt).where(Receipt.occurrence_id == row.id, Receipt.voided.is_(False)))
        if receipt:
            await bot.send_document(query.message.chat.id, BufferedInputFile(receipt_html(receipt, row, description).encode(), filename=f'{receipt.number}.html'), caption='Демонстрационный чек · дата оплаты сохранена, деньги не списывались.')
