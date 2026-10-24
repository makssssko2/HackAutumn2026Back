from html import escape

from app.core.config import today


def amount_label(row):
    return f'{row.amount:,.2f}'.replace(',', ' ').replace('.', ',') + ' ' + row.currency


def status_label(row, event):
    if row.cancelled:
        return 'Счет отменен'
    if row.paid:
        return 'Оплата подтверждена'
    if row.due_date < today():
        return 'Счет просрочен'
    return 'Напоминание об оплате' if event == 'reminder' else 'Новый счет для команды'


def telegram_message(row, event):
    lines = [f'<b>{status_label(row, event)}</b>', '', f'<b>{escape(row.title)}</b>',
             f'<blockquote><b>{amount_label(row)}</b>\nСрок оплаты: {row.due_date:%d.%m.%Y}</blockquote>']
    if row.paid:
        delay = max(0, (row.paid_at - row.due_date).days)
        lines += [f'Фактическая оплата: <b>{row.paid_at:%d.%m.%Y}</b>',
                  f'Подтвердил: {escape(row.paid_by or "Участник")}',
                  f'Просрочка: {delay} дн.' if delay else 'Оплачено вовремя.']
    elif not row.cancelled:
        delay = max(0, (today() - row.due_date).days)
        if delay:
            lines += [f'<b>Просрочка: {delay} дн.</b>', 'Уже оплатили? Укажите фактическую дату кнопкой ниже.']
        else:
            lines += ['Счет оплачивает один участник целиком.']
    lines += ['', '<i>Invoice Keeper · демонстрационный режим, без списания средств</i>']
    return '\n'.join(lines)


def email_html(row, event, url):
    title, state, amount = escape(row.title), status_label(row, event), amount_label(row)
    if row.paid:
        caption = f'Оплата подтверждена {row.paid_at:%d.%m.%Y}. Чек доступен на странице счета.'
        button = 'Посмотреть счет и чек'
    elif row.cancelled:
        caption, button = 'Администратор отменил этот счет. Оплата не требуется.', 'Посмотреть статус'
    else:
        delay = max(0, (today() - row.due_date).days)
        caption = (f'Срок прошел {delay} дн. назад. Если вы оплатили раньше, укажите фактическую дату.'
                   if delay else 'Оплатить общий счет может один участник команды. Статус обновится для всех.')
        button = 'Оплатить счет'
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f6f4f1;color:#202020;font-family:Arial,sans-serif">
<div style="display:none;max-height:0;overflow:hidden">{escape(state)}: {title} · {amount} · до {row.due_date:%d.%m.%Y}</div>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f6f4f1"><tr><td align="center" style="padding:32px 16px">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" style="width:100%;max-width:600px">
<tr><td style="padding:0 0 24px;font-size:22px;font-weight:bold;letter-spacing:-0.5px">invoice<span style="color:#a44900">keeper</span></td></tr>
<tr><td style="padding:32px;background:#ffffff;border-radius:16px">
<p style="margin:0 0 20px;font-size:14px;color:#805326">{escape(state)}</p>
<h1 style="margin:0 0 24px;font-size:28px;line-height:1.25;overflow-wrap:anywhere">{title}</h1>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#fff0e2;border-radius:12px"><tr><td style="padding:24px">
<p style="margin:0 0 8px;color:#805326;font-size:13px">Сумма к оплате</p>
<p style="margin:0 0 16px;font-size:34px;font-weight:bold">{amount}</p>
<p style="margin:0;font-size:14px">Срок оплаты · <strong>{row.due_date:%d.%m.%Y}</strong></p>
</td></tr></table>
<p style="margin:24px 0;line-height:1.6;font-size:15px;color:#57524d">{escape(caption)}</p>
<table role="presentation" cellspacing="0" cellpadding="0"><tr><td bgcolor="#ff8000" style="border-radius:10px;text-align:center">
<a href="{escape(url, quote=True)}" style="display:inline-block;padding:16px 24px;color:#202020;text-decoration:none;font-size:16px;font-weight:bold">{button} →</a>
</td></tr></table>
<p style="margin:24px 0 0;font-size:13px;line-height:1.6;color:#63615e">Откроется только этот счет. Вход в панель администратора не требуется. Подтверждение оплаты выполняется отдельной кнопкой.</p>
</td></tr><tr><td style="padding:22px 8px;font-size:12px;color:#63615e;line-height:1.6">Демонстрационный режим: деньги не списываются, чек не является фискальным.<br>Ссылка на счет действует 90 дней. Не пересылайте ее посторонним.</td></tr>
</table></td></tr></table></body></html>'''
