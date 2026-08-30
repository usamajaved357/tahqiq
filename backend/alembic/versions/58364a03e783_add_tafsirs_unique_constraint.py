"""add tafsirs unique constraint

Revision ID: 58364a03e783
Revises: c862ad9937af
Create Date: 2026-08-30 05:38:46.558439

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '58364a03e783'
down_revision: Union[str, Sequence[str], None] = 'c862ad9937af'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_unique_constraint("uq_tafsirs_ayah_id_source_name", "tafsirs", ["ayah_id", "source_name"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("uq_tafsirs_ayah_id_source_name", "tafsirs", type_="unique")
