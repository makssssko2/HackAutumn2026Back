from sqlalchemy.ext.asyncio import AsyncSession as Session

from app.modules.invoices.models import Occurrence
from app.modules.receipts.models import Receipt


async def get_receipt_occurrence(db: Session, receipt: Receipt) -> Occurrence | None:
    return (await db.get(Occurrence, receipt.occurrence_id))
