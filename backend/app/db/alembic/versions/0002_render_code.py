"""render code on chart artefacts.

A chart artefact is SQL + the Python that turns that SQL's DataFrame into a
figure.  Storing the render code lets a dashboard refresh re-render the chart
from fresh, viewer-scoped rows with zero model calls (Section 9.1).

Revision ID: 0002_render_code
Revises: 0001_initial
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_render_code"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("turn_chart", "saved_chart"):
        op.add_column(table, sa.Column("render_code", sa.Text(), nullable=True))
        op.add_column(table, sa.Column("dataset_name", sa.Text(), nullable=False, server_default="df"))


def downgrade() -> None:
    for table in ("turn_chart", "saved_chart"):
        op.drop_column(table, "dataset_name")
        op.drop_column(table, "render_code")
