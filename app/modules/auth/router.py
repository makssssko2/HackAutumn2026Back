from app.modules.auth import service as operations
from app.modules.auth.service import login_attempts as login_attempts  # compatibility export

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession as Session

from app.core.db import get_db
from app.core.config import settings
from app.modules.auth.dependencies import current_user
from app.modules.users.presentation import user_out
from app.modules.users.models import User
from app.modules.auth.schemas import LoginIn, PasswordChange
from app.modules.users.schemas import ProfilePatch

router = APIRouter(prefix='/api/v1/auth', tags=['Auth'])


def set_session_cookie(response: Response, token: str, max_age: int = 604800):
    response.set_cookie('ik_session', token, httponly=True, secure=settings.cookie_secure,
                        samesite='lax', max_age=max_age, path='/api')


@router.post('/login')
async def login(data: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    user, token = await operations.login(data, request.client.host if request.client else 'local', db)
    set_session_cookie(response, token)
    return user_out(user)


@router.get('/me')
async def me(user: User = Depends(current_user)):
    return user_out(user)


@router.patch('/me')
async def update_me(data: ProfilePatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return await operations.update_me(data, user, db)


@router.post('/password')
async def change_password(data: PasswordChange, response: Response,
                    user: User = Depends(current_user), db: Session = Depends(get_db)):
    result = await operations.change_password(data, user, db)
    response.delete_cookie('ik_session', path='/api')
    return result


@router.post('/logout')
async def logout(request: Request, response: Response, user: User = Depends(current_user), db: Session = Depends(get_db)):
    result = await operations.logout(request.cookies['ik_session'], user, db)
    response.delete_cookie('ik_session', path='/api')
    return result
