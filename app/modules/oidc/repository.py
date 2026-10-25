from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.oidc.models import OIDCProvider, OIDCPendingLogin


async def lock_provider(db: AsyncSession):
    """Serialize config changes, login finalization and queue admission on both supported DBs.

    The no-op UPDATE takes a PostgreSQL row lock / SQLite write lock until commit.
    Callers must finish remote I/O before entering this short transaction.
    """
    if db.bind.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    await db.execute(insert(OIDCProvider).values(id=1).on_conflict_do_nothing(index_elements=['id']))
    await db.execute(update(OIDCProvider).where(OIDCProvider.id == 1).values(revision=OIDCProvider.revision))
    return await db.scalar(select(OIDCProvider).where(OIDCProvider.id == 1).execution_options(populate_existing=True))


async def consume_pending(db, state_hash, browser_hash, now):
    row = await db.scalar(delete(OIDCPendingLogin).where(
        OIDCPendingLogin.state_hash == state_hash,
        OIDCPendingLogin.browser_hash == browser_hash,
        OIDCPendingLogin.expires_at > now,
    ).returning(OIDCPendingLogin))
    # A callback is single-use even when the IdP fails or the request is cancelled later.
    await db.commit()
    if row is not None:
        # RETURNING loads an ORM instance even though the row is deleted. Keep its
        # snapshot detached so the later read-transaction rollback cannot expire it.
        db.expunge(row)
    return row


async def pending_count(db, now):
    await db.execute(delete(OIDCPendingLogin).where(OIDCPendingLogin.expires_at <= now))
    return await db.scalar(select(func.count()).select_from(OIDCPendingLogin))
