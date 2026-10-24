from app.core import bootstrap as implementation

SessionLocal = implementation.SessionLocal
settings = implementation.settings


def bootstrap():
    implementation.SessionLocal = SessionLocal
    implementation.bootstrap()


__all__ = ['SessionLocal', 'bootstrap', 'settings']
