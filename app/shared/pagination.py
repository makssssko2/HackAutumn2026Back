from dataclasses import dataclass

from fastapi import Query
from sqlalchemy import String, func, or_, select


class PageRequest:
    def __init__(self, page: int | None = Query(None, ge=1), page_size: int | None = Query(None, ge=1, le=100)):
        self.requested = page is not None or page_size is not None
        self.page = page or 1
        self.page_size = page_size or 12


@dataclass
class PageRows:
    items: list
    total: int
    params: PageRequest

    def response(self, items: list, **metadata):
        if not self.params.requested:
            return items
        return {'items': items, 'total': self.total, 'page': self.params.page, 'page_size': self.params.page_size, **metadata}


async def fetch_page(db, statement, params: PageRequest, *, scalar=True, legacy_limit=None):
    total = 0
    if params.requested:
        total = (await db.scalar(select(func.count()).select_from(statement.order_by(None).subquery())))
        statement = statement.offset((params.page - 1) * params.page_size).limit(params.page_size)
    elif legacy_limit:
        statement = statement.limit(legacy_limit)
    rows = list((await db.scalars(statement)) if scalar else (await db.execute(statement)))
    return PageRows(rows, total if params.requested else len(rows), params)


async def collection_page(db, model, params, *, conditions=(), order_by=(), q=None, fields=(), search_extra=(), legacy_limit=None):
    statement = select(model).where(*conditions)
    if q:
        statement = statement.where(or_(*(field.cast(String).icontains(q, autoescape=True) for field in fields), *search_extra))
    return (await fetch_page(db, statement.order_by(*order_by, model.id), params, legacy_limit=legacy_limit))
