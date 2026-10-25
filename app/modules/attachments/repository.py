from sqlalchemy.ext.asyncio import AsyncSession as Session

from app.modules.attachments.models import Attachment


async def get_attachment(db: Session, aid: str) -> Attachment | None:
    return (await db.get(Attachment, aid))
