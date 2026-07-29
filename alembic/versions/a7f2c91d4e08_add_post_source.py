"""add posts.source

Facebook Page posts and Instagram media now both land in `posts`, so each row
has to say which surface it came from. Insights are computed per source and
never pooled — organic Page reach and Instagram reach are driven by different
ranking systems, so an engagement rate averaged over both describes neither.

Existing rows are all Instagram: the Facebook ingestion path did not exist
before this revision, so the backfill is exact rather than a guess.

Revision ID: a7f2c91d4e08
Revises: 12dbff86b659
Create Date: 2026-07-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7f2c91d4e08"
down_revision: Union[str, None] = "12dbff86b659"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default backfills existing rows in the same statement; it is then
    # dropped so the application layer stays the single source of the default
    # and a future source can't be silently mislabelled as instagram.
    op.add_column(
        "posts",
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=False,
            server_default="instagram",
        ),
    )
    op.alter_column("posts", "source", server_default=None)


def downgrade() -> None:
    op.drop_column("posts", "source")
