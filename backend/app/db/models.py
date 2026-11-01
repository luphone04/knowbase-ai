from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Stable constraint names, so Alembic autogenerate produces reviewable diffs.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for all ORM models. Tables arrive from Milestone 2 onward."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
