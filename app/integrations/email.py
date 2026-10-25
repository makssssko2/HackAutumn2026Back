"""SMTP transport shared by ordinary and B2B notifications."""
import asyncio
import ssl

import aiosmtplib

from app.core.config import settings


async def send_email(message):
    # Loading the OS trust store reads files; only this unavoidable synchronous
    # library boundary is offloaded, while SMTP itself uses native async sockets.
    tls_context = await asyncio.to_thread(ssl.create_default_context)
    # Explicit start_tls preserves the configured mode; implicit TLS takes priority.
    async with aiosmtplib.SMTP(hostname=settings.smtp_host, port=settings.smtp_port,
                              use_tls=settings.smtp_ssl,
                              start_tls=settings.smtp_starttls and not settings.smtp_ssl,
                              tls_context=tls_context, timeout=15) as smtp:
        try:
            if settings.smtp_username:
                await smtp.login(settings.smtp_username, settings.smtp_password)
            await smtp.send_message(message)
        except asyncio.CancelledError:
            smtp.close()
            raise
