"""Add completed_at column to caio_event_decisions.

When Pedro approves a Caio Think Loop event he records intent. Acting on
that intent (writing the WhatsApp reply, merging the PR, etc.) happens in
the real world; this column lets the Cockpit UI distinguish "To Do" from
"Done" for approved items. Always nullable: an approval without follow-up
stays at ``None`` indefinitely, and rejected rows never set it.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-05-23 21:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c2d3e4f5a6b7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "caio_event_decisions",
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("caio_event_decisions", "completed_at")
