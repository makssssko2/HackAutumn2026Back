"""Domain-neutral identifiers and UTC timestamps for persistence."""
import uuid
from datetime import datetime, timezone


def uid():
    return str(uuid.uuid4())


def utcnow():
    return datetime.now(timezone.utc)
