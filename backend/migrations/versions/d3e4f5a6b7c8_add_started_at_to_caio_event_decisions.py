"""Add started_at column to caio_event_decisions.

Approved Caio actions move To Do -> In Progress -> Done. ``started_at`` is
written by Caio's own runtime when it picks the action off the queue;
``completed_at`` is written when Caio finishes it. Both nullable: rejected
rows and to-do rows leave them empty.

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-05-23 21:50:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d3e4f5a6b7c8"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "caio_event_decisions",
        sa.Column("started_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("caio_event_decisions", "started_at")
