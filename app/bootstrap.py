from app.core import bootstrap as implementation

SessionLocal = implementation.SessionLocal
settings = implementation.settings


async def bootstrap():
    implementation.SessionLocal = SessionLocal
    (await implementation.bootstrap())


__all__ = ['SessionLocal', 'bootstrap', 'settings']
