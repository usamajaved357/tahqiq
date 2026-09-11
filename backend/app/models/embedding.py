from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

# multilingual-e5-small: 384 dims, stored as halfvec (16-bit float) instead of
# vector (32-bit float) to cut embeddings storage ~5x — see Phase 2 sizing
# discussion. Table is empty pre-Phase-2, so this is a clean type change, not
# a data migration.
EMBEDDING_DIM = 384


class Embedding(Base):
    __tablename__ = "embeddings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(HALFVEC(EMBEDDING_DIM))
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
