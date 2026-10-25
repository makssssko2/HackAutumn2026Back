from anyio import Path as AsyncPath
from pathlib import Path
from urllib.parse import urlparse
from fastapi import HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession as Session
from app.core.config import settings
from app.shared.formatters import public
from app.modules.audit.service import audit
from app.modules.attachments.models import Attachment
from app.modules.invoices.models import Occurrence
from app.modules.users.models import User
from app.shared.model_utils import uid
from app.shared.repository import get_or_404
from app.modules.attachments.schemas import LinkIn


async def upload_attachment(occurrence_id: str, file: UploadFile, user: User, db: Session):
    (await get_or_404(db, Occurrence, occurrence_id))
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
    content = (await file.read(10 * 1024 * 1024 + 1))
    if not content or len(content) > 10 * 1024 * 1024:
        raise HTTPException(422, 'Размер файла: от 1 байта до 10 МБ')
    signatures = {'.pdf': b'%PDF-', '.png': b'\x89PNG\r\n\x1a\n', '.jpg': b'\xff\xd8\xff', '.jpeg': b'\xff\xd8\xff'}
    if suffix in signatures and not content.startswith(signatures[suffix]):
        raise HTTPException(422, 'Содержимое файла не соответствует формату')
    directory = settings.data_dir / 'attachments'
    (await AsyncPath(directory).mkdir(parents=True, exist_ok=True))
    storage_name = uid() + suffix
    path = directory / storage_name
    (await AsyncPath(path).write_bytes(content))
    row = Attachment(
        occurrence_id=occurrence_id,
        name=Path(file.filename.replace('\\', '/')).name[:240],
        storage_name=storage_name,
        content_type=allowed[suffix],
    )
    try:
        db.add(row)
        (await db.flush())
        audit(db, user.login, 'attachment.created', occurrence_id, name=row.name)
        (await db.commit())
    except Exception:
        (await AsyncPath(path).unlink(missing_ok=True))
        raise
    return public(row, ('storage_name',))


async def attach_link(occurrence_id: str, data: LinkIn, user: User, db: Session):
    (await get_or_404(db, Occurrence, occurrence_id))
    parsed = urlparse(data.url)
    if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(422, 'Нужна ссылка HTTP(S) без учетных данных')
    row = Attachment(occurrence_id=occurrence_id, name=data.name, url=data.url)
    db.add(row)
    audit(db, user.login, 'attachment.linked', occurrence_id, name=data.name)
    (await db.commit())
    return public(row, ('storage_name',))
