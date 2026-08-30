import uuid

from sqlalchemy import ARRAY, ForeignKey, Integer, Text, TIMESTAMP, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

DEFAULT_GRADE_FILTER = ["sahih", "hasan", "da_if", "mawdu"]


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str | None] = mapped_column(Text, unique=True)
    preferred_language: Mapped[str] = mapped_column(Text, default="en")
    hadith_grade_filter: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=DEFAULT_GRADE_FILTER
    )
    created_at: Mapped[object] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class QueryHistory(Base):
    __tablename__ = "query_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    language_code: Mapped[str | None] = mapped_column(Text)
    retrieved_source_ids: Mapped[dict | None] = mapped_column(JSONB)
    answer_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[object] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class Bookmark(Base):
    __tablename__ = "bookmarks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    source_type: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[object] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class Flag(Base):
    __tablename__ = "flags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    query_history_id: Mapped[int | None] = mapped_column(ForeignKey("query_history.id"))
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="open")
    reviewer_notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[object] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
