import hashlib
from pathlib import Path

from fastapi import HTTPException, UploadFile

from app.core.config import settings
from app.models import uid


ALLOWED_FILES = {
    '.pdf': ('application/pdf', b'%PDF-'),
    '.png': ('image/png', b'\x89PNG\r\n\x1a\n'),
    '.jpg': ('image/jpeg', b'\xff\xd8\xff'),
    '.jpeg': ('image/jpeg', b'\xff\xd8\xff'),
}


async def save_upload(file: UploadFile, total_size: int) -> tuple[str, str, str, int, str, str]:
    original_name = Path((file.filename or '').replace('\\', '/')).name[:240]
    suffix = Path(original_name).suffix.lower()
    allowed = ALLOWED_FILES.get(suffix)
    if allowed is None:
        raise HTTPException(415, 'Only PDF, PNG, and JPEG documents are supported')
    content = await file.read(settings.b2b_document_max_bytes + 1)
    if not content or len(content) > settings.b2b_document_max_bytes or total_size + len(content) > settings.b2b_document_max_total_bytes:
        raise HTTPException(413, 'Document size limit exceeded')
    content_type, signature = allowed
    if not content.startswith(signature):
        raise HTTPException(415, 'Document signature does not match its extension')
    storage_dir = settings.incoming_invoice_storage_dir
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage_name = uid() + suffix
    (storage_dir / storage_name).write_bytes(content)
    scan_status = 'pending' if settings.b2b_clamav_enabled else 'clean'
    return original_name, storage_name, content_type, len(content), hashlib.sha256(content).hexdigest(), scan_status


def document_path(storage_name: str) -> Path:
    path = settings.incoming_invoice_storage_dir / storage_name
    if path.parent != settings.incoming_invoice_storage_dir or not path.is_file():
        raise HTTPException(404, 'Document file is unavailable')
    return path
