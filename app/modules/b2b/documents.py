from anyio import Path as AsyncPath
import asyncio
import struct
import hashlib
from pathlib import Path

from fastapi import HTTPException, UploadFile

from app.core.config import settings
from app.shared.model_utils import uid


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
    content = (await file.read(settings.b2b_document_max_bytes + 1))
    if not content or len(content) > settings.b2b_document_max_bytes or total_size + len(content) > settings.b2b_document_max_total_bytes:
        raise HTTPException(413, 'Document size limit exceeded')
    content_type, signature = allowed
    if not content.startswith(signature):
        raise HTTPException(415, 'Document signature does not match its extension')
    storage_dir = settings.incoming_invoice_storage_dir
    await AsyncPath(storage_dir).mkdir(parents=True, exist_ok=True)
    storage_name = uid() + suffix
    await AsyncPath(storage_dir / storage_name).write_bytes(content)
    scan_status = 'pending' if settings.b2b_clamav_enabled else 'clean'
    return original_name, storage_name, content_type, len(content), hashlib.sha256(content).hexdigest(), scan_status


async def document_path(storage_name: str) -> Path:
    path = settings.incoming_invoice_storage_dir / storage_name
    if path.parent != settings.incoming_invoice_storage_dir or not await AsyncPath(path).is_file():
        raise HTTPException(404, 'Document file is unavailable')
    return path


class ClamAVScanError(Exception):
    pass


async def scan_document(path: Path) -> str:
    """Stream one document to ClamAV without blocking other delivery lanes."""
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(settings.b2b_clamav_host, settings.b2b_clamav_port, limit=4096),
            timeout=10,
        )
        writer.write(b'zINSTREAM\0')
        await asyncio.wait_for(writer.drain(), timeout=30)
        async with await AsyncPath(path).open('rb') as document:
            while chunk := await document.read(65536):
                writer.write(struct.pack('!I', len(chunk)) + chunk)
                await asyncio.wait_for(writer.drain(), timeout=30)
        writer.write(b'\0\0\0\0')
        await asyncio.wait_for(writer.drain(), timeout=30)
        response = await asyncio.wait_for(reader.readuntil(b'\0'), timeout=30)
        if len(response) > 4096:
            raise ClamAVScanError('unexpected_response')
    except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as error:
        raise ClamAVScanError(type(error).__name__) from error
    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=10)
            except (OSError, TimeoutError):
                pass
    if response.endswith(b' OK\0'):
        return 'clean'
    if b' FOUND\0' in response:
        return 'rejected'
    raise ClamAVScanError('unexpected_response')
