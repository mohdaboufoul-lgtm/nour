"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Conventions (DESIGN §5.3): table definitions are frozen in this file (never imported from
``nour.db.models``); every timestamp column is ``nour.db.base.UtcDateTime``; append-only,
single-transition and forward-only rules are DB triggers emitted by ``nour.db.engine.trigger_ddl``
and the Postgres roles / grants / RLS by ``nour.db.engine.pg_roles_ddl`` — the same generators
``create_schema`` runs for tests — so register every new wall marker in ``TABLE_FLAGS`` and every
per-role grant in ``GRANTS`` (``0001_initial`` shows the shape).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: str | Sequence[str] | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """Upgrade schema."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Downgrade schema."""
    ${downgrades if downgrades else "pass"}
