"""fix tafsirs unique constraint to include language_code

Revision ID: b39066af023b
Revises: 58364a03e783
Create Date: 2026-08-30 06:05:56.800363

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b39066af023b'
down_revision: Union[str, Sequence[str], None] = '58364a03e783'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint("uq_tafsirs_ayah_id_source_name", "tafsirs", type_="unique")
    op.create_unique_constraint(
        "uq_tafsirs_ayah_id_source_name_language_code",
        "tafsirs",
        ["ayah_id", "source_name", "language_code"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("uq_tafsirs_ayah_id_source_name_language_code", "tafsirs", type_="unique")
    op.create_unique_constraint("uq_tafsirs_ayah_id_source_name", "tafsirs", ["ayah_id", "source_name"])
