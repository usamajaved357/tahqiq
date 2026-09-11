"""switch embeddings to halfvec 384 dims

Revision ID: f16acd0831a5
Revises: b39066af023b
Create Date: 2026-09-08 23:53:35.227536

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f16acd0831a5'
down_revision: Union[str, Sequence[str], None] = 'b39066af023b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # embeddings table is empty (Phase 2 hasn't populated it yet), so this is
    # a clean type swap, not a data migration: vector(1024) -> halfvec(384),
    # switching to multilingual-e5-small to cut storage ~5x (see Phase 2
    # sizing discussion in project notes).
    op.execute("DROP INDEX IF EXISTS embeddings_embedding_idx")
    op.execute("ALTER TABLE embeddings ALTER COLUMN embedding TYPE halfvec(384)")
    op.execute("CREATE INDEX ON embeddings USING hnsw (embedding halfvec_cosine_ops)")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS embeddings_embedding_idx")
    op.execute("ALTER TABLE embeddings ALTER COLUMN embedding TYPE vector(1024)")
    op.execute("CREATE INDEX ON embeddings USING hnsw (embedding vector_cosine_ops)")
