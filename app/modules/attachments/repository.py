from sqlalchemy.orm import Session

from app.models import Attachment


def get_attachment(db: Session, aid: str) -> Attachment | None:
    return db.get(Attachment, aid)
