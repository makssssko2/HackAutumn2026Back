from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.modules.attachments import router as attachments
from app.modules.audit import router as audit
from app.modules.auth import router as auth
from app.modules.b2b import admin_router as incoming_invoices
from app.modules.b2b import router as b2b
from app.modules.channels import router as channels
from app.modules.groups import router as groups
from app.modules.integrations import router as integrations
from app.modules.invoices import router as occurrences
from app.modules.notifications import router as notifications
from app.modules.payments import one_time_router, reports_router, router as payments
from app.modules.receipts import router as receipts
from app.modules.users import router as users
from app.modules.oidc import router as oidc
from app.modules.auth.router import login_attempts  # re-exported for tests and legacy imports
from app.modules.auth import system_router as system
from app.modules.notifications.service import telegram_call  # re-exported for tests
from app.core.bootstrap import bootstrap
from app.core.config import settings
from app.core.db import engine
from app.core.logging import protect_public_invoice_logs
from app.modules.invoices.public_router import router as public_invoice_router
from app.modules.invoices.payment_access import router as payment_access_router
from app.core.rate_limit import SlidingWindowLimiter

__all__ = ['app', 'login_attempts', 'telegram_call']

protect_public_invoice_logs()


@asynccontextmanager
async def lifespan(app):
    # Schema migrations are an explicit startup step; never auto-create production tables.
    try:
        await bootstrap()
        yield
    finally:
        await engine.dispose()


app = FastAPI(
    title='Invoice Keeper API',
    version='1.0.0',
    description='Учет регулярных расходов и уведомления. Пользователи просматривают счета и подтверждают демонстрационные оплаты, администратор управляет платежами и доставкой. Участники подтверждают демонстрационную оплату в Telegram или по приватной ссылке одного счета из письма, без веб-аккаунта.',
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'],
    allow_headers=['Content-Type'],
)

b2b_rate_limiter = SlidingWindowLimiter(settings.b2b_rate_limit_per_minute, 60, settings.rate_limit_max_keys)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    if request.url.path in ('/api/v1/auth/password', '/api/v1/oidc/provider'):
        # Validation feedback must not echo submitted passwords.
        return JSONResponse(status_code=422, content={'detail': [
            {key: error[key] for key in ('loc', 'msg', 'type')}
            for error in exc.errors()
        ]})
    return await request_validation_exception_handler(request, exc)


@app.middleware('http')
async def protect_origins(request: Request, call_next):
    if settings.b2b_enabled and request.url.path.startswith('/api/b2b/v1/'):
        client_host = request.client.host if request.client else 'unknown'
        if not b2b_rate_limiter.allow(f'b2b-ip:{client_host}'):
            return JSONResponse({'detail': 'Слишком много запросов. Попробуйте позже'}, status_code=429,
                                headers={'Retry-After': '60'})
    origin = request.headers.get('origin')
    if request.method in ('POST', 'PATCH', 'DELETE', 'PUT') and origin and origin not in settings.allowed_origins:
        return JSONResponse({'detail': 'Недопустимый источник запроса'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = (
        'no-referrer' if '/public/payments/' in request.url.path or '/public/invoices/' in request.url.path or '/oidc/' in request.url.path else 'same-origin'
    )
    if request.url.path.startswith('/api/v1/public/invoices/'):
        response.headers['X-Robots-Tag'] = 'noindex, nofollow'
    return response


app.include_router(payment_access_router)
app.include_router(public_invoice_router)
app.include_router(system.router)
app.include_router(auth.router)
app.include_router(oidc.router)
app.include_router(users.router)
app.include_router(payments.router)
app.include_router(one_time_router.router)
app.include_router(integrations.router)
app.include_router(occurrences.router)
app.include_router(reports_router.router)
app.include_router(channels.router)
app.include_router(groups.router)
app.include_router(notifications.router)
app.include_router(attachments.router)
app.include_router(receipts.router)
app.include_router(audit.router)
if settings.b2b_enabled:
    app.include_router(b2b.router)
    app.include_router(incoming_invoices.router)
    app.include_router(incoming_invoices.admin_router)
