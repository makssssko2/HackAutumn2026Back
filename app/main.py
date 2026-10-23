import calendar
import json
import re
import secrets
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.bootstrap import bootstrap
from app.config import settings, today
from app.db import Base, engine, get_db
from app.models import Attachment, AuditLog, AudienceGroup, AuthSession, Channel, ChannelLink, Delivery, Occurrence, Receipt, RecurringPayment, User, uid, utcnow
from app.notifications import validate_webhook
from app.receipts import receipt_html
from app.schemas import ChannelIn, GroupIn, LinkIn, LoginIn, OccurrencePatch, RecurringIn, UserIn, UserPatch
from app.security import admin_user, current_user, hash_password, token_hash, verify_password
from app.services import audit, confirm_occurrence, create_payment, generate_occurrences, occurrence_out, queue_event, stop_payment, summarize


@asynccontextmanager
async def lifespan(app):
    # Schema migrations are an explicit startup step; never auto-create production tables.
    bootstrap()
    yield


app = FastAPI(title='Invoice Keeper API', version='1.0.0', description='Учет регулярных расходов, уведомления и демонстрационная оплата.', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins, allow_credentials=True, allow_methods=['GET', 'POST', 'PATCH', 'DELETE'], allow_headers=['Content-Type'])
prefix = '/api/v1'
login_attempts = defaultdict(list)


@app.middleware('http')
async def protect_origins(request: Request, call_next):
    origin = request.headers.get('origin')
    if request.method in ('POST', 'PATCH', 'DELETE', 'PUT') and origin and origin not in settings.allowed_origins:
        return JSONResponse({'detail': 'Недопустимый источник запроса'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'same-origin'
    return response


def public(model, exclude=()):
    return {column.name: getattr(model, column.name) for column in model.__table__.columns if column.name not in exclude}


def user_out(user):
    return public(user, ('password_hash',))


def get_or_404(db, cls, oid):
    row = db.get(cls, oid)
    if not row:
        raise HTTPException(404, 'Объект не найден')
    return row


@app.get('/health', tags=['System'])
def health():
    return {'status': 'ok'}


@app.post(prefix + '/auth/login', tags=['Auth'])
def login(data: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    ip = request.client.host if request.client else 'local'
    now = datetime.now(timezone.utc)
    login_attempts[ip] = [stamp for stamp in login_attempts[ip] if now - stamp < timedelta(minutes=1)]
    if len(login_attempts[ip]) >= 10:
        raise HTTPException(429, 'Слишком много попыток. Попробуйте через минуту')
    user = db.scalar(select(User).where(User.login == data.login, User.active.is_(True)))
    if not user or not verify_password(data.password, user.password_hash):
        login_attempts[ip].append(now)
        audit(db, 'anonymous', 'auth.failed', details_login=data.login)
        db.commit()
        raise HTTPException(401, 'Неверный логин или пароль')
    token = secrets.token_urlsafe(48)
    db.add(AuthSession(token_hash=token_hash(token), user_id=user.id, expires_at=now + timedelta(days=7)))
    audit(db, user.login, 'auth.login', user.id)
    db.commit()
    response.set_cookie('ik_session', token, httponly=True, secure=settings.cookie_secure, samesite='lax', max_age=604800, path='/api')
    return user_out(user)


@app.get(prefix + '/auth/me', tags=['Auth'])
def me(user: User = Depends(current_user)):
    return user_out(user)


@app.post(prefix + '/auth/logout', tags=['Auth'])
def logout(request: Request, response: Response, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.execute(delete(AuthSession).where(AuthSession.token_hash == token_hash(request.cookies['ik_session'])))
    audit(db, user.login, 'auth.logout', user.id)
    db.commit()
    response.delete_cookie('ik_session', path='/api')
    return {'ok': True}


@app.get(prefix + '/users', tags=['Users'])
def users(user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return [user_out(row) for row in db.scalars(select(User).where(User.deleted.is_(False)).order_by(User.created_at))]


@app.post(prefix + '/users', status_code=201, tags=['Users'])
def add_user(data: UserIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    if db.scalar(select(User.id).where(User.login == data.login)):
        raise HTTPException(409, 'Логин уже занят')
    row = User(login=data.login, name=data.name, password_hash=hash_password(data.password))
    db.add(row)
    db.flush()
    audit(db, user.login, 'user.created', row.id, login=row.login)
    db.commit()
    return user_out(row)


@app.patch(prefix + '/users/{user_id}', tags=['Users'])
def change_user(user_id: str, data: UserPatch, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    row = get_or_404(db, User, user_id)
    if row.role == 'admin':
        raise HTTPException(403, 'Администратор управляется только через .env')
    for key, value in data.model_dump(exclude_unset=True).items():
        if value is None:
            raise HTTPException(422, 'Значение не может быть пустым')
        if key == 'password':
            row.password_hash = hash_password(value)
        else:
            setattr(row, key, value)
    if data.password or data.active is False:
        db.execute(delete(AuthSession).where(AuthSession.user_id == row.id))
    audit(db, user.login, 'user.updated', row.id)
    db.commit()
    return user_out(row)


@app.delete(prefix + '/users/{user_id}', tags=['Users'])
def remove_user(user_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    row = get_or_404(db, User, user_id)
    if row.role == 'admin':
        raise HTTPException(403, 'Нельзя удалить администратора')
    audit(db, user.login, 'user.deleted', row.id, login=row.login)
    row.active = False
    row.deleted = True
    row.login = 'deleted_' + row.id
    row.telegram_id = None
    db.execute(delete(AuthSession).where(AuthSession.user_id == row.id))
    db.commit()
    return {'ok': True}


@app.get(prefix + '/recurring-payments', tags=['Payments'])
def recurring(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [{**public(row), 'amount': str(row.amount)} for row in db.scalars(select(RecurringPayment).order_by(RecurringPayment.created_at.desc()))]


@app.post(prefix + '/recurring-payments', status_code=201, tags=['Payments'])
def add_recurring(data: RecurringIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    row = create_payment(db, data, user.login)
    db.commit()
    return {**public(row), 'amount': str(row.amount)}


@app.delete(prefix + '/recurring-payments/{payment_id}', tags=['Payments'])
def remove_recurring(payment_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    payment = get_or_404(db, RecurringPayment, payment_id)
    if payment.active:
        stop_payment(db, payment, user.login)
        db.commit()
    return {'ok': True}


def occurrence_query(date_from=None, date_to=None, status=None, currency=None, q=None, recurring_id=None):
    query = select(Occurrence)
    if date_from:
        query = query.where(Occurrence.due_date >= date_from)
    if date_to:
        query = query.where(Occurrence.due_date <= date_to)
    if currency:
        query = query.where(Occurrence.currency == currency)
    if recurring_id:
        query = query.where(Occurrence.recurring_id == recurring_id)
    if q:
        query = query.where(Occurrence.title.icontains(q, autoescape=True))
    if status == 'paid':
        query = query.where(Occurrence.paid.is_(True), Occurrence.cancelled.is_(False))
    elif status == 'overdue':
        query = query.where(Occurrence.paid.is_(False), Occurrence.cancelled.is_(False), Occurrence.due_date < today())
    elif status == 'unpaid':
        query = query.where(Occurrence.paid.is_(False), Occurrence.cancelled.is_(False))
    elif status == 'cancelled':
        query = query.where(Occurrence.cancelled.is_(True))
    return query.order_by(Occurrence.due_date, Occurrence.title)


@app.get(prefix + '/payment-occurrences', tags=['Occurrences'])
def occurrences(date_from: date | None = None, date_to: date | None = None, status: str | None = None, currency: str | None = None, q: str | None = None, recurring_id: str | None = None, limit: int = Query(1000, ge=1, le=10000), user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [occurrence_out(row) for row in db.scalars(occurrence_query(date_from, date_to, status, currency, q, recurring_id).limit(limit))]


@app.get(prefix + '/payment-occurrences/{occurrence_id}', tags=['Occurrences'])
def occurrence(occurrence_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = get_or_404(db, Occurrence, occurrence_id)
    payment = db.get(RecurringPayment, row.recurring_id)
    return {**occurrence_out(row), 'description': payment.description, 'frequency': payment.frequency,
            'receipts': [public(r) for r in db.scalars(select(Receipt).where(Receipt.occurrence_id == row.id).order_by(Receipt.created_at.desc()))],
            'attachments': [public(a, ('storage_name',)) for a in db.scalars(select(Attachment).where(Attachment.occurrence_id == row.id).order_by(Attachment.created_at.desc()))],
            'history': [public(a) for a in db.scalars(select(AuditLog).where(AuditLog.object_id == row.id).order_by(AuditLog.created_at.desc()))]}


@app.patch(prefix + '/payment-occurrences/{occurrence_id}', tags=['Occurrences'])
def change_occurrence(occurrence_id: str, data: OccurrencePatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row, changed = confirm_occurrence(db, occurrence_id, user.login, **data.model_dump())
    db.commit()
    return {**occurrence_out(row), 'changed': changed}


@app.get(prefix + '/payments/summary', tags=['Reports'])
def summary(date_from: date | None = None, date_to: date | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    start = date_from or today().replace(day=1)
    end = date_to or today().replace(day=calendar.monthrange(today().year, today().month)[1])
    return {'date_from': start, 'date_to': end, 'currencies': summarize(db.scalars(occurrence_query(start, end)))}


@app.get(prefix + '/payments/budget', tags=['Reports'])
def budget(year: int = Query(default_factory=lambda: today().year, ge=2000, le=2100), user: User = Depends(current_user), db: Session = Depends(get_db)):
    until = date(year, 12, 31)
    for payment in db.scalars(select(RecurringPayment).where(RecurringPayment.active.is_(True))):
        generate_occurrences(db, payment, until)
    db.commit()
    rows = list(db.scalars(occurrence_query(date(year, 1, 1), until)))
    return {'year': year, 'currencies': summarize(rows), 'months': [{'month': month, 'currencies': summarize([r for r in rows if r.due_date.month == month])} for month in range(1, 13)]}


@app.get(prefix + '/channels', tags=['Notifications'])
def channels(user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return [{**public(row), 'config': {k: v for k, v in row.config.items() if k != 'secret'}, 'has_secret': bool(row.config.get('secret'))} for row in db.scalars(select(Channel).order_by(Channel.name))]


def check_channel(data: ChannelIn):
    if data.kind == 'email' and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', data.config.get('email', '')):
        raise HTTPException(422, 'Укажите корректный email')
    if data.kind == 'webhook':
        try:
            validate_webhook(data.config.get('url', ''))
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc) if isinstance(exc, ValueError) else 'Не удалось проверить адрес webhook')
    if data.kind == 'telegram' and data.config.get('chat_id') and not re.fullmatch(r'-?\d+', data.config['chat_id']):
        raise HTTPException(422, 'Chat ID должен быть числом')


@app.post(prefix + '/channels', status_code=201, tags=['Notifications'])
def add_channel(data: ChannelIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    check_channel(data)
    row = Channel(**data.model_dump())
    db.add(row)
    db.flush()
    audit(db, user.login, 'channel.created', row.id, kind=row.kind, name=row.name)
    db.commit()
    return public(row, ('config',))


@app.patch(prefix + '/channels/{channel_id}', tags=['Notifications'])
def update_channel(channel_id: str, data: ChannelIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    row = get_or_404(db, Channel, channel_id)
    if row.kind == 'inapp' and (not data.active or data.kind != 'inapp'):
        raise HTTPException(409, 'Внутренние уведомления должны оставаться включены')
    check_channel(data)
    config = data.config.copy()
    if row.kind == data.kind == 'webhook' and 'secret' not in config and row.config.get('secret'):
        config['secret'] = row.config['secret']
    row.name, row.kind, row.config, row.active = data.name, data.kind, config, data.active
    audit(db, user.login, 'channel.updated', row.id)
    db.commit()
    return public(row, ('config',))


@app.post(prefix + '/channels/{channel_id}/telegram-link', tags=['Notifications'])
def telegram_link(channel_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    row = get_or_404(db, Channel, channel_id)
    if row.kind != 'telegram':
        raise HTTPException(422, 'Это не Telegram-канал')
    token = secrets.token_urlsafe(18)
    db.add(ChannelLink(token=token, channel_id=row.id, expires_at=utcnow() + timedelta(minutes=10)))
    audit(db, user.login, 'channel.link_requested', row.id)
    db.commit()
    return {'command': f'/connect {token}', 'expires_in': 600}


@app.post(prefix + '/channels/{channel_id}/test', tags=['Notifications'])
def test_channel(channel_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    channel = get_or_404(db, Channel, channel_id)
    row = db.scalar(select(Occurrence).where(Occurrence.cancelled.is_(False)).order_by(Occurrence.due_date.desc()))
    if not row:
        raise HTTPException(409, 'Сначала создайте хотя бы один платеж')
    db.add(Delivery(occurrence_id=row.id, channel_id=channel.id, event='test:' + uid()))
    audit(db, user.login, 'channel.test', channel.id)
    db.commit()
    return {'ok': True, 'message': 'Тестовая отправка добавлена в очередь'}


@app.get(prefix + '/groups', tags=['Groups'])
def groups(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [public(row) for row in db.scalars(select(AudienceGroup).order_by(AudienceGroup.name))]


def check_group(db, data):
    found = set(db.scalars(select(Channel.id).where(Channel.id.in_(data.channel_ids))))
    if found != set(data.channel_ids):
        raise HTTPException(422, 'Один из каналов не найден')


@app.post(prefix + '/groups', status_code=201, tags=['Groups'])
def add_group(data: GroupIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    check_group(db, data)
    row = AudienceGroup(**data.model_dump())
    db.add(row)
    db.flush()
    audit(db, user.login, 'group.created', row.id, name=row.name)
    db.commit()
    return public(row)


@app.patch(prefix + '/groups/{group_id}', tags=['Groups'])
def change_group(group_id: str, data: GroupIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    row = get_or_404(db, AudienceGroup, group_id)
    check_group(db, data)
    for key, value in data.model_dump().items():
        setattr(row, key, value)
    audit(db, user.login, 'group.updated', row.id)
    db.commit()
    return public(row)


@app.delete(prefix + '/groups/{group_id}', tags=['Groups'])
def remove_group(group_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    row = get_or_404(db, AudienceGroup, group_id)
    if any(group_id in p.group_ids for p in db.scalars(select(RecurringPayment).where(RecurringPayment.active.is_(True)))):
        raise HTTPException(409, 'Группа используется активным платежом')
    audit(db, user.login, 'group.deleted', row.id, name=row.name)
    db.delete(row)
    db.commit()
    return {'ok': True}


@app.get(prefix + '/notifications', tags=['Notifications'])
def notifications(user: User = Depends(current_user), db: Session = Depends(get_db)):
    query = select(Delivery, Occurrence, Channel).join(Occurrence, Delivery.occurrence_id == Occurrence.id).join(Channel, Delivery.channel_id == Channel.id)
    if user.role != 'admin':
        query = query.where(Channel.kind == 'inapp', Delivery.status == 'sent', Delivery.event.in_(['created', 'reminder']))
    rows = list(db.execute(query.order_by(Delivery.created_at.desc()).limit(300)))
    occurrence_ids = {row.id for _, row, _ in rows}
    receipts = {receipt.occurrence_id: receipt.id for receipt in db.scalars(select(Receipt).where(Receipt.occurrence_id.in_(occurrence_ids), Receipt.voided.is_(False)).order_by(Receipt.created_at))}
    return [{**public(delivery), 'title': row.title, 'amount': str(row.amount), 'currency': row.currency, 'channel_name': channel.name, 'channel_kind': channel.kind, 'due_date': row.due_date, 'payment': occurrence_out(row), 'receipt_id': receipts.get(row.id)} for delivery, row, channel in rows]


@app.post(prefix + '/notifications/{delivery_id}/retry', tags=['Notifications'])
def retry_notification(delivery_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    row = get_or_404(db, Delivery, delivery_id)
    if row.status not in ('failed', 'retry', 'uncertain'):
        raise HTTPException(409, 'Эту отправку нельзя повторить')
    row.status, row.next_attempt_at, row.error, row.attempts = 'pending', utcnow(), None, 0
    audit(db, user.login, 'notification.retry', row.id)
    db.commit()
    return {'ok': True}


@app.get(prefix + '/audit', tags=['Audit'])
def audit_list(user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return [public(row) for row in db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(500))]


@app.get(prefix + '/settings', tags=['System'])
def app_settings(user: User = Depends(current_user)):
    return {'timezone': settings.app_timezone, 'today': today(), 'demo_mode': True, 'telegram_configured': bool(settings.telegram_bot_token), 'smtp_configured': bool(settings.smtp_host)}


@app.post(prefix + '/payment-occurrences/{occurrence_id}/attachments', status_code=201, tags=['Attachments'])
async def upload_attachment(occurrence_id: str, file: UploadFile = File(...), user: User = Depends(current_user), db: Session = Depends(get_db)):
    get_or_404(db, Occurrence, occurrence_id)
    allowed = {'.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.txt': 'text/plain'}
    suffix = Path(file.filename or '').suffix.lower()
    if suffix not in allowed:
        raise HTTPException(422, 'Поддерживаются PDF, PNG, JPG и TXT')
    content = await file.read(10 * 1024 * 1024 + 1)
    if not content or len(content) > 10 * 1024 * 1024:
        raise HTTPException(422, 'Размер файла: от 1 байта до 10 МБ')
    signatures = {'.pdf': b'%PDF-', '.png': b'\x89PNG\r\n\x1a\n', '.jpg': b'\xff\xd8\xff', '.jpeg': b'\xff\xd8\xff'}
    if suffix in signatures and not content.startswith(signatures[suffix]):
        raise HTTPException(422, 'Содержимое файла не соответствует формату')
    directory = settings.data_dir / 'attachments'
    directory.mkdir(parents=True, exist_ok=True)
    storage_name = uid() + suffix
    path = directory / storage_name
    path.write_bytes(content)
    row = Attachment(occurrence_id=occurrence_id, name=Path(file.filename.replace('\\', '/')).name[:240], storage_name=storage_name, content_type=allowed[suffix])
    try:
        db.add(row)
        db.flush()
        audit(db, user.login, 'attachment.created', occurrence_id, name=row.name)
        db.commit()
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return public(row, ('storage_name',))


@app.post(prefix + '/payment-occurrences/{occurrence_id}/links', status_code=201, tags=['Attachments'])
def attach_link(occurrence_id: str, data: LinkIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    get_or_404(db, Occurrence, occurrence_id)
    parsed = urlparse(data.url)
    if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(422, 'Нужна ссылка HTTP(S) без учетных данных')
    row = Attachment(occurrence_id=occurrence_id, name=data.name, url=data.url)
    db.add(row)
    audit(db, user.login, 'attachment.linked', occurrence_id, name=data.name)
    db.commit()
    return public(row, ('storage_name',))


@app.get(prefix + '/attachments/{attachment_id}/download', tags=['Attachments'])
def download_attachment(attachment_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = get_or_404(db, Attachment, attachment_id)
    if not row.storage_name:
        raise HTTPException(404, 'Это ссылка, а не файл')
    path = settings.data_dir / 'attachments' / row.storage_name
    if not path.is_file():
        raise HTTPException(404, 'Файл недоступен')
    return FileResponse(path, media_type=row.content_type, filename=row.name)


@app.get(prefix + '/receipts/{receipt_id}', response_class=HTMLResponse, tags=['Receipts'])
def download_receipt(receipt_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    receipt = get_or_404(db, Receipt, receipt_id)
    row = db.get(Occurrence, receipt.occurrence_id)
    return HTMLResponse(receipt_html(receipt, row), headers={'Content-Disposition': f'attachment; filename="{receipt.number}.html"', 'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; sandbox"})


@app.get(prefix + '/docs', include_in_schema=False)
def swagger(user: User = Depends(current_user)):
    return get_swagger_ui_html(openapi_url=prefix + '/openapi.json', title='Invoice Keeper · API')


@app.get(prefix + '/openapi.json', include_in_schema=False)
def openapi(user: User = Depends(current_user)):
    return JSONResponse(app.openapi())
