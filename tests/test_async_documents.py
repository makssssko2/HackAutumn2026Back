import asyncio
import hashlib
import io
import struct

import pytest
from fastapi import HTTPException, UploadFile

from app.core.config import settings
from app.modules.b2b.documents import ClamAVScanError, document_path, save_upload, scan_document

pytestmark = pytest.mark.asyncio


async def test_upload_is_persisted_and_missing_document_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'data_dir', tmp_path)
    monkeypatch.setattr(settings, 'b2b_clamav_enabled', False)
    content = b'%PDF-1.7\nlocal document'
    upload = UploadFile(io.BytesIO(content), filename='invoice.pdf')
    name, storage, content_type, size, checksum, status = await save_upload(upload, 0)
    path = await document_path(storage)
    assert path.read_bytes() == content
    assert (name, content_type, size, checksum, status) == (
        'invoice.pdf', 'application/pdf', len(content), hashlib.sha256(content).hexdigest(), 'clean',
    )
    with pytest.raises(HTTPException) as missing:
        await document_path('missing.pdf')
    assert missing.value.status_code == 404
    with pytest.raises(HTTPException) as traversal:
        await document_path('../outside.pdf')
    assert traversal.value.status_code == 404


@pytest.mark.parametrize('reply, expected', [(b'stream: OK\0', 'clean'), (b'stream: Eicar FOUND\0', 'rejected'), (b'stream: ERROR\0', None)])
async def test_clamav_streams_file_and_reads_fragmented_response(tmp_path, monkeypatch, reply, expected):
    content = b'x' * 70000
    path = tmp_path / 'invoice.pdf'
    path.write_bytes(content)
    received = bytearray()
    command = []
    finished = asyncio.Event()

    async def scanner(reader, writer):
        try:
            command.append(await reader.readuntil(b'\0'))
            while size := struct.unpack('!I', await reader.readexactly(4))[0]:
                received.extend(await reader.readexactly(size))
            writer.write(reply[:4])
            await writer.drain()
            await asyncio.sleep(0)
            writer.write(reply[4:])
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()

    async with await asyncio.start_server(scanner, '127.0.0.1', 0) as server:
        monkeypatch.setattr(settings, 'b2b_clamav_host', '127.0.0.1')
        monkeypatch.setattr(settings, 'b2b_clamav_port', server.sockets[0].getsockname()[1])
        if expected is None:
            with pytest.raises(ClamAVScanError):
                await scan_document(path)
        else:
            assert await scan_document(path) == expected
        await asyncio.wait_for(finished.wait(), timeout=2)
    assert command == [b'zINSTREAM\0']
    assert received == content


async def test_clamav_wait_is_nonblocking_and_cancellation_closes_connection(tmp_path, monkeypatch):
    path = tmp_path / 'invoice.pdf'
    path.write_bytes(b'example')
    waiting = asyncio.Event()
    closed = asyncio.Event()

    async def scanner(reader, writer):
        try:
            await reader.readuntil(b'\0')
            while size := struct.unpack('!I', await reader.readexactly(4))[0]:
                await reader.readexactly(size)
            waiting.set()
            assert await reader.read() == b''
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()

    async with await asyncio.start_server(scanner, '127.0.0.1', 0) as server:
        monkeypatch.setattr(settings, 'b2b_clamav_host', '127.0.0.1')
        monkeypatch.setattr(settings, 'b2b_clamav_port', server.sockets[0].getsockname()[1])
        task = asyncio.create_task(scan_document(path))
        try:
            await asyncio.wait_for(waiting.wait(), timeout=2)
            assert not task.done()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await asyncio.wait_for(closed.wait(), timeout=2)
