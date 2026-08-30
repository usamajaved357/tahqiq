"""initial schema

Revision ID: c862ad9937af
Revises:
Create Date: 2026-08-30 02:43:53.011381

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, UUID


# revision identifiers, used by Alembic.
revision: str = 'c862ad9937af'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMBEDDING_DIM = 1024


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ===== QURAN =====
    op.create_table(
        "surahs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("number", sa.Integer, nullable=False, unique=True),
        sa.Column("name_ar", sa.Text, nullable=False),
        sa.Column("name_en", sa.Text, nullable=False),
        sa.Column("revelation_type", sa.Text),
    )

    op.create_table(
        "ayahs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("surah_id", sa.Integer, sa.ForeignKey("surahs.id")),
        sa.Column("ayah_number", sa.Integer, nullable=False),
        sa.Column("text_ar", sa.Text, nullable=False),
        sa.Column("juz", sa.Integer),
        sa.Column("page", sa.Integer),
        sa.UniqueConstraint("surah_id", "ayah_number"),
    )

    op.create_table(
        "translations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ayah_id", sa.Integer, sa.ForeignKey("ayahs.id")),
        sa.Column("translator", sa.Text, nullable=False),
        sa.Column("language_code", sa.String(8), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.UniqueConstraint("ayah_id", "translator"),
    )

    op.create_table(
        "tafsirs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ayah_id", sa.Integer, sa.ForeignKey("ayahs.id")),
        sa.Column("source_name", sa.Text, nullable=False),
        sa.Column("language_code", sa.String(8), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
    )

    # ===== HADITH =====
    op.create_table(
        "hadith_collections",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("name_ar", sa.Text),
        sa.Column("total_hadith", sa.Integer),
    )

    op.create_table(
        "hadith_books",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("collection_id", sa.Integer, sa.ForeignKey("hadith_collections.id")),
        sa.Column("book_number", sa.Integer),
        sa.Column("name_en", sa.Text),
        sa.Column("name_ar", sa.Text),
        sa.UniqueConstraint("collection_id", "book_number"),
    )

    op.create_table(
        "hadiths",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("book_id", sa.Integer, sa.ForeignKey("hadith_books.id")),
        sa.Column("hadith_number", sa.Text, nullable=False),
        sa.Column("text_ar", sa.Text),
        sa.Column("narrator_chain", sa.Text),
        sa.Column("topic_tags", sa.ARRAY(sa.Text)),
        sa.Column("related_hadith_ids", sa.ARRAY(sa.Integer)),
        sa.Column("source_dataset", sa.Text, nullable=False),
        sa.Column("verification_status", sa.Text, server_default="unverified"),
        sa.UniqueConstraint("book_id", "hadith_number"),
    )

    op.create_table(
        "hadith_gradings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("hadith_id", sa.Integer, sa.ForeignKey("hadiths.id")),
        sa.Column("grader_name", sa.Text, nullable=False),
        sa.Column("grade", sa.Text, nullable=False),
        sa.Column("source_work", sa.Text),
        sa.Column("note", sa.Text),
        sa.UniqueConstraint("hadith_id", "grader_name"),
    )

    op.create_table(
        "hadith_translations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("hadith_id", sa.Integer, sa.ForeignKey("hadiths.id")),
        sa.Column("language_code", sa.String(8), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.UniqueConstraint("hadith_id", "language_code"),
    )

    # ===== USERS =====
    op.create_table(
        "users",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("email", sa.Text, unique=True),
        sa.Column("preferred_language", sa.Text, server_default="en"),
        sa.Column(
            "hadith_grade_filter",
            sa.ARRAY(sa.Text),
            server_default="{sahih,hasan,da_if,mawdu}",
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "query_history",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("query_text", sa.Text, nullable=False),
        sa.Column("language_code", sa.Text),
        sa.Column("retrieved_source_ids", JSONB),
        sa.Column("answer_text", sa.Text),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "bookmarks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("source_type", sa.Text),
        sa.Column("source_id", sa.Integer),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "flags",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("query_history_id", sa.Integer, sa.ForeignKey("query_history.id")),
        sa.Column("reason", sa.Text),
        sa.Column("status", sa.Text, server_default="open"),
        sa.Column("reviewer_notes", sa.Text),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )

    # ===== MONETIZATION =====
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("stripe_subscription_id", sa.Text),
        sa.Column("status", sa.Text),
        sa.Column("plan", sa.Text),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "donations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("stripe_payment_id", sa.Text),
        sa.Column("amount_cents", sa.Integer),
        sa.Column("currency", sa.Text),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )

    # ===== VECTOR SEARCH (pgvector) =====
    op.create_table(
        "embeddings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source_type", sa.Text, nullable=False),
        sa.Column("source_id", sa.Integer, nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM)),
        sa.Column("metadata", JSONB),
    )
    op.execute(
        "CREATE INDEX ON embeddings USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("embeddings")
    op.drop_table("donations")
    op.drop_table("subscriptions")
    op.drop_table("flags")
    op.drop_table("bookmarks")
    op.drop_table("query_history")
    op.drop_table("users")
    op.drop_table("hadith_translations")
    op.drop_table("hadith_gradings")
    op.drop_table("hadiths")
    op.drop_table("hadith_books")
    op.drop_table("hadith_collections")
    op.drop_table("tafsirs")
    op.drop_table("translations")
    op.drop_table("ayahs")
    op.drop_table("surahs")
