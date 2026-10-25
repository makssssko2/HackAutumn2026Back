import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.rate_limit import SlidingWindowLimiter
from app.modules.auth.dependencies import admin_user
from app.modules.auth.router import set_session_cookie
from app.modules.oidc import client, native, service
from app.modules.oidc.models import OIDCProvider
from app.modules.oidc.schemas import HandoffIn, ProviderIn, ProviderOut
from app.modules.users.models import User

class OIDCRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def safe_handler(request):
            try:
                return await handler(request)
            except RequestValidationError:
                # Never echo native codes/verifiers or provider secrets from malformed input.
                raise HTTPException(422, 'Некорректные параметры OIDC') from None
        return safe_handler


router = APIRouter(prefix='/api/v1/oidc', tags=['OIDC'], route_class=OIDCRoute)
limiter = SlidingWindowLimiter(20, 60)
logger = logging.getLogger(__name__)
COOKIE_PATH = '/api/v1/oidc'


@router.get('/provider', response_model=ProviderOut)
async def get_provider(user: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    return service.provider_out(await db.get(OIDCProvider, 1))


@router.put('/provider', response_model=ProviderOut)
async def put_provider(data: ProviderIn, user: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    return await service.save_provider(db, data, user)


@router.post('/provider/test')
async def test_provider(user: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    provider = await db.get(OIDCProvider, 1)
    if not provider or not provider.client_id:
        raise HTTPException(400, 'Сначала сохраните настройки провайдера')
    try:
        metadata, _ = await client.read_discovery(provider.discovery_url)
        if provider.enabled and metadata['issuer'] != provider.issuer:
            raise client.OIDCError('issuer_changed')
    except Exception:
        logger.warning('OIDC discovery test failed')
        raise HTTPException(400, 'Не удалось проверить discovery/JWKS провайдера') from None
    return {'ok': True, 'issuer': metadata['issuer'],
            'message': 'Discovery и JWKS доступны. Учётные данные проверяются при входе.'}


@router.get('/public')
async def public_provider(db: AsyncSession = Depends(get_db)):
    provider = await db.get(OIDCProvider, 1)
    return {'enabled': bool(provider and provider.enabled),
            'name': provider.name if provider else 'Корпоративный вход'}


def check_rate(request):
    if not limiter.allow(request.client.host if request.client else 'unknown'):
        raise HTTPException(429, 'Слишком много попыток входа. Попробуйте позже')


def failed_login():
    response = RedirectResponse(settings.public_frontend_url + '/login?oidc_error=1', status_code=303)
    response.delete_cookie('ik_oidc', path=COOKIE_PATH)
    return response


@router.get('/login')
async def login(request: Request, db: AsyncSession = Depends(get_db)):
    check_rate(request)
    try:
        url, browser = await service.start_login(db)
    except Exception as error:
        await db.rollback()
        logger.warning('OIDC login denied: %s', error.args[0] if isinstance(error, client.OIDCError) else type(error).__name__)
        return failed_login()
    response = RedirectResponse(url, status_code=303)
    response.set_cookie('ik_oidc', browser, httponly=True, secure=settings.cookie_secure,
                        samesite='lax', max_age=300, path=COOKIE_PATH)
    return response


@router.get('/callback')
async def callback(request: Request, db: AsyncSession = Depends(get_db)):
    check_rate(request)
    try:
        params = request.query_params
        if any(len(params.getlist(key)) > 1 for key in ('state', 'code', 'error')):
            raise client.OIDCError('duplicate_parameter')
        result = await service.finish_login(db, params.get('state'), request.cookies.get('ik_oidc'),
                                            params.get('code'), error='error' in params)
    except Exception as error:
        await db.rollback()
        logger.warning('OIDC callback denied: %s', error.args[0] if isinstance(error, client.OIDCError) else type(error).__name__)
        return failed_login()
    if isinstance(result, native.NativeRedirect):
        response = RedirectResponse(result.url, status_code=303)
        response.delete_cookie('ik_oidc', path=COOKIE_PATH)
        return response
    token, seconds = result
    response = RedirectResponse(settings.public_frontend_url + '/', status_code=303)
    response.delete_cookie('ik_oidc', path=COOKIE_PATH)
    set_session_cookie(response, token, seconds)
    return response


@router.get('/mcp/login')
async def mcp_login(request: Request, db: AsyncSession = Depends(get_db)):
    check_rate(request)
    params = request.query_params
    try:
        if set(params) != {'redirect_uri', 'code_challenge', 'state'} or any(len(params.getlist(k)) != 1 for k in params):
            raise client.OIDCError('invalid_native_parameters')
        fields = native.validate_login(params['redirect_uri'], params['code_challenge'], params['state'])
        url, browser = await service.start_login(db, fields)
    except Exception:
        await db.rollback()
        logger.warning('OIDC native login denied')
        return failed_login()
    response = RedirectResponse(url, status_code=303)
    response.set_cookie('ik_oidc', browser, httponly=True, secure=settings.cookie_secure,
                        samesite='lax', max_age=300, path=COOKIE_PATH)
    return response


@router.post('/mcp/exchange')
async def mcp_exchange(data: HandoffIn, request: Request, response: Response,
                       db: AsyncSession = Depends(get_db)):
    check_rate(request)
    try:
        token, seconds = await service.exchange_handoff(db, data)
    except Exception:
        await db.rollback()
        logger.warning('OIDC native exchange denied')
        raise HTTPException(401, 'Не удалось завершить корпоративный вход') from None
    set_session_cookie(response, token, seconds)
    return {'ok': True}
