from html import escape

from app.models import Occurrence, Receipt


def receipt_html(receipt: Receipt, occurrence: Occurrence):
    state = 'АННУЛИРОВАН' if receipt.voided else 'Подтверждение оплаты'
    return f'''<!doctype html><html lang="ru"><meta charset="utf-8"><title>{escape(receipt.number)}</title>
<style>body{{font:16px/1.6 system-ui;color:#152c3c;background:#edf4f7;padding:32px}}main{{max-width:560px;margin:auto;background:white;padding:36px;border-radius:16px}}h1{{font-size:25px}}dl{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}dd{{margin:0;text-align:right}}small{{display:block;color:#4c6270;margin-top:28px}}strong{{color:#086b8f}}</style>
<main><strong>Invoice Keeper</strong><h1>{state}</h1><p>{escape(receipt.number)}</p>
<dl><dt>Назначение</dt><dd>{escape(occurrence.title)}</dd><dt>Сумма</dt><dd>{occurrence.amount:.2f} {occurrence.currency}</dd>
<dt>Период / срок</dt><dd>{occurrence.due_date:%d.%m.%Y}</dd><dt>Дата оплаты</dt><dd>{receipt.paid_at:%d.%m.%Y}</dd>
<dt>Плательщик</dt><dd>{escape(receipt.payer)}</dd></dl>
<small>Демонстрационный чек. Оплата имитирована. Документ не является фискальным чеком и не подтверждает перевод денежных средств.</small></main></html>'''
