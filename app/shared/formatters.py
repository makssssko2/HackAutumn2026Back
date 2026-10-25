"""Presentation-level formatters: convert ORM rows to API dicts."""


def public(model, exclude: tuple = ()) -> dict:
    return {
        column.name: getattr(model, column.name)
        for column in model.__table__.columns
        if column.name not in exclude
    }


from app.modules.users.presentation import user_out  # compatibility export
