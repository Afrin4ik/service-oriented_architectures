from pathlib import Path

from alembic import op

revision = "flight_001"
down_revision = None


def upgrade():
    op.execute(Path(__file__).with_suffix(".sql").read_text())


def downgrade():
    raise NotImplementedError("Use a database backup to revert this migration")
