from contextlib import contextmanager
from typing import Iterable

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.db import HadithSessionLocal, SessionLocal


@contextmanager
def db_session():
    """Session against the primary DB (Supabase) — Quran, tafsir, users, embeddings."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def hadith_db_session():
    """Session against the Hadith-domain DB (Neon) — hadiths, translations,
    gradings, books, collections. See app/core/db.py for why these are split."""
    session = HadithSessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def upsert_one(session: Session, model, values: dict, index_elements: list[str]) -> int:
    """Insert a row or update it in place by unique key; return its id."""
    table = model.__table__
    stmt = pg_insert(table).values(**values)
    non_index = {k: v for k, v in values.items() if k not in index_elements}
    set_ = {k: stmt.excluded[k] for k in non_index} or {
        index_elements[0]: stmt.excluded[index_elements[0]]
    }
    stmt = stmt.on_conflict_do_update(index_elements=index_elements, set_=set_).returning(
        table.c.id
    )
    return session.execute(stmt).scalar_one()


def upsert_many(
    session: Session, model, rows: Iterable[dict], index_elements: list[str]
) -> None:
    """Bulk insert/update, skipping the RETURNING round-trip — use when callers
    don't need the generated ids back."""
    rows = list(rows)
    if not rows:
        return
    table = model.__table__
    stmt = pg_insert(table).values(rows)
    sample = rows[0]
    non_index = {k for k in sample if k not in index_elements}
    set_ = {k: stmt.excluded[k] for k in non_index} or {
        index_elements[0]: stmt.excluded[index_elements[0]]
    }
    stmt = stmt.on_conflict_do_update(index_elements=index_elements, set_=set_)
    session.execute(stmt)


def upsert_many_returning(
    session: Session,
    model,
    rows: Iterable[dict],
    index_elements: list[str],
    chunk_size: int = 1000,
) -> list:
    """Bulk insert/update in chunks, returning (id, *index_elements) for every
    affected row — one round-trip per chunk instead of one per row."""
    rows = list(rows)
    if not rows:
        return []
    table = model.__table__
    returning_cols = [table.c.id] + [table.c[e] for e in index_elements]
    results = []
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i : i + chunk_size]
        stmt = pg_insert(table).values(chunk)
        non_index = {k for k in chunk[0] if k not in index_elements}
        set_ = {k: stmt.excluded[k] for k in non_index} or {
            index_elements[0]: stmt.excluded[index_elements[0]]
        }
        stmt = stmt.on_conflict_do_update(index_elements=index_elements, set_=set_).returning(
            *returning_cols
        )
        results.extend(session.execute(stmt).all())
    return results
