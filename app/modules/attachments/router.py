from app.modules.attachments import service as operations
from anyio import Path as AsyncPath

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession as Session

from app.core.config import settings
from app.core.db import get_db
from app.modules.auth.dependencies import current_user
from app.modules.attachments.models import Attachment
from app.modules.users.models import User
from app.shared.repository import get_or_404
from app.modules.attachments.schemas import LinkIn

router = APIRouter(tags=['Attachments'])

prefix = '/api/v1/payment-occurrences'


@router.post(prefix + '/{occurrence_id}/attachments', status_code=201)
async def upload_attachment(
    occurrence_id: str,
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    return await operations.upload_attachment(occurrence_id, file, user, db)


@router.post(prefix + '/{occurrence_id}/links', status_code=201)
async def attach_link(
    occurrence_id: str,
    data: LinkIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    return await operations.attach_link(occurrence_id, data, user, db)


@router.get('/api/v1/attachments/{attachment_id}/download')
async def download_attachment(attachment_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = (await get_or_404(db, Attachment, attachment_id))
    if not row.storage_name:
        raise HTTPException(404, 'Это ссылка, а не файл')
    path = settings.data_dir / 'attachments' / row.storage_name
    if not (await AsyncPath(path).is_file()):
        raise HTTPException(404, 'Файл недоступен')
    return FileResponse(path, media_type=row.content_type, filename=row.name)
