"""Initial schema: the 16 SPEC §15 entities, the infrastructure tables, the append-only /
single-transition / forward-only triggers, and on Postgres the roles, grants and row-level
security (DESIGN §5.3; SPEC §12 §13 §15).

Revision ID: 0001
Revises:
Create Date: 2026-10-02

Generated from ``nour.db.models`` (``alembic revision --autogenerate``) and then frozen: this file
never imports the models, so a later change to them shows up as a non-empty ``alembic check``
diff (``tests/unit/test_migrations_match_metadata.py``) instead of silently rewriting history.
What it shares with ``create_schema`` are the DDL *generators* of ``nour.db.engine`` —
``trigger_ddl`` (both dialects) and ``pg_roles_ddl`` (Postgres) — fed with the tables created
here plus the wall markers frozen in ``TABLE_FLAGS`` / ``GRANTS``, so a test database built by
``create_schema`` and a production database built by this migration carry the same triggers
(append-only, kept rows, single-transition, immutable columns, forward-only), roles, grants
(per-role verb and column grants) and row-level-security policies (DESK_ROW and the
desk-partitioned ``inbox_event`` / ``pending_owner_message``).

Postgres: the migration runs as the schema owner (migrator credentials); the five runtime roles
are created ``NOLOGIN`` and the operator grants LOGIN and a password out of band. Roles are
cluster-wide and are left in place by ``downgrade()``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from nour.db.base import (
    NAMING_CONVENTION,
    TABLE_INFO_KEY,
    EncryptedBytes,
    JSONCol,
    Scope,
    UtcDateTime,
)
from nour.db.engine import GRANTS_KEY, pg_roles_ddl, trigger_ddl

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUDITOR_SCHEMA = "auditor"


def _flags(
    scope: str,
    *,
    append_only: bool = False,
    single_transition: tuple[str, ...] = (),
    forward_only: dict[str, list[str]] | None = None,
    immutable: tuple[str, ...] = (),
    desk_partitioned: bool = False,
) -> dict[str, Any]:
    return {
        "scope": scope,
        "append_only": append_only,
        "single_transition": single_transition,
        "forward_only": forward_only or {},
        "immutable": immutable,
        "desk_partitioned": desk_partitioned,
    }


APPROVAL_DECISION_COLUMNS: tuple[str, ...] = (
    "decided_at",
    "decision",
    "reason",
    "passphrase_verified",
    "second_channel_confirmed",
    "decided_via",
    "decided_by_event_id",
)

TABLE_FLAGS: dict[str, dict[str, Any]] = {
    # table key: the wall markers (Base.__scope__ & co.) frozen at this revision
    "audit_chain_head": _flags("shared"),
    "audit_event": _flags("shared", append_only=True),
    "auditor.auditor_report": _flags("auditor_write", append_only=True),
    "card": _flags("governance_only"),
    "card_authorization": _flags("shared", append_only=True),
    "coat": _flags("shared"),
    "dnc_entry": _flags("shared"),
    "document": _flags(
        "assistant_only",
        immutable=(
            "id",
            "created_at",
            "entity_ref",
            "type",
            "tier",
            "title",
            "expiry",
            "allowed_recipients",
            "storage_ref",
            "sha256",
            "version",
            "supersedes_id",
            "content_text",
            "metadata",
            "confirmed_by_event_id",
        ),
    ),
    "found_instruction": _flags("shared", append_only=True),
    "freeze_state": _flags("shared", single_transition=("released_at", "released_by_approval_id")),
    "inbox_event": _flags(
        "shared",
        immutable=(
            "id",
            "created_at",
            "desk",
            "coat_id",
            "source_kind",
            "channel",
            "line_id",
            "sender",
            "origin",
            "body",
            "audio_ref",
            "payload",
            "signature_valid",
            "passphrase_attempt",
            "attempt_id",
            "provider_msg_id",
            "priority",
            "received_at",
        ),
        desk_partitioned=True,
    ),
    "incident": _flags(
        "shared",
        single_transition=("resolved_at", "postmortem_ref"),
        immutable=(
            "id",
            "created_at",
            "desk",
            "coat_id",
            "type",
            "detected_at",
            "detected_by",
            "first_response",
            "frozen_scope",
            "event_id",
            "details",
        ),
    ),
    "model_trace": _flags("desk_row", append_only=True),
    "owner": _flags("governance_only"),
    "passphrase_attempt": _flags("shared", append_only=True),
    "pending_owner_message": _flags(
        "shared", single_transition=("sent_at", "provider_msg_id"), desk_partitioned=True
    ),
    "readback_pending": _flags(
        "desk_row", single_transition=("confirmed_at", "confirmed_by_event_id")
    ),
    "release": _flags(
        "shared",
        single_transition=("burnt_at",),
        immutable=(
            "id",
            "created_at",
            "desk",
            "call_id",
            "nonce",
            "tier",
            "approval_id",
            "minted_at",
            "minted_by",
        ),
    ),
    "second_channel_challenge": _flags(
        "shared", single_transition=("consumed_at", "consumed_by", "approved")
    ),
    "skill": _flags("desk_row"),
    "timer_slot": _flags("governance_only", append_only=True),
    "vault_secret": _flags("assistant_only"),
    "approval": _flags(
        "shared",
        single_transition=APPROVAL_DECISION_COLUMNS,
        immutable=(
            "id",
            "created_at",
            "desk",
            "coat_id",
            "seq",
            "item_type",
            "item_ref",
            "action_json",
            "category",
            "tier",
            "amount",
            "currency",
            "draft_ref",
            "requested_at",
            "expires_at",
            "trigger_event_id",
        ),
    ),
    "beneficiary": _flags("assistant_only"),
    "category_state": _flags("desk_row"),
    "contact": _flags("desk_row"),
    "experiment": _flags("operator_only"),
    "handoff": _flags(
        "shared",
        single_transition=("taken_at",),
        immutable=("id", "created_at", "coat_id", "handoff_json", "source_event_id", "pushed_by"),
    ),
    "memory_record": _flags("desk_row"),
    "task": _flags("desk_row"),
    "conversation": _flags("desk_row"),
    "decision_journal": _flags("shared", append_only=True),
    "transaction": _flags(
        "shared",
        forward_only={
            "status": ["prepared", "authorized", "declined", "released", "settled", "reconciled"]
        },
    ),
    "message": _flags("desk_row"),
}
"""The wall markers of every table at this revision (``Base.__scope__`` & co., frozen)."""


def _grant(
    insert: bool | tuple[str, ...] = False,
    update: bool | tuple[str, ...] = False,
    delete: bool = False,
) -> dict[str, Any]:
    return {"insert": insert, "update": update, "delete": delete}


INBOX_ACK_COLUMNS: tuple[str, ...] = ("acked_at", "parked_until", "lock_until", "error")
INBOX_DESK_INSERT_COLUMNS: tuple[str, ...] = (
    "id",
    "created_at",
    "updated_at",
    "desk",
    "coat_id",
    "source_kind",
    "channel",
    "line_id",
    "sender",
    "origin",
    "body",
    "audio_ref",
    "payload",
    "provider_msg_id",
    "priority",
    "received_at",
    "acked_at",
    "parked_until",
    "lock_until",
    "error",
)
_DESKS = ("nour_desk_operator", "nour_desk_assistant")
_RUNTIME = ("nour_desk_operator", "nour_desk_assistant", "nour_ingress", "nour_scheduler")

GRANTS: dict[str, dict[str, dict[str, Any]]] = {
    # table key: {role: {"insert": bool | columns, "update": bool | columns, "delete": bool}}
    "coat": {role: _grant() for role in _DESKS},
    "dnc_entry": {role: _grant(insert=True) for role in _DESKS},
    "freeze_state": {role: _grant(insert=True) for role in _DESKS},
    "inbox_event": {
        **{
            role: _grant(insert=INBOX_DESK_INSERT_COLUMNS, update=INBOX_ACK_COLUMNS)
            for role in _DESKS
        },
        "nour_ingress": _grant(insert=True),
        "nour_scheduler": _grant(insert=True, delete=True),
    },
    "passphrase_attempt": {
        role: _grant() for role in ("nour_desk_operator", "nour_desk_assistant", "nour_scheduler")
    },
    "release": {role: _grant(insert=True, update=("burnt_at",)) for role in _RUNTIME},
    "approval": {role: _grant(insert=True, update=APPROVAL_DECISION_COLUMNS) for role in _RUNTIME},
    "handoff": {
        "nour_desk_assistant": _grant(insert=True),
        "nour_desk_operator": _grant(update=("taken_at",)),
        "nour_ingress": _grant(),
        "nour_scheduler": _grant(),
    },
}
"""Per-role verb and column grants (``nour.db.engine.GRANTS_KEY``; THREAT_REVIEW 6.1), frozen
like the flags."""


def _flagged_metadata(tables: list[sa.Table]) -> sa.MetaData:
    """One ``MetaData`` holding the tables this revision created, with the frozen wall markers
    attached the way ``Base`` attaches them, so the shared DDL generators can read them."""
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)
    for table in tables:
        copy = table.to_metadata(metadata)
        frozen = TABLE_FLAGS[copy.key]
        flags: dict[str, Any] = {
            "scope": Scope(frozen["scope"]),
            "append_only": bool(frozen["append_only"]),
            "single_transition": tuple(frozen["single_transition"]),
            "forward_only": {
                column: list(order) for column, order in frozen["forward_only"].items()
            },
            "immutable": tuple(frozen["immutable"]),
            "desk_partitioned": bool(frozen["desk_partitioned"]),
        }
        copy.info[TABLE_INFO_KEY] = flags
        grants = GRANTS.get(copy.key)
        if grants:
            copy.info[GRANTS_KEY] = {
                role: {verb: value for verb, value in grant.items()}
                for role, grant in grants.items()
            }
    return metadata


def upgrade() -> None:
    """Create every table, then the triggers (both dialects) and the Postgres roles/RLS."""
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(f'CREATE SCHEMA IF NOT EXISTS "{AUDITOR_SCHEMA}"')
    tables: list[sa.Table] = []
    tables.append(
        op.create_table(
            "audit_chain_head",
            sa.Column("singleton", sa.Integer(), autoincrement=False, nullable=False),
            sa.Column("last_hash", sa.Text(), nullable=False),
            sa.Column("last_seq", sa.Integer(), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.CheckConstraint("singleton = 1", name=op.f("ck_audit_chain_head_singleton")),
            sa.PrimaryKeyConstraint("singleton", name=op.f("pk_audit_chain_head")),
        )
    )
    tables.append(
        op.create_table(
            "audit_event",
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("seq", sa.Integer(), nullable=False),
            sa.Column("ts", UtcDateTime(), nullable=False),
            sa.Column("actor", sa.String(length=16), nullable=False),
            sa.Column("action", sa.String(length=128), nullable=False),
            sa.Column("category", sa.String(length=64), nullable=True),
            sa.Column("tier", sa.String(length=1), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("phase", sa.String(length=8), nullable=False),
            sa.Column("counterpart", sa.Text(), nullable=True),
            sa.Column("amount", sa.BigInteger(), nullable=True),
            sa.Column("currency", sa.String(length=3), nullable=True),
            sa.Column("approval_id", sa.String(length=26), nullable=True),
            sa.Column("data_tier", sa.Integer(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("input_hash", sa.Text(), nullable=False),
            sa.Column("output_hash", sa.Text(), nullable=False),
            sa.Column("event_id", sa.String(length=26), nullable=True),
            sa.Column("invocation_id", sa.String(length=26), nullable=False),
            sa.Column("prev_hash", sa.Text(), nullable=False),
            sa.Column(
                "entry_hash",
                sa.Text(),
                nullable=False,
                comment="Keyed HMAC (nour.core.hashing.keyed_hash with the audit chain key) over the canonical row and prev_hash, computed by nour/audit/log.py; never a plain sha256 (SPEC §12; THREAT_REVIEW 6.6).",
            ),
            sa.Column("config_hash", sa.Text(), nullable=False),
            sa.Column("dry_run", sa.Boolean(), nullable=False),
            sa.Column(
                "desk",
                sa.Enum(
                    "operator",
                    "assistant",
                    "governance",
                    name="desk",
                    native_enum=False,
                    create_constraint=True,
                    length=16,
                ),
                nullable=False,
            ),
            sa.Column("coat_id", sa.String(length=64), nullable=True),
            sa.CheckConstraint(
                "actor IN ('nour', 'subagent', 'owner', 'auditor', 'system', 'deputy')",
                name=op.f("ck_audit_event_actor"),
            ),
            sa.CheckConstraint("phase IN ('opened', 'closed')", name=op.f("ck_audit_event_phase")),
            sa.CheckConstraint(
                "status IN ('opened', 'executed', 'notified', 'queued', 'refused', 'declined', 'failed', 'readback_pending', 'frozen', 'deferred', 'dry_run', 'observed', 'released')",
                name=op.f("ck_audit_event_status"),
            ),
            sa.CheckConstraint(
                "tier IS NULL OR tier IN ('A', 'N', 'K')", name=op.f("ck_audit_event_tier")
            ),
            sa.CheckConstraint("data_tier IN (0, 1, 2, 3)", name=op.f("ck_audit_event_data_tier")),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_event")),
            sa.UniqueConstraint("entry_hash", name=op.f("uq_audit_event_entry_hash")),
            sa.UniqueConstraint(
                "invocation_id", "phase", name=op.f("uq_audit_event_invocation_id_phase")
            ),
            sa.UniqueConstraint("seq", name=op.f("uq_audit_event_seq")),
        )
    )
    op.create_index(op.f("ix_audit_event_coat_id"), "audit_event", ["coat_id"], unique=False)
    op.create_index(op.f("ix_audit_event_desk"), "audit_event", ["desk"], unique=False)
    op.create_index(op.f("ix_audit_event_event_id"), "audit_event", ["event_id"], unique=False)
    op.create_index(
        op.f("ix_audit_event_invocation_id"), "audit_event", ["invocation_id"], unique=False
    )
    op.create_index(op.f("ix_audit_event_ts"), "audit_event", ["ts"], unique=False)
    tables.append(
        op.create_table(
            "auditor_report",
            sa.Column("day", sa.Date(), nullable=False),
            sa.Column("findings", JSONCol, nullable=False),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("vendor", sa.String(length=64), nullable=False),
            sa.Column("model", sa.String(length=128), nullable=False),
            sa.Column("sent_at", UtcDateTime(), nullable=True),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_auditor_report")),
            schema="auditor",
        )
    )
    op.create_index(
        op.f("ix_auditor_auditor_report_day"),
        "auditor_report",
        ["day"],
        unique=False,
        schema="auditor",
    )
    tables.append(
        op.create_table(
            "card",
            sa.Column("holder", sa.String(length=64), nullable=False),
            sa.Column("card_ref", sa.Text(), nullable=False),
            sa.Column("monthly_cap", sa.BigInteger(), nullable=False),
            sa.Column("currency", sa.String(length=3), nullable=False),
            sa.Column("frozen", sa.Boolean(), nullable=False),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.Column(
                "desk",
                sa.Enum(
                    "operator",
                    "assistant",
                    "governance",
                    name="desk",
                    native_enum=False,
                    create_constraint=True,
                    length=16,
                ),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_card")),
            sa.UniqueConstraint("card_ref", name=op.f("uq_card_card_ref")),
            sa.UniqueConstraint("holder", name=op.f("uq_card_holder")),
        )
    )
    op.create_index(op.f("ix_card_desk"), "card", ["desk"], unique=False)
    tables.append(
        op.create_table(
            "card_authorization",
            sa.Column("card_ref", sa.Text(), nullable=False),
            sa.Column("holder", sa.String(length=64), nullable=False),
            sa.Column("amount", sa.BigInteger(), nullable=False),
            sa.Column("currency", sa.String(length=3), nullable=False),
            sa.Column("merchant", sa.Text(), nullable=False),
            sa.Column("approved", sa.Boolean(), nullable=False),
            sa.Column("decline_reason", sa.Text(), nullable=True),
            sa.Column("auth_ref", sa.Text(), nullable=True),
            sa.Column("call_id", sa.String(length=26), nullable=False),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.Column("audit_id", sa.String(length=26), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_card_authorization")),
            sa.UniqueConstraint("auth_ref", name=op.f("uq_card_authorization_auth_ref")),
        )
    )
    op.create_index(
        "ix_card_authorization_card_ref_created_at",
        "card_authorization",
        ["card_ref", "created_at"],
        unique=False,
    )
    tables.append(
        op.create_table(
            "coat",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("slug", sa.String(length=64), nullable=False),
            sa.Column("legal_entity", sa.Text(), nullable=False),
            sa.Column("domain", sa.Text(), nullable=False),
            sa.Column("email_identity", sa.Text(), nullable=False),
            sa.Column("whatsapp_line", sa.String(length=32), nullable=False),
            sa.Column("signature_ref", sa.Text(), nullable=False),
            sa.Column("letterhead_ref", sa.Text(), nullable=False),
            sa.Column("tone_guide_ref", sa.Text(), nullable=False),
            sa.Column("knowledge_pack_ref", sa.Text(), nullable=False),
            sa.Column("mandate", JSONCol, nullable=False),
            sa.Column("approval_rules", JSONCol, nullable=False),
            sa.Column("allowed_activities", JSONCol, nullable=False),
            sa.Column("banking_ref", sa.Text(), nullable=False),
            sa.Column("desks_allowed", JSONCol, nullable=False),
            sa.Column("config_hash", sa.Text(), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.CheckConstraint("slug = id", name=op.f("ck_coat_slug_is_id")),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_coat")),
            sa.UniqueConstraint("email_identity", name=op.f("uq_coat_email_identity")),
            sa.UniqueConstraint("name", name=op.f("uq_coat_name")),
            sa.UniqueConstraint("slug", name=op.f("uq_coat_slug")),
            sa.UniqueConstraint("whatsapp_line", name=op.f("uq_coat_whatsapp_line")),
        )
    )
    tables.append(
        op.create_table(
            "dnc_entry",
            sa.Column("address", sa.Text(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("set_at", UtcDateTime(), nullable=False),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.Column("audit_id", sa.String(length=26), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_dnc_entry")),
            sa.UniqueConstraint("address", name=op.f("uq_dnc_entry_address")),
        )
    )
    tables.append(
        op.create_table(
            "document",
            sa.Column("entity_ref", sa.Text(), nullable=False),
            sa.Column("type", sa.String(length=64), nullable=False),
            sa.Column("tier", sa.Integer(), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("expiry", sa.Date(), nullable=True),
            sa.Column("allowed_recipients", JSONCol, nullable=False),
            sa.Column("storage_ref", sa.Text(), nullable=False),
            sa.Column("sha256", sa.Text(), nullable=False),
            sa.Column("share_log", JSONCol, nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("supersedes_id", sa.String(length=26), nullable=True),
            sa.Column("content_text", sa.Text(), nullable=True),
            sa.Column("metadata", JSONCol, nullable=False),
            sa.Column("confirmed_by_event_id", sa.String(length=26), nullable=False),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.CheckConstraint(
                "tier <> 2 OR content_text IS NULL", name=op.f("ck_document_tier2_metadata_only")
            ),
            sa.CheckConstraint("tier IN (0, 1, 2, 3)", name=op.f("ck_document_tier")),
            sa.ForeignKeyConstraint(
                ["supersedes_id"], ["document.id"], name=op.f("fk_document_supersedes_id_document")
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_document")),
        )
    )
    op.create_index("ix_document_entity_ref_type", "document", ["entity_ref", "type"], unique=False)
    op.create_index(op.f("ix_document_expiry"), "document", ["expiry"], unique=False)
    op.create_index(op.f("ix_document_tier"), "document", ["tier"], unique=False)
    tables.append(
        op.create_table(
            "found_instruction",
            sa.Column("event_id", sa.String(length=26), nullable=False),
            sa.Column("quote", sa.Text(), nullable=False),
            sa.Column("location", sa.Text(), nullable=False),
            sa.Column("mentions_money", sa.Boolean(), nullable=False),
            sa.Column("pattern", sa.String(length=64), nullable=False),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.Column(
                "desk",
                sa.Enum(
                    "operator",
                    "assistant",
                    "governance",
                    name="desk",
                    native_enum=False,
                    create_constraint=True,
                    length=16,
                ),
                nullable=False,
            ),
            sa.Column("coat_id", sa.String(length=64), nullable=True),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_found_instruction")),
        )
    )
    op.create_index(
        op.f("ix_found_instruction_coat_id"), "found_instruction", ["coat_id"], unique=False
    )
    op.create_index(op.f("ix_found_instruction_desk"), "found_instruction", ["desk"], unique=False)
    op.create_index(
        op.f("ix_found_instruction_event_id"), "found_instruction", ["event_id"], unique=False
    )
    tables.append(
        op.create_table(
            "freeze_state",
            sa.Column("scope", sa.String(length=32), nullable=False),
            sa.Column("target", sa.Text(), nullable=True),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("actor", sa.String(length=32), nullable=False),
            sa.Column("engaged_at", UtcDateTime(), nullable=False),
            sa.Column("released_at", UtcDateTime(), nullable=True),
            sa.Column("released_by_approval_id", sa.String(length=26), nullable=True),
            sa.Column("event_id", sa.String(length=26), nullable=True),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.CheckConstraint(
                "scope IN ('high_impact', 'autonomous', 'channel', 'coat_outgoing', 'vault_sharing', 'category', 'all_outbound')",
                name=op.f("ck_freeze_state_scope"),
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_freeze_state")),
        )
    )
    op.create_index(
        "ix_freeze_state_released_at",
        "freeze_state",
        ["released_at"],
        unique=False,
        postgresql_where=sa.text("released_at IS NULL"),
    )
    tables.append(
        op.create_table(
            "inbox_event",
            sa.Column("source_kind", sa.String(length=32), nullable=False),
            sa.Column("channel", sa.String(length=64), nullable=False),
            sa.Column("line_id", sa.String(length=128), nullable=True),
            sa.Column("sender", sa.Text(), nullable=False),
            sa.Column("origin", sa.String(length=16), nullable=False),
            sa.Column("body", sa.Text(), nullable=True),
            sa.Column("audio_ref", sa.Text(), nullable=True),
            sa.Column("payload", JSONCol, nullable=False),
            sa.Column("signature_valid", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("passphrase_attempt", sa.String(length=32), nullable=True),
            sa.Column("attempt_id", sa.String(length=26), nullable=True),
            sa.Column("provider_msg_id", sa.Text(), nullable=True),
            sa.Column("priority", sa.Integer(), nullable=False),
            sa.Column("received_at", UtcDateTime(), nullable=False),
            sa.Column("acked_at", UtcDateTime(), nullable=True),
            sa.Column("parked_until", UtcDateTime(), nullable=True),
            sa.Column("lock_until", UtcDateTime(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.Column(
                "desk",
                sa.Enum(
                    "operator",
                    "assistant",
                    "governance",
                    name="desk",
                    native_enum=False,
                    create_constraint=True,
                    length=16,
                ),
                nullable=False,
            ),
            sa.Column("coat_id", sa.String(length=64), nullable=True),
            sa.CheckConstraint(
                "origin IN ('text', 'voice', 'system')", name=op.f("ck_inbox_event_origin")
            ),
            sa.CheckConstraint(
                "passphrase_attempt IS NULL OR passphrase_attempt IN ('ok', 'wrong', 'spoken', 'wrong_thread', 'spoofed_number', 'spoof_suspected', 'replayed')",
                name=op.f("ck_inbox_event_passphrase_attempt"),
            ),
            sa.CheckConstraint(
                "source_kind IN ('whatsapp', 'email', 'phone_notification', 'timer', 'approval_decision', 'handoff', 'second_channel', 'staff_line', 'readback')",
                name=op.f("ck_inbox_event_source_kind"),
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_inbox_event")),
            sa.UniqueConstraint(
                "source_kind",
                "provider_msg_id",
                name=op.f("uq_inbox_event_source_kind_provider_msg_id"),
            ),
        )
    )
    op.create_index(op.f("ix_inbox_event_coat_id"), "inbox_event", ["coat_id"], unique=False)
    op.create_index(op.f("ix_inbox_event_desk"), "inbox_event", ["desk"], unique=False)
    op.create_index(
        "ix_inbox_event_desk_acked_at_priority_received_at",
        "inbox_event",
        ["desk", "acked_at", "priority", "received_at"],
        unique=False,
    )
    tables.append(
        op.create_table(
            "incident",
            sa.Column("type", sa.String(length=32), nullable=False),
            sa.Column("detected_at", UtcDateTime(), nullable=False),
            sa.Column("detected_by", sa.String(length=16), nullable=False),
            sa.Column("first_response", sa.Text(), nullable=False),
            sa.Column("frozen_scope", sa.String(length=32), nullable=True),
            sa.Column("resolved_at", UtcDateTime(), nullable=True),
            sa.Column("postmortem_ref", sa.Text(), nullable=True),
            sa.Column("event_id", sa.String(length=26), nullable=True),
            sa.Column("details", JSONCol, nullable=False),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.Column(
                "desk",
                sa.Enum(
                    "operator",
                    "assistant",
                    "governance",
                    name="desk",
                    native_enum=False,
                    create_constraint=True,
                    length=16,
                ),
                nullable=False,
            ),
            sa.Column("coat_id", sa.String(length=64), nullable=True),
            sa.CheckConstraint(
                "detected_by IN ('nour', 'auditor', 'owner', 'system')",
                name=op.f("ck_incident_detected_by"),
            ),
            sa.CheckConstraint(
                "frozen_scope IS NULL OR frozen_scope IN ('high_impact', 'autonomous', 'channel', 'coat_outgoing', 'vault_sharing', 'category', 'all_outbound')",
                name=op.f("ck_incident_frozen_scope"),
            ),
            sa.CheckConstraint(
                "type IN ('suspicious_payment', 'bad_message', 'data_leak', 'channel_banned', 'instruction_in_content', 'model_outage', 'auth_failure', 'impersonation', 'watchdog_loop', 'watchdog_spend', 'watchdog_failed_sends', 'kill_switch')",
                name=op.f("ck_incident_type"),
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_incident")),
        )
    )
    op.create_index(op.f("ix_incident_coat_id"), "incident", ["coat_id"], unique=False)
    op.create_index(op.f("ix_incident_desk"), "incident", ["desk"], unique=False)
    op.create_index(
        "ix_incident_type_detected_at", "incident", ["type", "detected_at"], unique=False
    )
    tables.append(
        op.create_table(
            "model_trace",
            sa.Column("event_id", sa.String(length=26), nullable=False),
            sa.Column("request_hash", sa.Text(), nullable=False),
            sa.Column("response_json", JSONCol, nullable=False),
            sa.Column("vendor", sa.String(length=64), nullable=False),
            sa.Column("model", sa.String(length=128), nullable=False),
            sa.Column("expires_at", UtcDateTime(), nullable=False),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.Column(
                "desk",
                sa.Enum(
                    "operator",
                    "assistant",
                    "governance",
                    name="desk",
                    native_enum=False,
                    create_constraint=True,
                    length=16,
                ),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_model_trace")),
        )
    )
    op.create_index(op.f("ix_model_trace_desk"), "model_trace", ["desk"], unique=False)
    op.create_index(op.f("ix_model_trace_event_id"), "model_trace", ["event_id"], unique=False)
    tables.append(
        op.create_table(
            "owner",
            sa.Column("singleton", sa.Integer(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("whatsapp_number", sa.String(length=32), nullable=False),
            sa.Column("passphrase_hash", sa.Text(), nullable=False),
            sa.Column("passphrase_fp", sa.LargeBinary(), nullable=False),
            sa.Column("second_channel", JSONCol, nullable=False),
            sa.Column("deputy_id", sa.String(length=64), nullable=True),
            sa.Column("quiet_hours", JSONCol, nullable=False),
            sa.Column("timezone", sa.String(length=64), nullable=False),
            sa.Column("deputy_mode_since", UtcDateTime(), nullable=True),
            sa.Column("last_seen_at", UtcDateTime(), nullable=True),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.CheckConstraint("singleton = 1", name=op.f("ck_owner_singleton")),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_owner")),
            sa.UniqueConstraint("singleton", name=op.f("uq_owner_singleton")),
            sa.UniqueConstraint("whatsapp_number", name=op.f("uq_owner_whatsapp_number")),
        )
    )
    tables.append(
        op.create_table(
            "passphrase_attempt",
            sa.Column("event_id", sa.String(length=26), nullable=True),
            sa.Column("sender", sa.Text(), nullable=False),
            sa.Column("channel", sa.String(length=64), nullable=False),
            sa.Column("outcome", sa.String(length=32), nullable=False),
            sa.Column("at", UtcDateTime(), nullable=False),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.CheckConstraint(
                "outcome IN ('ok', 'wrong', 'spoken', 'wrong_thread', 'spoofed_number', 'spoof_suspected', 'replayed')",
                name=op.f("ck_passphrase_attempt_outcome"),
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_passphrase_attempt")),
        )
    )
    op.create_index(op.f("ix_passphrase_attempt_at"), "passphrase_attempt", ["at"], unique=False)
    tables.append(
        op.create_table(
            "pending_owner_message",
            sa.Column("kind", sa.String(length=64), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("emergency", sa.Boolean(), nullable=False),
            sa.Column("parked_until", UtcDateTime(), nullable=True),
            sa.Column("sent_at", UtcDateTime(), nullable=True),
            sa.Column("provider_msg_id", sa.Text(), nullable=True),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.Column(
                "desk",
                sa.Enum(
                    "operator",
                    "assistant",
                    "governance",
                    name="desk",
                    native_enum=False,
                    create_constraint=True,
                    length=16,
                ),
                nullable=False,
            ),
            sa.Column("audit_id", sa.String(length=26), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_pending_owner_message")),
        )
    )
    op.create_index(
        op.f("ix_pending_owner_message_desk"), "pending_owner_message", ["desk"], unique=False
    )
    op.create_index(
        "ix_pending_owner_message_sent_at",
        "pending_owner_message",
        ["sent_at"],
        unique=False,
        postgresql_where=sa.text("sent_at IS NULL"),
    )
    tables.append(
        op.create_table(
            "readback_pending",
            sa.Column("event_id", sa.String(length=26), nullable=False),
            sa.Column("proposal_json", JSONCol, nullable=False),
            sa.Column("understood", sa.Text(), nullable=False),
            sa.Column("expires_at", UtcDateTime(), nullable=False),
            sa.Column("confirmed_by_event_id", sa.String(length=26), nullable=True),
            sa.Column("confirmed_at", UtcDateTime(), nullable=True),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.Column(
                "desk",
                sa.Enum(
                    "operator",
                    "assistant",
                    "governance",
                    name="desk",
                    native_enum=False,
                    create_constraint=True,
                    length=16,
                ),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_readback_pending")),
        )
    )
    op.create_index(op.f("ix_readback_pending_desk"), "readback_pending", ["desk"], unique=False)
    op.create_index(
        op.f("ix_readback_pending_expires_at"), "readback_pending", ["expires_at"], unique=False
    )
    tables.append(
        op.create_table(
            "release",
            sa.Column("call_id", sa.String(length=26), nullable=False),
            sa.Column("nonce", sa.String(length=128), nullable=False),
            sa.Column("tier", sa.String(length=1), nullable=False),
            sa.Column("approval_id", sa.String(length=26), nullable=True),
            sa.Column("minted_at", UtcDateTime(), nullable=False),
            sa.Column("minted_by", sa.String(length=16), nullable=False),
            sa.Column("burnt_at", UtcDateTime(), nullable=True),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.Column(
                "desk",
                sa.Enum(
                    "operator",
                    "assistant",
                    "governance",
                    name="desk",
                    native_enum=False,
                    create_constraint=True,
                    length=16,
                ),
                nullable=False,
            ),
            sa.CheckConstraint(
                "minted_by IN ('gate', 'approval')", name=op.f("ck_release_minted_by")
            ),
            sa.CheckConstraint("tier IN ('A', 'N', 'K')", name=op.f("ck_release_tier")),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_release")),
            sa.UniqueConstraint("call_id", name=op.f("uq_release_call_id")),
            sa.UniqueConstraint("nonce", name=op.f("uq_release_nonce")),
        )
    )
    op.create_index(op.f("ix_release_desk"), "release", ["desk"], unique=False)
    tables.append(
        op.create_table(
            "second_channel_challenge",
            sa.Column("ref", sa.Text(), nullable=False),
            sa.Column("purpose", sa.String(length=64), nullable=False),
            sa.Column("token_hash", sa.Text(), nullable=False),
            sa.Column("issued_at", UtcDateTime(), nullable=False),
            sa.Column("expires_at", UtcDateTime(), nullable=False),
            sa.Column("consumed_at", UtcDateTime(), nullable=True),
            sa.Column("consumed_by", sa.Text(), nullable=True),
            sa.Column("approved", sa.Boolean(), nullable=True),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_second_channel_challenge")),
            sa.UniqueConstraint("token_hash", name=op.f("uq_second_channel_challenge_token_hash")),
        )
    )
    op.create_index(
        op.f("ix_second_channel_challenge_ref"), "second_channel_challenge", ["ref"], unique=False
    )
    tables.append(
        op.create_table(
            "skill",
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("trigger", sa.Text(), nullable=False),
            sa.Column("steps_ref", sa.Text(), nullable=False),
            sa.Column("inputs", JSONCol, nullable=False),
            sa.Column("failure_signs", JSONCol, nullable=False),
            sa.Column("dry_run_until", sa.Date(), nullable=True),
            sa.Column("approved_at", UtcDateTime(), nullable=True),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.Column(
                "desk",
                sa.Enum(
                    "operator",
                    "assistant",
                    "governance",
                    name="desk",
                    native_enum=False,
                    create_constraint=True,
                    length=16,
                ),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_skill")),
            sa.UniqueConstraint("name", "version", name=op.f("uq_skill_name_version")),
        )
    )
    op.create_index(op.f("ix_skill_desk"), "skill", ["desk"], unique=False)
    tables.append(
        op.create_table(
            "timer_slot",
            sa.Column("timer_name", sa.String(length=64), nullable=False),
            sa.Column("slot", UtcDateTime(), nullable=False),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_timer_slot")),
            sa.UniqueConstraint("timer_name", "slot", name=op.f("uq_timer_slot_timer_name_slot")),
        )
    )
    tables.append(
        op.create_table(
            "vault_secret",
            sa.Column("entity_ref", sa.Text(), nullable=False),
            sa.Column("key", sa.String(length=64), nullable=False),
            sa.Column("uri", sa.Text(), nullable=False),
            sa.Column("ciphertext", EncryptedBytes(), nullable=False),
            sa.Column("last4", sa.String(length=4), nullable=False),
            sa.Column("content_fp", sa.Text(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_vault_secret")),
            sa.UniqueConstraint("uri", name=op.f("uq_vault_secret_uri")),
        )
    )
    op.create_index(
        op.f("ix_vault_secret_entity_ref"), "vault_secret", ["entity_ref"], unique=False
    )
    tables.append(
        op.create_table(
            "approval",
            sa.Column("seq", sa.Integer(), nullable=False),
            sa.Column("item_type", sa.String(length=64), nullable=False),
            sa.Column("item_ref", sa.Text(), nullable=False),
            sa.Column("action_json", JSONCol, nullable=False),
            sa.Column("category", sa.String(length=64), nullable=False),
            sa.Column("tier", sa.String(length=1), nullable=False),
            sa.Column("amount", sa.BigInteger(), nullable=True),
            sa.Column("currency", sa.String(length=3), nullable=True),
            sa.Column("draft_ref", sa.Text(), nullable=True),
            sa.Column("requested_at", UtcDateTime(), nullable=False),
            sa.Column("expires_at", UtcDateTime(), nullable=False),
            sa.Column("decided_at", UtcDateTime(), nullable=True),
            sa.Column("decision", sa.String(length=16), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("passphrase_verified", sa.Boolean(), nullable=True),
            sa.Column("second_channel_confirmed", sa.Boolean(), nullable=True),
            sa.Column("decided_via", sa.String(length=32), nullable=True),
            sa.Column("decided_by_event_id", sa.String(length=26), nullable=True),
            sa.Column("trigger_event_id", sa.String(length=26), nullable=False),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.Column(
                "desk",
                sa.Enum(
                    "operator",
                    "assistant",
                    "governance",
                    name="desk",
                    native_enum=False,
                    create_constraint=True,
                    length=16,
                ),
                nullable=False,
            ),
            sa.Column("coat_id", sa.String(length=64), nullable=True),
            sa.CheckConstraint(
                "decision IS NULL OR decision IN ('approve', 'reject', 'expired')",
                name=op.f("ck_approval_decision"),
            ),
            sa.CheckConstraint("tier = 'K'", name=op.f("ck_approval_tier_is_k")),
            sa.ForeignKeyConstraint(
                ["coat_id"], ["coat.id"], name=op.f("fk_approval_coat_id_coat")
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_approval")),
            sa.UniqueConstraint("seq", name=op.f("uq_approval_seq")),
        )
    )
    op.create_index(op.f("ix_approval_coat_id"), "approval", ["coat_id"], unique=False)
    op.create_index(
        "ix_approval_decision",
        "approval",
        ["decision"],
        unique=False,
        postgresql_where=sa.text("decision IS NULL"),
    )
    op.create_index(op.f("ix_approval_desk"), "approval", ["desk"], unique=False)
    tables.append(
        op.create_table(
            "beneficiary",
            sa.Column("coat_id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("bank_details_ref", sa.Text(), nullable=False),
            sa.Column("bank_details_ct", EncryptedBytes(), nullable=False),
            sa.Column("bank_last4", sa.String(length=4), nullable=False),
            sa.Column("bank_fp", sa.Text(), nullable=False),
            sa.Column("verified_at", UtcDateTime(), nullable=True),
            sa.Column("verified_by", sa.Text(), nullable=True),
            sa.Column("verification_method", sa.String(length=32), nullable=True),
            sa.Column("change_history", JSONCol, nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["coat_id"], ["coat.id"], name=op.f("fk_beneficiary_coat_id_coat")
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_beneficiary")),
            sa.UniqueConstraint("coat_id", "name", name=op.f("uq_beneficiary_coat_id_name")),
        )
    )
    op.create_index(op.f("ix_beneficiary_coat_id"), "beneficiary", ["coat_id"], unique=False)
    tables.append(
        op.create_table(
            "category_state",
            sa.Column("coat_id", sa.String(length=64), nullable=False),
            sa.Column("category", sa.String(length=64), nullable=False),
            sa.Column("tier", sa.String(length=1), nullable=False),
            sa.Column("started_at", sa.Date(), nullable=False),
            sa.Column("promoted_at", UtcDateTime(), nullable=True),
            sa.Column("demoted_at", UtcDateTime(), nullable=True),
            sa.Column("items", sa.Integer(), nullable=False),
            sa.Column("unedited", sa.Integer(), nullable=False),
            sa.Column("reduce_to_k", sa.Boolean(), nullable=False),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.Column(
                "desk",
                sa.Enum(
                    "operator",
                    "assistant",
                    "governance",
                    name="desk",
                    native_enum=False,
                    create_constraint=True,
                    length=16,
                ),
                nullable=False,
            ),
            sa.CheckConstraint("tier IN ('A', 'N', 'K')", name=op.f("ck_category_state_tier")),
            sa.ForeignKeyConstraint(
                ["coat_id"], ["coat.id"], name=op.f("fk_category_state_coat_id_coat")
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_category_state")),
            sa.UniqueConstraint(
                "coat_id", "category", "desk", name=op.f("uq_category_state_coat_id_category_desk")
            ),
        )
    )
    op.create_index(op.f("ix_category_state_desk"), "category_state", ["desk"], unique=False)
    tables.append(
        op.create_table(
            "contact",
            sa.Column("coat_id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("org", sa.Text(), nullable=True),
            sa.Column("role", sa.Text(), nullable=True),
            sa.Column("channels", JSONCol, nullable=False),
            sa.Column("primary_address", sa.Text(), nullable=False),
            sa.Column("language", sa.String(length=16), nullable=False),
            sa.Column("register", sa.String(length=32), nullable=False),
            sa.Column("consent_status", sa.String(length=32), nullable=False),
            sa.Column("dnc_flag", sa.Boolean(), nullable=False),
            sa.Column("verified_phone", sa.String(length=32), nullable=True),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("last_touch", UtcDateTime(), nullable=True),
            sa.Column("next_touch", UtcDateTime(), nullable=True),
            sa.Column("cadence_id", sa.String(length=64), nullable=True),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.Column(
                "desk",
                sa.Enum(
                    "operator",
                    "assistant",
                    "governance",
                    name="desk",
                    native_enum=False,
                    create_constraint=True,
                    length=16,
                ),
                nullable=False,
            ),
            sa.Column("audit_id", sa.String(length=26), nullable=False),
            sa.ForeignKeyConstraint(["coat_id"], ["coat.id"], name=op.f("fk_contact_coat_id_coat")),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_contact")),
            sa.UniqueConstraint(
                "coat_id",
                "desk",
                "primary_address",
                name=op.f("uq_contact_coat_id_desk_primary_address"),
            ),
        )
    )
    op.create_index(op.f("ix_contact_coat_id"), "contact", ["coat_id"], unique=False)
    op.create_index(op.f("ix_contact_desk"), "contact", ["desk"], unique=False)
    op.create_index(op.f("ix_contact_next_touch"), "contact", ["next_touch"], unique=False)
    tables.append(
        op.create_table(
            "experiment",
            sa.Column("coat_id", sa.String(length=64), nullable=False),
            sa.Column("hypothesis", sa.Text(), nullable=False),
            sa.Column("budget", sa.BigInteger(), nullable=False),
            sa.Column("currency", sa.String(length=3), nullable=False),
            sa.Column("deadline", sa.Date(), nullable=False),
            sa.Column("metric", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("result", sa.Text(), nullable=True),
            sa.Column("kill_reason", sa.Text(), nullable=True),
            sa.Column("playbook_ref", sa.Text(), nullable=True),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.Column(
                "desk",
                sa.Enum(
                    "operator",
                    "assistant",
                    "governance",
                    name="desk",
                    native_enum=False,
                    create_constraint=True,
                    length=16,
                ),
                nullable=False,
            ),
            sa.CheckConstraint("desk = 'operator'", name=op.f("ck_experiment_operator_only")),
            sa.ForeignKeyConstraint(
                ["coat_id"], ["coat.id"], name=op.f("fk_experiment_coat_id_coat")
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_experiment")),
        )
    )
    op.create_index(
        "ix_experiment_coat_id_status", "experiment", ["coat_id", "status"], unique=False
    )
    op.create_index(op.f("ix_experiment_desk"), "experiment", ["desk"], unique=False)
    tables.append(
        op.create_table(
            "handoff",
            sa.Column("coat_id", sa.String(length=64), nullable=False),
            sa.Column("handoff_json", JSONCol, nullable=False),
            sa.Column("source_event_id", sa.String(length=26), nullable=False),
            sa.Column("pushed_by", sa.String(length=16), nullable=False),
            sa.Column("taken_at", UtcDateTime(), nullable=True),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.CheckConstraint("pushed_by = 'assistant'", name=op.f("ck_handoff_pushed_by")),
            sa.ForeignKeyConstraint(["coat_id"], ["coat.id"], name=op.f("fk_handoff_coat_id_coat")),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_handoff")),
        )
    )
    op.create_index(op.f("ix_handoff_coat_id"), "handoff", ["coat_id"], unique=False)
    tables.append(
        op.create_table(
            "memory_record",
            sa.Column("store", sa.String(length=16), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("source_refs", JSONCol, nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("approved_by_owner", sa.Boolean(), nullable=False),
            sa.Column("expires_at", UtcDateTime(), nullable=True),
            sa.Column("vector_ref", sa.Text(), nullable=True),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.Column(
                "desk",
                sa.Enum(
                    "operator",
                    "assistant",
                    "governance",
                    name="desk",
                    native_enum=False,
                    create_constraint=True,
                    length=16,
                ),
                nullable=False,
            ),
            sa.Column("coat_id", sa.String(length=64), nullable=True),
            sa.Column("audit_id", sa.String(length=26), nullable=False),
            sa.CheckConstraint(
                "desk IN ('operator', 'assistant')", name=op.f("ck_memory_record_desk_is_a_desk")
            ),
            sa.CheckConstraint(
                "store <> 'owner_profile' OR desk = 'assistant'",
                name=op.f("ck_memory_record_owner_profile_assistant_only"),
            ),
            sa.CheckConstraint(
                "store IN ('episodic', 'semantic', 'procedural', 'owner_profile')",
                name=op.f("ck_memory_record_store"),
            ),
            sa.ForeignKeyConstraint(
                ["coat_id"], ["coat.id"], name=op.f("fk_memory_record_coat_id_coat")
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_record")),
        )
    )
    op.create_index(op.f("ix_memory_record_coat_id"), "memory_record", ["coat_id"], unique=False)
    op.create_index(op.f("ix_memory_record_desk"), "memory_record", ["desk"], unique=False)
    op.create_index(
        "ix_memory_record_desk_store_created_at",
        "memory_record",
        ["desk", "store", "created_at"],
        unique=False,
    )
    tables.append(
        op.create_table(
            "task",
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("owner_type", sa.String(length=16), nullable=False),
            sa.Column("owner_ref", sa.Text(), nullable=True),
            sa.Column("due", UtcDateTime(), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.Column("parent_task_id", sa.String(length=26), nullable=True),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.Column(
                "desk",
                sa.Enum(
                    "operator",
                    "assistant",
                    "governance",
                    name="desk",
                    native_enum=False,
                    create_constraint=True,
                    length=16,
                ),
                nullable=False,
            ),
            sa.Column("coat_id", sa.String(length=64), nullable=True),
            sa.Column("audit_id", sa.String(length=26), nullable=False),
            sa.CheckConstraint(
                "owner_type IN ('owner', 'staff', 'nour', 'freelancer')",
                name=op.f("ck_task_owner_type"),
            ),
            sa.CheckConstraint(
                "source IN ('brief', 'staff_request', 'experiment', 'renewal', 'handoff', 'owner')",
                name=op.f("ck_task_source"),
            ),
            sa.ForeignKeyConstraint(["coat_id"], ["coat.id"], name=op.f("fk_task_coat_id_coat")),
            sa.ForeignKeyConstraint(
                ["parent_task_id"], ["task.id"], name=op.f("fk_task_parent_task_id_task")
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_task")),
        )
    )
    op.create_index(op.f("ix_task_coat_id"), "task", ["coat_id"], unique=False)
    op.create_index("ix_task_coat_id_status", "task", ["coat_id", "status"], unique=False)
    op.create_index(op.f("ix_task_desk"), "task", ["desk"], unique=False)
    op.create_index(op.f("ix_task_due"), "task", ["due"], unique=False)
    tables.append(
        op.create_table(
            "conversation",
            sa.Column("coat_id", sa.String(length=64), nullable=False),
            sa.Column("contact_id", sa.String(length=26), nullable=True),
            sa.Column("channel", sa.String(length=64), nullable=False),
            sa.Column("thread_ref", sa.Text(), nullable=False),
            sa.Column("bucket", sa.String(length=16), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.Column(
                "desk",
                sa.Enum(
                    "operator",
                    "assistant",
                    "governance",
                    name="desk",
                    native_enum=False,
                    create_constraint=True,
                    length=16,
                ),
                nullable=False,
            ),
            sa.Column("audit_id", sa.String(length=26), nullable=False),
            sa.CheckConstraint(
                "bucket IN ('handles', 'draft', 'escalate', 'ignore')",
                name=op.f("ck_conversation_bucket"),
            ),
            sa.ForeignKeyConstraint(
                ["coat_id"], ["coat.id"], name=op.f("fk_conversation_coat_id_coat")
            ),
            sa.ForeignKeyConstraint(
                ["contact_id"], ["contact.id"], name=op.f("fk_conversation_contact_id_contact")
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation")),
            sa.UniqueConstraint(
                "coat_id",
                "channel",
                "thread_ref",
                name=op.f("uq_conversation_coat_id_channel_thread_ref"),
            ),
        )
    )
    op.create_index(op.f("ix_conversation_coat_id"), "conversation", ["coat_id"], unique=False)
    op.create_index(op.f("ix_conversation_desk"), "conversation", ["desk"], unique=False)
    tables.append(
        op.create_table(
            "decision_journal",
            sa.Column("approval_id", sa.String(length=26), nullable=False),
            sa.Column("category", sa.String(length=64), nullable=False),
            sa.Column("decision", sa.String(length=16), nullable=False),
            sa.Column("reason_text", sa.Text(), nullable=False),
            sa.Column("pattern_tags", JSONCol, nullable=False),
            sa.Column("decided_by_event_id", sa.String(length=26), nullable=False),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["approval_id"],
                ["approval.id"],
                name=op.f("fk_decision_journal_approval_id_approval"),
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_decision_journal")),
        )
    )
    op.create_index(
        "ix_decision_journal_category_created_at",
        "decision_journal",
        ["category", "created_at"],
        unique=False,
    )
    tables.append(
        op.create_table(
            "transaction",
            sa.Column("coat_id", sa.String(length=64), nullable=False),
            sa.Column("direction", sa.String(length=8), nullable=False),
            sa.Column("amount", sa.BigInteger(), nullable=False),
            sa.Column("currency", sa.String(length=3), nullable=False),
            sa.Column("counterpart_ref", sa.Text(), nullable=False),
            sa.Column("beneficiary_id", sa.String(length=26), nullable=True),
            sa.Column("experiment_id", sa.String(length=26), nullable=True),
            sa.Column("task_id", sa.String(length=26), nullable=True),
            sa.Column("approval_id", sa.String(length=26), nullable=True),
            sa.Column("holder", sa.String(length=64), nullable=False),
            sa.Column("card_ref", sa.Text(), nullable=True),
            sa.Column("card_auth_ref", sa.Text(), nullable=True),
            sa.Column("bank_ref", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("occurred_at", UtcDateTime(), nullable=False),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.Column(
                "desk",
                sa.Enum(
                    "operator",
                    "assistant",
                    "governance",
                    name="desk",
                    native_enum=False,
                    create_constraint=True,
                    length=16,
                ),
                nullable=False,
            ),
            sa.Column("audit_id", sa.String(length=26), nullable=False),
            sa.CheckConstraint("direction IN ('in', 'out')", name=op.f("ck_transaction_direction")),
            sa.CheckConstraint(
                "status IN ('prepared', 'authorized', 'declined', 'released', 'settled', 'reconciled')",
                name=op.f("ck_transaction_status"),
            ),
            sa.ForeignKeyConstraint(
                ["beneficiary_id"],
                ["beneficiary.id"],
                name=op.f("fk_transaction_beneficiary_id_beneficiary"),
            ),
            sa.ForeignKeyConstraint(
                ["coat_id"], ["coat.id"], name=op.f("fk_transaction_coat_id_coat")
            ),
            sa.ForeignKeyConstraint(
                ["experiment_id"],
                ["experiment.id"],
                name=op.f("fk_transaction_experiment_id_experiment"),
            ),
            sa.ForeignKeyConstraint(
                ["task_id"], ["task.id"], name=op.f("fk_transaction_task_id_task")
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_transaction")),
            sa.UniqueConstraint("card_auth_ref", name=op.f("uq_transaction_card_auth_ref")),
        )
    )
    op.create_index(
        "ix_transaction_coat_id_occurred_at",
        "transaction",
        ["coat_id", "occurred_at"],
        unique=False,
    )
    op.create_index(op.f("ix_transaction_desk"), "transaction", ["desk"], unique=False)
    op.create_index(
        "ix_transaction_holder_occurred_at", "transaction", ["holder", "occurred_at"], unique=False
    )
    op.create_index(op.f("ix_transaction_status"), "transaction", ["status"], unique=False)
    tables.append(
        op.create_table(
            "message",
            sa.Column("coat_id", sa.String(length=64), nullable=False),
            sa.Column("conversation_id", sa.String(length=26), nullable=False),
            sa.Column("direction", sa.String(length=8), nullable=False),
            sa.Column("channel", sa.String(length=64), nullable=False),
            sa.Column("body_ref", sa.Text(), nullable=False),
            sa.Column("body_text", sa.Text(), nullable=True),
            sa.Column("language", sa.String(length=16), nullable=False),
            sa.Column("transcript_ref", sa.Text(), nullable=True),
            sa.Column("sent_by", sa.String(length=16), nullable=False),
            sa.Column("approval_id", sa.String(length=26), nullable=True),
            sa.Column("critic_score", sa.Float(), nullable=True),
            sa.Column("provider_msg_id", sa.Text(), nullable=True),
            sa.Column("audio_ref", sa.Text(), nullable=True),
            sa.Column("audio_delete_after", UtcDateTime(), nullable=True),
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("created_at", UtcDateTime(), nullable=False),
            sa.Column("updated_at", UtcDateTime(), nullable=False),
            sa.Column(
                "desk",
                sa.Enum(
                    "operator",
                    "assistant",
                    "governance",
                    name="desk",
                    native_enum=False,
                    create_constraint=True,
                    length=16,
                ),
                nullable=False,
            ),
            sa.Column("audit_id", sa.String(length=26), nullable=False),
            sa.CheckConstraint("direction IN ('in', 'out')", name=op.f("ck_message_direction")),
            sa.CheckConstraint(
                "sent_by IN ('nour', 'owner', 'staff', 'contact')", name=op.f("ck_message_sent_by")
            ),
            sa.ForeignKeyConstraint(["coat_id"], ["coat.id"], name=op.f("fk_message_coat_id_coat")),
            sa.ForeignKeyConstraint(
                ["conversation_id"],
                ["conversation.id"],
                name=op.f("fk_message_conversation_id_conversation"),
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_message")),
            sa.UniqueConstraint("provider_msg_id", name=op.f("uq_message_provider_msg_id")),
        )
    )
    op.create_index(op.f("ix_message_coat_id"), "message", ["coat_id"], unique=False)
    op.create_index(
        "ix_message_conversation_id_created_at",
        "message",
        ["conversation_id", "created_at"],
        unique=False,
    )
    op.create_index(op.f("ix_message_desk"), "message", ["desk"], unique=False)

    metadata = _flagged_metadata(tables)
    missing = set(TABLE_FLAGS) - set(metadata.tables)
    if missing:  # pragma: no cover - a frozen flag must name a table this revision creates
        raise RuntimeError(
            f"TABLE_FLAGS names tables this revision does not create: {sorted(missing)}"
        )
    for statement in trigger_ddl(dialect, metadata):
        op.execute(statement)
    if dialect == "postgresql":
        for statement in pg_roles_ddl(metadata):
            op.execute(statement)


def downgrade() -> None:
    """Drop every table (triggers and policies go with them); roles and the ``auditor`` schema
    are left in place on Postgres (cluster-wide objects the operator manages)."""
    op.drop_index(op.f("ix_message_desk"), table_name="message")
    op.drop_index("ix_message_conversation_id_created_at", table_name="message")
    op.drop_index(op.f("ix_message_coat_id"), table_name="message")
    op.drop_table("message")
    op.drop_index(op.f("ix_transaction_status"), table_name="transaction")
    op.drop_index("ix_transaction_holder_occurred_at", table_name="transaction")
    op.drop_index(op.f("ix_transaction_desk"), table_name="transaction")
    op.drop_index("ix_transaction_coat_id_occurred_at", table_name="transaction")
    op.drop_table("transaction")
    op.drop_index("ix_decision_journal_category_created_at", table_name="decision_journal")
    op.drop_table("decision_journal")
    op.drop_index(op.f("ix_conversation_desk"), table_name="conversation")
    op.drop_index(op.f("ix_conversation_coat_id"), table_name="conversation")
    op.drop_table("conversation")
    op.drop_index(op.f("ix_task_due"), table_name="task")
    op.drop_index(op.f("ix_task_desk"), table_name="task")
    op.drop_index("ix_task_coat_id_status", table_name="task")
    op.drop_index(op.f("ix_task_coat_id"), table_name="task")
    op.drop_table("task")
    op.drop_index("ix_memory_record_desk_store_created_at", table_name="memory_record")
    op.drop_index(op.f("ix_memory_record_desk"), table_name="memory_record")
    op.drop_index(op.f("ix_memory_record_coat_id"), table_name="memory_record")
    op.drop_table("memory_record")
    op.drop_index(op.f("ix_handoff_coat_id"), table_name="handoff")
    op.drop_table("handoff")
    op.drop_index(op.f("ix_experiment_desk"), table_name="experiment")
    op.drop_index("ix_experiment_coat_id_status", table_name="experiment")
    op.drop_table("experiment")
    op.drop_index(op.f("ix_contact_next_touch"), table_name="contact")
    op.drop_index(op.f("ix_contact_desk"), table_name="contact")
    op.drop_index(op.f("ix_contact_coat_id"), table_name="contact")
    op.drop_table("contact")
    op.drop_index(op.f("ix_category_state_desk"), table_name="category_state")
    op.drop_table("category_state")
    op.drop_index(op.f("ix_beneficiary_coat_id"), table_name="beneficiary")
    op.drop_table("beneficiary")
    op.drop_index(op.f("ix_approval_desk"), table_name="approval")
    op.drop_index(
        "ix_approval_decision", table_name="approval", postgresql_where=sa.text("decision IS NULL")
    )
    op.drop_index(op.f("ix_approval_coat_id"), table_name="approval")
    op.drop_table("approval")
    op.drop_index(op.f("ix_vault_secret_entity_ref"), table_name="vault_secret")
    op.drop_table("vault_secret")
    op.drop_table("timer_slot")
    op.drop_index(op.f("ix_skill_desk"), table_name="skill")
    op.drop_table("skill")
    op.drop_index(op.f("ix_second_channel_challenge_ref"), table_name="second_channel_challenge")
    op.drop_table("second_channel_challenge")
    op.drop_index(op.f("ix_release_desk"), table_name="release")
    op.drop_table("release")
    op.drop_index(op.f("ix_readback_pending_expires_at"), table_name="readback_pending")
    op.drop_index(op.f("ix_readback_pending_desk"), table_name="readback_pending")
    op.drop_table("readback_pending")
    op.drop_index(
        "ix_pending_owner_message_sent_at",
        table_name="pending_owner_message",
        postgresql_where=sa.text("sent_at IS NULL"),
    )
    op.drop_index(op.f("ix_pending_owner_message_desk"), table_name="pending_owner_message")
    op.drop_table("pending_owner_message")
    op.drop_index(op.f("ix_passphrase_attempt_at"), table_name="passphrase_attempt")
    op.drop_table("passphrase_attempt")
    op.drop_table("owner")
    op.drop_index(op.f("ix_model_trace_event_id"), table_name="model_trace")
    op.drop_index(op.f("ix_model_trace_desk"), table_name="model_trace")
    op.drop_table("model_trace")
    op.drop_index("ix_incident_type_detected_at", table_name="incident")
    op.drop_index(op.f("ix_incident_desk"), table_name="incident")
    op.drop_index(op.f("ix_incident_coat_id"), table_name="incident")
    op.drop_table("incident")
    op.drop_index("ix_inbox_event_desk_acked_at_priority_received_at", table_name="inbox_event")
    op.drop_index(op.f("ix_inbox_event_desk"), table_name="inbox_event")
    op.drop_index(op.f("ix_inbox_event_coat_id"), table_name="inbox_event")
    op.drop_table("inbox_event")
    op.drop_index(
        "ix_freeze_state_released_at",
        table_name="freeze_state",
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.drop_table("freeze_state")
    op.drop_index(op.f("ix_found_instruction_event_id"), table_name="found_instruction")
    op.drop_index(op.f("ix_found_instruction_desk"), table_name="found_instruction")
    op.drop_index(op.f("ix_found_instruction_coat_id"), table_name="found_instruction")
    op.drop_table("found_instruction")
    op.drop_index(op.f("ix_document_tier"), table_name="document")
    op.drop_index(op.f("ix_document_expiry"), table_name="document")
    op.drop_index("ix_document_entity_ref_type", table_name="document")
    op.drop_table("document")
    op.drop_table("dnc_entry")
    op.drop_table("coat")
    op.drop_index("ix_card_authorization_card_ref_created_at", table_name="card_authorization")
    op.drop_table("card_authorization")
    op.drop_index(op.f("ix_card_desk"), table_name="card")
    op.drop_table("card")
    op.drop_index(
        op.f("ix_auditor_auditor_report_day"), table_name="auditor_report", schema="auditor"
    )
    op.drop_table("auditor_report", schema="auditor")
    op.drop_index(op.f("ix_audit_event_ts"), table_name="audit_event")
    op.drop_index(op.f("ix_audit_event_invocation_id"), table_name="audit_event")
    op.drop_index(op.f("ix_audit_event_event_id"), table_name="audit_event")
    op.drop_index(op.f("ix_audit_event_desk"), table_name="audit_event")
    op.drop_index(op.f("ix_audit_event_coat_id"), table_name="audit_event")
    op.drop_table("audit_event")
    op.drop_table("audit_chain_head")
