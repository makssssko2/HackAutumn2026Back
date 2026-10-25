"""Explicit demo data: python -m app.seed (idempotent, no external sends)."""
import asyncio
from datetime import timedelta

from sqlalchemy import select

from app.core.bootstrap import bootstrap
from app.core.config import today
from app.core.db import SessionLocal, engine
from app.core.security import hash_password_async as hash_password
from app.modules.payments.service import confirm_occurrence, create_payment
from app.modules.groups.models import AudienceGroup
from app.modules.channels.models import Channel
from app.modules.invoices.models import Occurrence
from app.modules.payments.models import RecurringPayment
from app.modules.users.models import User
from app.modules.payments.schemas import RecurringIn


async def main():
    (await bootstrap())
    async with SessionLocal() as db:
        if (await db.scalar(select(RecurringPayment.id))):
            print('Demo payments already exist; nothing changed.')
            return
        if not (await db.scalar(select(User).where(User.login == 'demo'))):
            db.add(User(login='demo', name='Алексей Морозов', password_hash=(await hash_password('demo12345'))))
        internal = (await db.scalar(select(Channel).where(Channel.kind == 'inapp')))
        for name, description in [('Инфраструктура', 'Серверы, облака и инструменты команды'), ('Офис', 'Аренда и операционные расходы'), ('Подписки', 'Рабочие сервисы и лицензии')]:
            db.add(AudienceGroup(name=name, description=description, channel_ids=[internal.id]))
        (await db.flush())
        samples = [
            ('Аренда офиса', 'Аренда рабочего пространства команды', '85000', 'RUB', -4),
            ('Облачный сервер Aéza', 'Основной сервер приложения и базы данных', '3490', 'RUB', 2),
            ('GitHub Team', 'Совместная работа над кодом', '24', 'USD', 3),
            ('Figma Professional', 'Дизайн и прототипирование', '45', 'EUR', 5),
            ('Резервное копирование', 'Ежедневные резервные копии', '1290', 'RUB', 7),
            ('Notion Plus', 'База знаний команды', '30', 'USD', 9),
            ('Домен проекта', 'Продление основного домена', '1890', 'RUB', -8),
            ('Мониторинг серверов', 'Наблюдение за доступностью сервисов', '19', 'EUR', -2),
        ]
        for title, description, amount, currency, offset in samples:
            payment = (await create_payment(db, RecurringIn(title=title, description=description, amount=amount, currency=currency, first_due_date=today() + timedelta(days=offset)), 'admin'))
            if title in ('Домен проекта', 'Мониторинг серверов'):
                row = (await db.scalar(select(Occurrence).where(Occurrence.recurring_id == payment.id).order_by(Occurrence.due_date)))
                (await confirm_occurrence(db, row.id, 'demo', paid=True, paid_at=row.due_date, demo=True))
        (await db.commit())
    print('Created 8 demo payments, groups and demo user (demo / demo12345).')


async def run():
    try:
        await main()
    finally:
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(run())
