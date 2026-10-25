"""Explicit demo data: python -m app.seed (idempotent, no external sends)."""
from datetime import timedelta

from sqlalchemy import select

from app.core.bootstrap import bootstrap
from app.core.config import today
from app.core.db import SessionLocal
from app.core.security import hash_password
from app.modules.payments.service import confirm_occurrence, create_payment
from app.models import AudienceGroup, Channel, Occurrence, RecurringPayment, User
from app.schemas import RecurringIn


def main():
    bootstrap()
    with SessionLocal() as db:
        if db.scalar(select(RecurringPayment.id)):
            print('Demo payments already exist; nothing changed.')
            return
        if not db.scalar(select(User).where(User.login == 'demo')):
            db.add(User(login='demo', name='Алексей Морозов', password_hash=hash_password('demo12345')))
        internal = db.scalar(select(Channel).where(Channel.kind == 'inapp'))
        for name, description in [('Инфраструктура', 'Серверы, облака и инструменты команды'), ('Офис', 'Аренда и операционные расходы'), ('Подписки', 'Рабочие сервисы и лицензии')]:
            db.add(AudienceGroup(name=name, description=description, channel_ids=[internal.id]))
        db.flush()
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
            payment = create_payment(db, RecurringIn(title=title, description=description, amount=amount, currency=currency, first_due_date=today() + timedelta(days=offset)), 'admin')
            if title in ('Домен проекта', 'Мониторинг серверов'):
                row = db.scalar(select(Occurrence).where(Occurrence.recurring_id == payment.id).order_by(Occurrence.due_date))
                confirm_occurrence(db, row.id, 'demo', paid=True, paid_at=row.due_date, demo=True)
        db.commit()
    print('Created 8 demo payments, groups and demo user (demo / demo12345).')


if __name__ == '__main__':
    main()
