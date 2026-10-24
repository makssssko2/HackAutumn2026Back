from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import attachments, audit, auth, channels, groups, notifications, occurrences, payments, receipts, reports, system, users
from app.api.auth import login_attempts  # re-exported for tests and legacy imports
from app.notifications import telegram_call  # re-exported: tests monkeypatch main.telegram_call
from app.core.bootstrap import bootstrap
from app.core.config import settings
from app.payment_access import router as payment_access_router

__all__ = ['app', 'login_attempts', 'telegram_call']


@asynccontextmanager
async def lifespan(app):
    # Schema migrations are an explicit startup step; never auto-create production tables.
    bootstrap()
    yield


app = FastAPI(
    title='Invoice Keeper API',
    version='1.0.0',
    description='Учет регулярных расходов и уведомления. Веб-панель и HTTP API доступны только администратору. Участники подтверждают демонстрационную оплату в Telegram или по приватной ссылке одного счета из письма, без веб-аккаунта.',
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=['GET', 'POST', 'PATCH', 'DELETE'],
    allow_headers=['Content-Type'],
)


@app.middleware('http')
async def protect_origins(request: Request, call_next):
    origin = request.headers.get('origin')
    if request.method in ('POST', 'PATCH', 'DELETE', 'PUT') and origin and origin not in settings.allowed_origins:
        return JSONResponse({'detail': 'Недопустимый источник запроса'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = (
        'no-referrer' if '/public/payments/' in request.url.path else 'same-origin'
    )
    return response


app.include_router(payment_access_router)
app.include_router(system.router)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(payments.router)
app.include_router(occurrences.router)
app.include_router(reports.router)
app.include_router(channels.router)
app.include_router(groups.router)
app.include_router(notifications.router)
app.include_router(attachments.router)
app.include_router(receipts.router)
app.include_router(audit.router)
