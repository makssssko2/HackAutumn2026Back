from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.security import current_user
from app.shared.formatters import public
from app.modules.payments.service import audit
from app.models import Attachment, Occurrence, User, uid
from app.shared.repository import get_or_404
from app.schemas import LinkIn

router = APIRouter(tags=['Attachments'])

prefix = '/api/v1/payment-occurrences'


@router.post(prefix + '/{occurrence_id}/attachments', status_code=201)
async def upload_attachment(
    occurrence_id: str,
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    get_or_404(db, Occurrence, occurrence_id)
    allowed = {
        '.pdf': 'application/pdf',
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.txt': 'text/plain',
    }
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
    row = Attachment(
        occurrence_id=occurrence_id,
        name=Path(file.filename.replace('\\', '/')).name[:240],
        storage_name=storage_name,
        content_type=allowed[suffix],
    )
    try:
        db.add(row)
        db.flush()
        audit(db, user.login, 'attachment.created', occurrence_id, name=row.name)
        db.commit()
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return public(row, ('storage_name',))


@router.post(prefix + '/{occurrence_id}/links', status_code=201)
def attach_link(
    occurrence_id: str,
    data: LinkIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    get_or_404(db, Occurrence, occurrence_id)
    parsed = urlparse(data.url)
    if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(422, 'Нужна ссылка HTTP(S) без учетных данных')
    row = Attachment(occurrence_id=occurrence_id, name=data.name, url=data.url)
    db.add(row)
    audit(db, user.login, 'attachment.linked', occurrence_id, name=data.name)
    db.commit()
    return public(row, ('storage_name',))


@router.get('/api/v1/attachments/{attachment_id}/download')
def download_attachment(attachment_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = get_or_404(db, Attachment, attachment_id)
    if not row.storage_name:
        raise HTTPException(404, 'Это ссылка, а не файл')
    path = settings.data_dir / 'attachments' / row.storage_name
    if not path.is_file():
        raise HTTPException(404, 'Файл недоступен')
    return FileResponse(path, media_type=row.content_type, filename=row.name)
