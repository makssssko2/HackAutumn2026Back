from fastapi import APIRouter, Depends
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse

from app.core.config import settings, today
from app.modules.auth.dependencies import current_user
from app.modules.users.models import User

router = APIRouter(tags=['System'])

prefix = '/api/v1'


@router.get('/health')
async def health():
    return {'status': 'ok'}


@router.get(prefix + '/settings')
async def app_settings(user: User = Depends(current_user)):
    return {
        'timezone': settings.app_timezone,
        'today': today(),
        'demo_mode': True,
        'password_change_enabled': settings.password_change_enabled,
        'telegram_configured': bool(settings.telegram_bot_token),
        'smtp_configured': bool(settings.smtp_host),
    }


@router.get(prefix + '/docs', include_in_schema=False)
async def swagger(user: User = Depends(current_user)):
    return get_swagger_ui_html(openapi_url=prefix + '/openapi.json', title='Invoice Keeper · API')


@router.get(prefix + '/openapi.json', include_in_schema=False)
async def openapi(user: User = Depends(current_user)):
    # Import here to avoid circular reference at module load time.
    from app.main import app as _app  # noqa: PLC0415
    return JSONResponse(_app.openapi())
