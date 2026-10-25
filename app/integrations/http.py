"""HTTP transport construction with native asynchronous requests."""
import asyncio

import httpx


async def create_http_client(**kwargs) -> httpx.AsyncClient:
    # HTTPX loads CA certificates and environment configuration synchronously
    # during construction. Only this boundary runs outside the event loop;
    # callers own the client context and await all network operations.
    return await asyncio.to_thread(httpx.AsyncClient, **kwargs)
