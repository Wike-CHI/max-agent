"""Exact decimal storage on both SQLite and PostgreSQL."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Numeric, String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine


class ExactDecimal(TypeDecorator[Decimal]):
    """SQLite stores canonical text; PostgreSQL uses native NUMERIC.

    SQLite's NUMERIC affinity converts decimal input to binary float, so a
    plain mapped_column(Numeric) cannot round-trip a billing amount exactly.
    """

    impl = String
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Numeric(36, 18))
        return dialect.type_descriptor(String(80))

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> str | Decimal | None:
        if value is None:
            return None
        amount = Decimal(str(value))
        return amount if dialect.name == "postgresql" else format(amount, "f")

    def process_result_value(self, value: object, dialect: Dialect) -> Decimal | None:
        return Decimal(str(value)) if value is not None else None
