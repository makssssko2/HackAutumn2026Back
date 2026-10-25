from sqlalchemy.orm import Session

from app.models import Occurrence, Receipt


def get_receipt_occurrence(db: Session, receipt: Receipt) -> Occurrence | None:
    return db.get(Occurrence, receipt.occurrence_id)
