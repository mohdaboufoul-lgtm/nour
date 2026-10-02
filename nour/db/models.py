"""ORM models: the sixteen SPEC §15 entities and the infrastructure tables (DESIGN §3.8, §5;
SPEC §12 §13 §15).

One mapper per table, all registered on ``Base`` (``nour/db/base.py``), so ``create_schema(engine,
Base.metadata)`` — what ``tests/conftest.py`` runs for every test — installs the whole schema with
its triggers. Every mapper carries the four wall markers ``DeskWallGuard`` and ``pg_roles_ddl``
read (``__scope__``, ``__append_only__``, ``__single_transition__``, ``__forward_only__``); the
constraints the walls lean on are CHECKs in the database, not conventions:

* ``document``: ``CHECK (tier <> 2 OR content_text IS NULL)`` — a Tier 2 document is metadata-only
  in the index (SPEC §11; DESIGN §4b);
* ``memory_record``: ``CHECK (store <> 'owner_profile' OR desk = 'assistant')`` — the owner profile
  can only ever sit in the Assistant partition (SPEC §8 §5);
* ``experiment``: ``CHECK (desk = 'operator')`` — experiments are Operator-only (SPEC §15);
* ``approval``: ``CHECK (tier = 'K')`` — only ask-first items queue for the owner (SPEC §6);
* ``handoff``: ``CHECK (pushed_by = 'assistant')`` — the one-way gate (SPEC §5).

Beyond the four DESIGN §3.7 markers, two more from ``nour/db/base.py`` carry DESIGN §5's
mutability column into the database (DESIGN §4c "checked and burnt in one transaction";
THREAT_REVIEW top-10 #1 "executed, not string-asserted"):

* ``__immutable__`` — columns frozen at INSERT (trigger on both dialects + ORM guard): the
  identity and money of an ``approval`` and the nonce of a ``release`` cannot be rewritten
  after the owner answered or the token was burnt; the ingress-recorded authenticity of an
  ``inbox_event`` (``sender``, ``signature_valid``, ``passphrase_attempt``) and the body of a
  ``handoff`` never change; a ``document`` version changes only its ``share_log``; an
  ``incident`` changes only to resolve. A single-transition table also keeps its rows (no
  DELETE, no REPLACE), so a burnt nonce cannot be re-armed by deleting and re-inserting it.
* ``__desk_partitioned__`` — ``inbox_event`` and ``pending_owner_message`` are SHARED for the
  grants but partitioned per desk for a desk token's reads, updates and deletes (the Operator
  never sees the owner's mailbox or owner-thread bodies the Assistant desk receives, nor the
  briefs, read-backs and quoted instructions the Assistant drafts for the owner from them,
  SPEC §5; DESIGN §4a); any desk may insert a row for either desk (publishing an event to the
  other desk) and governance sees every row (:data:`DESK_PARTITIONED_TABLES`).

Per-role write grants (``nour.db.engine.GRANTS_KEY``, enforced by ``pg_roles_ddl`` on Postgres
and by ``DeskWallGuard`` on SQLite) narrow the scope defaults where DESIGN §5 names a writer:
desks only ack ``inbox_event`` rows and insert them without the authenticity columns; only the
Assistant authors a ``handoff`` and only the Operator takes one; only ingress records a
``passphrase_attempt``; desks insert ``dnc_entry`` and ``freeze_state`` rows but never change
or delete them (the owner CLI and the kill switch do); ``coat`` is written by governance; the
decision columns of an ``approval`` and ``release.burnt_at`` are the only desk-updatable columns
of those tables and nobody deletes them. ``owner.passphrase_hash`` / ``passphrase_fp`` are mapped
``deferred`` so a default ``select(OwnerRow)`` never names them — the desk roles' column grant
on Postgres would refuse the statement, and ``DeskWallGuard`` refuses a desk that undefers them.

Conventions (DESIGN §5): ``id`` is a 26-char ULID the caller takes from ``IdGenerator`` (never a
database default), ``created_at``/``updated_at`` come from the process clock, money is ``BIGINT``
fils plus a ``currency`` column, JSON is ``JSON``/``JSONB``, closed enums are ``TEXT`` + ``CHECK``,
encrypted fields are ``EncryptedBytes`` (only ``Ciphertext`` minted by ``FieldCipher`` is accepted),
and hashes of Tier 2 values are keyed HMACs. Two tables bend the id convention on purpose:
``coat.id`` *is* the ``CoatId`` slug (``"buzz-avenue"``), because every ``coat_id`` column holds a
``CoatId`` and ``REFERENCES coat(id)``; ``audit_chain_head`` is the ``singleton = 1`` row the audit
log locks.

Importing this module has no side effect beyond registering the mappers: no engine, no session,
no clock read. ``brain_metadata`` / ``auditor_metadata`` are split copies of ``Base.metadata`` for
the two Postgres schemas (``public`` and ``auditor``); ``Base.metadata`` stays the metadata the
ORM maps (the auditor's read-only session maps ``auditor_report`` through it for the harness).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from nour.core.clock import process_now
from nour.core.types import (
    ActionStatus,
    ActionTier,
    Actor,
    CoatId,
    Desk,
    FreezeScope,
    IncidentType,
    MemoryKind,
    Origin,
    PassphraseOutcome,
    SourceKind,
    Ulid,
)
from nour.db.base import (
    MAPPER_INFO_KEY,
    NAMING_CONVENTION,
    TABLE_INFO_KEY,
    Base,
    CoatMixin,
    DeskMixin,
    EncryptedBytes,
    JSONCol,
    MoneyCol,
    RecordMixin,
    Scope,
    UtcDateTime,
)
from nour.db.engine import (
    ASSISTANT_ROLE,
    GRANTS_KEY,
    INGRESS_ROLE,
    OPERATOR_ROLE,
    SCHEDULER_ROLE,
)

AUDITOR_SCHEMA = "auditor"
"""The Postgres schema ``auditor_report`` lives in (DESIGN §3.8); translated away on SQLite."""

ULID_LEN = 26
COAT_ID_LEN = 64

# --------------------------------------------------------------------------- closed value sets
# TEXT + CHECK for every closed enum (DESIGN §5 "Enums are TEXT + CHECK"); the tuples mirror the
# core enums where one exists, so a value the code cannot produce cannot be stored either.

BUCKETS: tuple[str, ...] = ("handles", "draft", "escalate", "ignore")
DIRECTIONS: tuple[str, ...] = ("in", "out")
SENT_BY: tuple[str, ...] = ("nour", "owner", "staff", "contact")
TASK_OWNER_TYPES: tuple[str, ...] = ("owner", "staff", "nour", "freelancer")
TASK_SOURCES: tuple[str, ...] = (
    "brief",
    "staff_request",
    "experiment",
    "renewal",
    "handoff",
    "owner",
)
DECISIONS: tuple[str, ...] = ("approve", "reject", "expired")
AUDIT_PHASES: tuple[str, ...] = ("opened", "closed")
TRANSACTION_STATES: tuple[str, ...] = (
    "prepared",
    "authorized",
    "declined",
    "released",
    "settled",
    "reconciled",
)
DETECTED_BY: tuple[str, ...] = ("nour", "auditor", "owner", "system")
MINTED_BY: tuple[str, ...] = ("gate", "approval")
MEMORY_STORES: tuple[str, ...] = tuple(kind.value for kind in MemoryKind)
PASSPHRASE_OUTCOMES: tuple[str, ...] = tuple(outcome.value for outcome in PassphraseOutcome)
ACTION_TIERS: tuple[str, ...] = tuple(tier.value for tier in ActionTier)
ACTION_STATUSES: tuple[str, ...] = tuple(status.value for status in ActionStatus)
ACTORS: tuple[str, ...] = tuple(actor.value for actor in Actor)
FREEZE_SCOPES: tuple[str, ...] = tuple(scope.value for scope in FreezeScope)
INCIDENT_TYPES: tuple[str, ...] = tuple(kind.value for kind in IncidentType)
SOURCE_KINDS: tuple[str, ...] = tuple(kind.value for kind in SourceKind)
ORIGINS: tuple[str, ...] = tuple(origin.value for origin in Origin)
DATA_TIERS: tuple[int, ...] = (0, 1, 2, 3)
REAL_DESKS: tuple[str, ...] = (Desk.OPERATOR.value, Desk.ASSISTANT.value)
"""The two desks rows can belong to; ``governance`` is a process, never a partition with rows."""

RUNTIME_ROLES: tuple[str, ...] = (OPERATOR_ROLE, ASSISTANT_ROLE, INGRESS_ROLE, SCHEDULER_ROLE)
"""The four writing roles (the auditor only reads SHARED tables and appends its report)."""

APPROVAL_DECISION_COLUMNS: tuple[str, ...] = (
    "decided_at",
    "decision",
    "reason",
    "passphrase_verified",
    "second_channel_confirmed",
    "decided_via",
    "decided_by_event_id",
)
"""The one transition of an ``approval`` row (DESIGN §5): the only columns a desk may UPDATE."""

INBOX_ACK_COLUMNS: tuple[str, ...] = ("acked_at", "parked_until", "lock_until", "error")
"""``EventBus.ack`` / ``park`` / the ``lock_until`` lease: the only ``inbox_event`` columns a desk
may UPDATE (DESIGN §5.2 "desks UPDATE ack columns")."""

INBOX_AUTH_COLUMNS: tuple[str, ...] = ("signature_valid", "passphrase_attempt", "attempt_id")
"""The ingress-recorded authenticity of an ``inbox_event`` (DESIGN §4d): only ingress may write
them; a desk-published event (handoff, approval decision, read-back) gets the column default —
``signature_valid = false``, no attempt — so a planted row can never verify."""

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
"""Every ``inbox_event`` column but :data:`INBOX_AUTH_COLUMNS`: what a desk may INSERT."""


def _grant(
    insert: bool | tuple[str, ...] = False,
    update: bool | tuple[str, ...] = False,
    delete: bool = False,
) -> dict[str, Any]:
    """One role's entry for ``GRANTS_KEY`` (``nour.db.engine``)."""
    return {"insert": insert, "update": update, "delete": delete}


def _grants_for(roles: Iterable[str], **verbs: Any) -> dict[str, dict[str, Any]]:
    """The same grant for several roles."""
    return {role: _grant(**verbs) for role in roles}


def _lit(value: str | int) -> str:
    if isinstance(value, int):
        return str(value)
    return "'" + value.replace("'", "''") + "'"


def _in(column: str, values: Iterable[str | int]) -> str:
    """``column IN ('a', 'b')`` for a CHECK constraint."""
    return f"{column} IN ({', '.join(_lit(v) for v in values)})"


def _in_or_null(column: str, values: Iterable[str | int]) -> str:
    return f"{column} IS NULL OR {_in(column, values)}"


# --------------------------------------------------------------------------- mixins


class TimestampMixin:
    """``created_at`` / ``updated_at`` from the process clock (DESIGN §5: never a DB default), for
    the tables whose primary key is not a ULID (``coat``, ``audit_chain_head``)."""

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=process_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=process_now, onupdate=process_now
    )


class AuditedMixin:
    """§12 §16: rows a tool handler writes outside a port carry the ``audit_id`` of the ``opened``
    audit row, so ``ActionCoverage.verify`` covers non-port effects too (DESIGN §4h)."""

    audit_id: Mapped[Ulid] = mapped_column(String(ULID_LEN), nullable=False)


# =========================================================================== §15 entities


class OwnerRow(Base, RecordMixin):
    """SPEC §15 Owner, §6 §12 §13: the one owner record (``singleton = 1``).

    GOVERNANCE_ONLY: only the governance processes (CLI, ingress) write it; on Postgres the desk
    roles get a column grant that excludes every ``passphrase*`` column, on SQLite the guard
    lets a desk read it and never write it. ``passphrase_hash`` is argon2id and
    ``passphrase_fp`` the LeakGuard fingerprint — the passphrase itself is never persisted. Both
    are mapped ``deferred`` (THREAT_REVIEW 6.3): a default ``select(OwnerRow)`` /
    ``session.get`` renders neither, so a desk's read of the owner number, second channel and
    quiet hours works under the Postgres column grant, and a desk that undefers them is refused
    by ``DeskWallGuard``; the governance verifier loads them with
    ``options(undefer(OwnerRow.passphrase_hash, OwnerRow.passphrase_fp))``.
    """

    __tablename__ = "owner"
    __scope__ = Scope.GOVERNANCE_ONLY
    __table_args__ = (CheckConstraint("singleton = 1", name="singleton"),)

    singleton: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, default=1)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    whatsapp_number: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    passphrase_hash: Mapped[str] = mapped_column(Text, nullable=False, deferred=True)
    passphrase_fp: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, deferred=True)
    second_channel: Mapped[dict[str, Any]] = mapped_column(JSONCol, nullable=False)
    deputy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quiet_hours: Mapped[dict[str, Any]] = mapped_column(JSONCol, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    deputy_mode_since: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class CoatRow(Base, TimestampMixin):
    """SPEC §15 Coat, §5: one row per company, synced from ``config/coats/<slug>.yaml`` at start
    (yaml wins, logged). ``id`` is the slug — the ``CoatId`` every ``coat_id`` column references —
    and ``slug`` repeats it (``CHECK (slug = id)``) so the §5 column list stays intact. The sync
    is a governance write: the desk roles read coats and never insert, update or delete them
    (``banking_ref``, ``approval_rules`` and ``desks_allowed`` are what the resolver trusts)."""

    __tablename__ = "coat"
    __scope__ = Scope.SHARED
    __table_args__ = (
        CheckConstraint("slug = id", name="slug_is_id"),
        {"info": {GRANTS_KEY: _grants_for((OPERATOR_ROLE, ASSISTANT_ROLE))}},
    )

    id: Mapped[CoatId] = mapped_column(String(COAT_ID_LEN), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    slug: Mapped[CoatId] = mapped_column(String(COAT_ID_LEN), nullable=False, unique=True)
    legal_entity: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str] = mapped_column(Text, nullable=False)
    email_identity: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    whatsapp_line: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    signature_ref: Mapped[str] = mapped_column(Text, nullable=False)
    letterhead_ref: Mapped[str] = mapped_column(Text, nullable=False)
    tone_guide_ref: Mapped[str] = mapped_column(Text, nullable=False)
    knowledge_pack_ref: Mapped[str] = mapped_column(Text, nullable=False)
    mandate: Mapped[dict[str, Any]] = mapped_column(JSONCol, nullable=False)
    approval_rules: Mapped[dict[str, Any]] = mapped_column(JSONCol, nullable=False)
    allowed_activities: Mapped[list[Any]] = mapped_column(JSONCol, nullable=False)
    banking_ref: Mapped[str] = mapped_column(Text, nullable=False)
    desks_allowed: Mapped[list[Any]] = mapped_column(JSONCol, nullable=False)
    config_hash: Mapped[str] = mapped_column(Text, nullable=False)


class ContactRow(Base, RecordMixin, DeskMixin, AuditedMixin):
    """SPEC §15 Contact, §5 §9: CRM rows partitioned per desk (DESK_ROW) and coat; the DNC flag is
    global across coats through ``dnc_entry``. ``primary_address`` is normalised (E.164 / lower-
    cased IDNA mail) before it is stored or compared (THREAT_REVIEW top-10 #9)."""

    __tablename__ = "contact"
    __scope__ = Scope.DESK_ROW
    __table_args__ = (
        ForeignKeyConstraint(["coat_id"], ["coat.id"]),
        UniqueConstraint("coat_id", "desk", "primary_address"),
    )

    coat_id: Mapped[CoatId] = mapped_column(String(COAT_ID_LEN), nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    org: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str | None] = mapped_column(Text, nullable=True)
    channels: Mapped[dict[str, Any]] = mapped_column(JSONCol, nullable=False)
    primary_address: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    register: Mapped[str] = mapped_column(String(32), nullable=False)
    consent_status: Mapped[str] = mapped_column(String(32), nullable=False)
    dnc_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verified_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    last_touch: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    next_touch: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True, index=True)
    cadence_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DncEntryRow(Base, RecordMixin, AuditedMixin):
    """SPEC §9 §13: the do-not-contact list, global across coats and desks (SHARED); rows are
    inserted by opt-outs (a desk may INSERT) and changed or removed only through the owner CLI
    (governance: the desk roles have no UPDATE or DELETE). ``address`` is normalised."""

    __tablename__ = "dnc_entry"
    __scope__ = Scope.SHARED
    __table_args__ = (
        {"info": {GRANTS_KEY: _grants_for((OPERATOR_ROLE, ASSISTANT_ROLE), insert=True)}},
    )

    address: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    set_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class ConversationRow(Base, RecordMixin, DeskMixin, AuditedMixin):
    """SPEC §15 Conversation, §5: one row per thread, partitioned per desk (DESK_ROW);
    ``bucket`` is the triage decision (handles / draft / escalate / ignore)."""

    __tablename__ = "conversation"
    __scope__ = Scope.DESK_ROW
    __table_args__ = (
        ForeignKeyConstraint(["coat_id"], ["coat.id"]),
        UniqueConstraint("coat_id", "channel", "thread_ref"),
        CheckConstraint(_in("bucket", BUCKETS), name="bucket"),
    )

    coat_id: Mapped[CoatId] = mapped_column(String(COAT_ID_LEN), nullable=False, index=True)
    contact_id: Mapped[Ulid | None] = mapped_column(
        String(ULID_LEN), ForeignKey("contact.id"), nullable=True
    )
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    thread_ref: Mapped[str] = mapped_column(Text, nullable=False)
    bucket: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class MessageRow(Base, RecordMixin, DeskMixin, AuditedMixin):
    """SPEC §15 Message, §9 §11: insert-only in practice (``audio_ref`` is nulled by retention
    after 7 days); bodies live in encrypted object storage behind ``body_ref`` and ``body_text``
    holds redacted Tier 0/1 text only. ``provider_msg_id`` is unique where not null (§13 replay)."""

    __tablename__ = "message"
    __scope__ = Scope.DESK_ROW
    __table_args__ = (
        ForeignKeyConstraint(["coat_id"], ["coat.id"]),
        UniqueConstraint("provider_msg_id"),
        CheckConstraint(_in("direction", DIRECTIONS), name="direction"),
        CheckConstraint(_in("sent_by", SENT_BY), name="sent_by"),
        Index("ix_message_conversation_id_created_at", "conversation_id", "created_at"),
    )

    coat_id: Mapped[CoatId] = mapped_column(String(COAT_ID_LEN), nullable=False, index=True)
    conversation_id: Mapped[Ulid] = mapped_column(
        String(ULID_LEN), ForeignKey("conversation.id"), nullable=False
    )
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    body_ref: Mapped[str] = mapped_column(Text, nullable=False)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    transcript_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_by: Mapped[str] = mapped_column(String(16), nullable=False)
    approval_id: Mapped[Ulid | None] = mapped_column(String(ULID_LEN), nullable=True)
    critic_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    provider_msg_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_delete_after: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class TaskRow(Base, RecordMixin, DeskMixin, CoatMixin, AuditedMixin):
    """SPEC §15 Task, §5: the shared master board, partitioned per desk (DESK_ROW); staff see
    only their coat's tasks through the desk that serves them."""

    __tablename__ = "task"
    __scope__ = Scope.DESK_ROW
    __table_args__ = (
        ForeignKeyConstraint(["coat_id"], ["coat.id"]),
        CheckConstraint(_in("owner_type", TASK_OWNER_TYPES), name="owner_type"),
        CheckConstraint(_in("source", TASK_SOURCES), name="source"),
        Index("ix_task_coat_id_status", "coat_id", "status"),
    )

    title: Mapped[str] = mapped_column(Text, nullable=False)
    owner_type: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    due: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    parent_task_id: Mapped[Ulid | None] = mapped_column(
        String(ULID_LEN), ForeignKey("task.id"), nullable=True
    )


class ApprovalRow(Base, RecordMixin, DeskMixin, CoatMixin):
    """SPEC §15 Approval, §6 §12: one row per tier K item (``CHECK (tier = 'K')``), inserted by
    ``ApprovalsQueue.enqueue`` and decided exactly once — every decision column is in
    ``__single_transition__`` (ORM guard + DB trigger: settable only while NULL) and every other
    column is ``__immutable__``, so what the owner answered ("approve 12": ``seq``, ``action_json``,
    ``amount``, ``currency``, ``item_ref``, ``expires_at`` …) is exactly what was queued, before
    and after the decision (DESIGN §5 "insert + ONE transition"; THREAT_REVIEW top-10 #3: money
    text is rendered from this row). Rows are never deleted or replaced. ``seq`` is the short
    number the owner answers with, assigned by the queue inside the serialised writer (there is no
    cross-dialect autoincrement for a non-key column). ``action_json`` is the scrubbed
    ``ResolvedAction``; ``item_ref`` is the proposal id (TEXT, DESIGN §5). On Postgres every
    runtime role may INSERT and UPDATE the decision columns only; nobody may DELETE."""

    __tablename__ = "approval"
    __scope__ = Scope.SHARED
    __single_transition__ = APPROVAL_DECISION_COLUMNS
    __immutable__ = (
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
    )
    __table_args__ = (
        ForeignKeyConstraint(["coat_id"], ["coat.id"]),
        CheckConstraint("tier = 'K'", name="tier_is_k"),
        CheckConstraint(_in_or_null("decision", DECISIONS), name="decision"),
        Index(
            "ix_approval_decision",
            "decision",
            postgresql_where=sql_text("decision IS NULL"),
        ),
        {
            "info": {
                GRANTS_KEY: _grants_for(
                    RUNTIME_ROLES, insert=True, update=APPROVAL_DECISION_COLUMNS
                )
            }
        },
    )

    seq: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    item_type: Mapped[str] = mapped_column(String(64), nullable=False)
    item_ref: Mapped[str] = mapped_column(Text, nullable=False)
    action_json: Mapped[dict[str, Any]] = mapped_column(JSONCol, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[str] = mapped_column(String(1), nullable=False)
    amount: Mapped[int | None] = mapped_column(MoneyCol, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    draft_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    passphrase_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    second_channel_confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    decided_via: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decided_by_event_id: Mapped[Ulid | None] = mapped_column(String(ULID_LEN), nullable=True)
    trigger_event_id: Mapped[Ulid] = mapped_column(String(ULID_LEN), nullable=False)


class DecisionJournalRow(Base, RecordMixin):
    """SPEC §15 DecisionJournal, §8 §12: append-only record of every owner decision; the input to
    graduated autonomy. UPDATE/DELETE are refused by trigger, grant and ORM guard."""

    __tablename__ = "decision_journal"
    __scope__ = Scope.SHARED
    __append_only__ = True
    __table_args__ = (Index("ix_decision_journal_category_created_at", "category", "created_at"),)

    approval_id: Mapped[Ulid] = mapped_column(
        String(ULID_LEN), ForeignKey("approval.id"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_text: Mapped[str] = mapped_column(Text, nullable=False)
    pattern_tags: Mapped[list[Any]] = mapped_column(JSONCol, nullable=False)
    decided_by_event_id: Mapped[Ulid] = mapped_column(String(ULID_LEN), nullable=False)


class AuditEventRow(Base, DeskMixin, CoatMixin):
    """SPEC §15 AuditEvent, §2 §12: the append-only, hash-chained log — two rows per action
    (``opened`` before the side effect, ``closed`` after, ``UNIQUE (invocation_id, phase)``).

    No ``updated_at``: nothing about a row ever changes. ``prev_hash`` / ``entry_hash`` chain the
    rows behind the locked ``audit_chain_head``; ``input_hash`` / ``output_hash`` are hashes of
    ``LeakGuard.safe_mapping``-scrubbed canonical JSON, so only hashes of the objects are stored.
    No foreign key on ``coat_id``: a refusal for an unknown coat must still be logged.
    """

    __tablename__ = "audit_event"
    __scope__ = Scope.SHARED
    __append_only__ = True
    __table_args__ = (
        UniqueConstraint("invocation_id", "phase"),
        CheckConstraint(_in("phase", AUDIT_PHASES), name="phase"),
        CheckConstraint(_in("actor", ACTORS), name="actor"),
        CheckConstraint(_in("status", ACTION_STATUSES), name="status"),
        CheckConstraint(_in_or_null("tier", ACTION_TIERS), name="tier"),
        CheckConstraint(_in("data_tier", DATA_TIERS), name="data_tier"),
    )

    id: Mapped[Ulid] = mapped_column(String(ULID_LEN), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=process_now)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    ts: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tier: Mapped[str | None] = mapped_column(String(1), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    phase: Mapped[str] = mapped_column(String(8), nullable=False)
    counterpart: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount: Mapped[int | None] = mapped_column(MoneyCol, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    approval_id: Mapped[Ulid | None] = mapped_column(String(ULID_LEN), nullable=True)
    data_tier: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    input_hash: Mapped[str] = mapped_column(Text, nullable=False)
    output_hash: Mapped[str] = mapped_column(Text, nullable=False)
    event_id: Mapped[Ulid | None] = mapped_column(String(ULID_LEN), nullable=True, index=True)
    invocation_id: Mapped[Ulid] = mapped_column(String(ULID_LEN), nullable=False, index=True)
    prev_hash: Mapped[str] = mapped_column(Text, nullable=False)
    entry_hash: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        unique=True,
        comment=(
            "Keyed HMAC (nour.core.hashing.keyed_hash with the audit chain key) over the "
            "canonical row and prev_hash, computed by nour/audit/log.py; never a plain sha256 "
            "(SPEC §12; THREAT_REVIEW 6.6)."
        ),
    )
    config_hash: Mapped[str] = mapped_column(Text, nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False)


class TransactionRow(Base, RecordMixin, DeskMixin, AuditedMixin):
    """SPEC §15 Transaction, §10: the ledger. ``status`` moves forward only along
    ``TRANSACTION_STATES`` (ORM guard + DB trigger); ``holder`` is the ``BudgetHolder`` the card
    cap is keyed by; ``card_auth_ref`` is unique, so one authorisation funds at most one row.
    SHARED without a desk partition — a reported deviation from DESIGN §5.1 "SHARED (RLS by desk
    for desk roles)": DESIGN §4a lets the Assistant read Operator transactions through
    ``Ledger.pnl`` for the owner's consolidated view, which per-desk RLS would refuse; desk code
    filters by ``desk`` itself."""

    __tablename__ = "transaction"
    __scope__ = Scope.SHARED
    __forward_only__ = {"status": list(TRANSACTION_STATES)}
    __table_args__ = (
        ForeignKeyConstraint(["coat_id"], ["coat.id"]),
        CheckConstraint(_in("direction", DIRECTIONS), name="direction"),
        CheckConstraint(_in("status", TRANSACTION_STATES), name="status"),
        Index("ix_transaction_coat_id_occurred_at", "coat_id", "occurred_at"),
        Index("ix_transaction_holder_occurred_at", "holder", "occurred_at"),
    )

    coat_id: Mapped[CoatId] = mapped_column(String(COAT_ID_LEN), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    amount: Mapped[int] = mapped_column(MoneyCol, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    counterpart_ref: Mapped[str] = mapped_column(Text, nullable=False)
    beneficiary_id: Mapped[Ulid | None] = mapped_column(
        String(ULID_LEN), ForeignKey("beneficiary.id"), nullable=True
    )
    experiment_id: Mapped[Ulid | None] = mapped_column(
        String(ULID_LEN), ForeignKey("experiment.id"), nullable=True
    )
    task_id: Mapped[Ulid | None] = mapped_column(
        String(ULID_LEN), ForeignKey("task.id"), nullable=True
    )
    approval_id: Mapped[Ulid | None] = mapped_column(String(ULID_LEN), nullable=True)
    holder: Mapped[str] = mapped_column(String(64), nullable=False)
    card_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    card_auth_ref: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    bank_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class BeneficiaryRow(Base, RecordMixin):
    """SPEC §15 Beneficiary, §10 §13: the registry invoice-redirect fraud is checked against.
    ASSISTANT_ONLY. ``bank_details_ct`` is ``EncryptedBytes`` (Tier 2 ciphertext, AAD
    ``beneficiary.bank_details_ct.<id>``), ``bank_last4`` is what briefs may show and ``bank_fp``
    a keyed hash; mutable only through a call that takes a ``ReleasedAction`` (phase 2)."""

    __tablename__ = "beneficiary"
    __scope__ = Scope.ASSISTANT_ONLY
    __table_args__ = (
        ForeignKeyConstraint(["coat_id"], ["coat.id"]),
        UniqueConstraint("coat_id", "name"),
    )

    coat_id: Mapped[CoatId] = mapped_column(String(COAT_ID_LEN), nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    bank_details_ref: Mapped[str] = mapped_column(Text, nullable=False)
    bank_details_ct: Mapped[bytes] = mapped_column(EncryptedBytes, nullable=False)
    bank_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    bank_fp: Mapped[str] = mapped_column(Text, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    verified_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    change_history: Mapped[list[Any]] = mapped_column(JSONCol, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class DocumentRow(Base, RecordMixin):
    """SPEC §15 Document, §11: the vault index. ASSISTANT_ONLY. A new row per version
    (``supersedes_id``); the blob at ``storage_ref`` is encrypted by ``FieldCipher``;
    ``CHECK (tier <> 2 OR content_text IS NULL)`` keeps Tier 2 documents metadata-only
    (DESIGN §4b). Rows are immutable except ``share_log`` (DESIGN §5.1): every other column —
    ``storage_ref`` and ``sha256`` above all, which the vault's integrity rests on — is
    ``__immutable__`` (trigger + ORM guard); a change is a new version. The ``metadata`` column
    is mapped as ``meta`` (``metadata`` is reserved by the declarative API)."""

    __tablename__ = "document"
    __scope__ = Scope.ASSISTANT_ONLY
    __immutable__ = (
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
    )
    __table_args__ = (
        CheckConstraint("tier <> 2 OR content_text IS NULL", name="tier2_metadata_only"),
        CheckConstraint(_in("tier", DATA_TIERS), name="tier"),
        Index("ix_document_entity_ref_type", "entity_ref", "type"),
    )

    entity_ref: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    allowed_recipients: Mapped[list[Any]] = mapped_column(JSONCol, nullable=False)
    storage_ref: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    share_log: Mapped[list[Any]] = mapped_column(JSONCol, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_id: Mapped[Ulid | None] = mapped_column(
        String(ULID_LEN), ForeignKey("document.id"), nullable=True
    )
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSONCol, nullable=False)
    confirmed_by_event_id: Mapped[Ulid] = mapped_column(String(ULID_LEN), nullable=False)


class VaultSecretRow(Base, RecordMixin):
    """SPEC §10 §11 §13: a Tier 2 value filed by ``VaultStore.put_secret`` (banking details).
    ASSISTANT_ONLY. Mirrors ``SecretRef``: ``uri`` (``vault://<entity>/<path>#<field>``),
    ``last4`` and the keyed ``content_fp``; ``ciphertext`` is ``EncryptedBytes`` (AAD
    ``vault_secret.ciphertext.<id>``). Not in DESIGN §5's column list — the columns are what
    DESIGN §3.13 ``VaultStore.put_secret`` / ``ref`` / ``reveal`` need and nothing else."""

    __tablename__ = "vault_secret"
    __scope__ = Scope.ASSISTANT_ONLY

    entity_ref: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    uri: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    ciphertext: Mapped[bytes] = mapped_column(EncryptedBytes, nullable=False)
    last4: Mapped[str] = mapped_column(String(4), nullable=False)
    content_fp: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class MemoryRecordRow(Base, RecordMixin, DeskMixin, CoatMixin, AuditedMixin):
    """SPEC §15 MemoryRecord, §8: the four stores, partitioned per desk (DESK_ROW; only the two
    desks, never ``governance``) with ``CHECK (store <> 'owner_profile' OR desk = 'assistant')``
    so the owner profile is Assistant-only by construction. ``content`` is ``SafeStr`` text
    (scanned and shape-checked before the write); ``vector_ref`` points into the per-desk
    namespace of the vector index."""

    __tablename__ = "memory_record"
    __scope__ = Scope.DESK_ROW
    __table_args__ = (
        ForeignKeyConstraint(["coat_id"], ["coat.id"]),
        CheckConstraint(_in("desk", REAL_DESKS), name="desk_is_a_desk"),
        CheckConstraint(_in("store", MEMORY_STORES), name="store"),
        CheckConstraint(
            "store <> 'owner_profile' OR desk = 'assistant'", name="owner_profile_assistant_only"
        ),
        Index("ix_memory_record_desk_store_created_at", "desk", "store", "created_at"),
    )

    store: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_refs: Mapped[list[Any]] = mapped_column(JSONCol, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    approved_by_owner: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    vector_ref: Mapped[str | None] = mapped_column(Text, nullable=True)


class ExperimentRow(Base, RecordMixin, DeskMixin):
    """SPEC §15 Experiment, §7: Operator only — OPERATOR_ONLY scope and ``CHECK (desk =
    'operator')``; ``budget`` is fils under the Operator card cap."""

    __tablename__ = "experiment"
    __scope__ = Scope.OPERATOR_ONLY
    __table_args__ = (
        ForeignKeyConstraint(["coat_id"], ["coat.id"]),
        CheckConstraint("desk = 'operator'", name="operator_only"),
        Index("ix_experiment_coat_id_status", "coat_id", "status"),
    )

    coat_id: Mapped[CoatId] = mapped_column(String(COAT_ID_LEN), nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    budget: Mapped[int] = mapped_column(MoneyCol, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    deadline: Mapped[date] = mapped_column(Date, nullable=False)
    metric: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    kill_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    playbook_ref: Mapped[str | None] = mapped_column(Text, nullable=True)


class SkillRow(Base, RecordMixin, DeskMixin):
    """SPEC §15 Skill, §8: versioned inserts (``UNIQUE (name, version)``), partitioned per desk;
    ``steps_ref`` is the markdown path under ``skills/`` in version control. The unique key is
    global across desks by DESIGN §5.1 (a skill name is one routine, whichever desk runs it), so
    an Operator insert that collides with an invisible Assistant skill fails with
    ``IntegrityError`` — an existence oracle over Tier 0 routine names, accepted as such."""

    __tablename__ = "skill"
    __scope__ = Scope.DESK_ROW
    __table_args__ = (UniqueConstraint("name", "version"),)

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    steps_ref: Mapped[str] = mapped_column(Text, nullable=False)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONCol, nullable=False)
    failure_signs: Mapped[list[Any]] = mapped_column(JSONCol, nullable=False)
    dry_run_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class IncidentRow(Base, RecordMixin, DeskMixin, CoatMixin):
    """SPEC §15 Incident, §12 playbook: one row per incident, mutable only to resolve
    (DESIGN §5.1): ``resolved_at`` and ``postmortem_ref`` are set once (``__single_transition__``,
    so the rows are also never deleted), every other column is ``__immutable__``.
    ``frozen_scope`` is checked against ``FreezeScope``; ``details`` is LeakGuard-scrubbed JSON.
    SHARED so the auditor and both desks see it. No foreign key on ``coat_id`` (an incident may
    name a coat the model invented)."""

    __tablename__ = "incident"
    __scope__ = Scope.SHARED
    __single_transition__ = ("resolved_at", "postmortem_ref")
    __immutable__ = (
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
    )
    __table_args__ = (
        CheckConstraint(_in("type", INCIDENT_TYPES), name="type"),
        CheckConstraint(_in("detected_by", DETECTED_BY), name="detected_by"),
        CheckConstraint(_in_or_null("frozen_scope", FREEZE_SCOPES), name="frozen_scope"),
        Index("ix_incident_type_detected_at", "type", "detected_at"),
    )

    type: Mapped[str] = mapped_column(String(32), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    detected_by: Mapped[str] = mapped_column(String(16), nullable=False)
    first_response: Mapped[str] = mapped_column(Text, nullable=False)
    frozen_scope: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    postmortem_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_id: Mapped[Ulid | None] = mapped_column(String(ULID_LEN), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSONCol, nullable=False)


# =========================================================================== infrastructure tables


class InboxEventRow(Base, RecordMixin, DeskMixin, CoatMixin):
    """DESIGN §3.15 ``EventBus``: the durable inbox (SPEC §4). ``body`` is already redacted by
    the ingress redactor and ``passphrase_attempt`` is the ingress-recorded outcome.

    Three walls, on both dialects (THREAT_REVIEW 6.1; DESIGN §4a §4d):

    * ``__desk_partitioned__``: a desk reads, acks, parks and leases its own events only — the
      Operator never sees an owner-thread or owner-mailbox body routed to the Assistant — while
      any desk may insert an event for either desk (a handoff, an approval decision, a read-back
      re-issue) and the governance processes see and write every partition;
    * ``__immutable__``: everything but the ack columns is frozen at INSERT, so neither the body
      nor ``sender`` / ``signature_valid`` / ``passphrase_attempt`` can be rewritten;
    * grants: desks INSERT without :data:`INBOX_AUTH_COLUMNS` (``signature_valid`` takes its
      ``false`` server default, the attempt stays NULL: a desk-published row can never verify as
      the owner), UPDATE :data:`INBOX_ACK_COLUMNS` only and never DELETE; ingress and the
      scheduler INSERT, the scheduler alone DELETEs (retention).

    Dedup is the unique ``(source_kind, provider_msg_id)`` (SPEC §13 replay); it spans desks, so
    a provider id already queued for the other desk surfaces as ``IntegrityError``."""

    __tablename__ = "inbox_event"
    __scope__ = Scope.SHARED
    __desk_partitioned__ = True
    __immutable__ = (
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
    )
    __table_args__ = (
        UniqueConstraint("source_kind", "provider_msg_id"),
        CheckConstraint(_in("source_kind", SOURCE_KINDS), name="source_kind"),
        CheckConstraint(_in("origin", ORIGINS), name="origin"),
        CheckConstraint(
            _in_or_null("passphrase_attempt", PASSPHRASE_OUTCOMES), name="passphrase_attempt"
        ),
        Index(
            "ix_inbox_event_desk_acked_at_priority_received_at",
            "desk",
            "acked_at",
            "priority",
            "received_at",
        ),
        {
            "info": {
                GRANTS_KEY: {
                    **_grants_for(
                        (OPERATOR_ROLE, ASSISTANT_ROLE),
                        insert=INBOX_DESK_INSERT_COLUMNS,
                        update=INBOX_ACK_COLUMNS,
                    ),
                    INGRESS_ROLE: _grant(insert=True),
                    SCHEDULER_ROLE: _grant(insert=True, delete=True),
                }
            }
        },
    )

    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    line_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sender: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONCol, nullable=False)
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    passphrase_attempt: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempt_id: Mapped[Ulid | None] = mapped_column(String(ULID_LEN), nullable=True)
    provider_msg_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    received_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    acked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    parked_until: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    lock_until: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class TimerSlotRow(Base, RecordMixin):
    """DESIGN §3.15 ``Scheduler``: exactly-once timers (SPEC §12 daily rhythm) — append-only
    with ``UNIQUE (timer_name, slot)``, written by the scheduler process only."""

    __tablename__ = "timer_slot"
    __scope__ = Scope.GOVERNANCE_ONLY
    __append_only__ = True
    __table_args__ = (UniqueConstraint("timer_name", "slot"),)

    timer_name: Mapped[str] = mapped_column(String(64), nullable=False)
    slot: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class ReleaseRow(Base, RecordMixin, DeskMixin):
    """DESIGN §4c ``ReleaseToken``: the burnt-nonce table (SPEC §6). ``call_id`` and ``nonce`` are
    unique, ``burnt_at`` is set exactly once by ``ToolExecutor.execute`` in the transaction that
    executes the action; ``minted_by`` is the gate (A/N) or the approvals queue (K). "Insert +
    ONE burn" (DESIGN §5.2) end to end: every other column is ``__immutable__`` (the nonce, the
    call, the tier and the approval a token was minted for cannot be re-bound), the rows are
    never deleted or replaced (a burnt nonce cannot be re-armed by re-inserting it), and on
    Postgres every runtime role may INSERT and UPDATE ``burnt_at`` only."""

    __tablename__ = "release"
    __scope__ = Scope.SHARED
    __single_transition__ = ("burnt_at",)
    __immutable__ = (
        "id",
        "created_at",
        "desk",
        "call_id",
        "nonce",
        "tier",
        "approval_id",
        "minted_at",
        "minted_by",
    )
    __table_args__ = (
        CheckConstraint(_in("tier", ACTION_TIERS), name="tier"),
        CheckConstraint(_in("minted_by", MINTED_BY), name="minted_by"),
        {"info": {GRANTS_KEY: _grants_for(RUNTIME_ROLES, insert=True, update=("burnt_at",))}},
    )

    call_id: Mapped[Ulid] = mapped_column(String(ULID_LEN), nullable=False, unique=True)
    nonce: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    tier: Mapped[str] = mapped_column(String(1), nullable=False)
    approval_id: Mapped[Ulid | None] = mapped_column(String(ULID_LEN), nullable=True)
    minted_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    minted_by: Mapped[str] = mapped_column(String(16), nullable=False)
    burnt_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class FreezeStateRow(Base, RecordMixin):
    """DESIGN §3.17 ``KillSwitch`` / ``Watchdog`` (SPEC §12): one row per freeze; ``released_at``
    and ``released_by_approval_id`` are set once (release needs the passphrase and, for the
    kill switch, the second channel) and the rows are never deleted. The desks may *engage* a
    freeze (INSERT: the watchdog runs in the desk loop) but never release one — on Postgres
    they have no UPDATE and no DELETE; release is the governance kill switch — and the auditor
    reads."""

    __tablename__ = "freeze_state"
    __scope__ = Scope.SHARED
    __single_transition__ = ("released_at", "released_by_approval_id")
    __table_args__ = (
        CheckConstraint(_in("scope", FREEZE_SCOPES), name="scope"),
        Index(
            "ix_freeze_state_released_at",
            "released_at",
            postgresql_where=sql_text("released_at IS NULL"),
        ),
        {"info": {GRANTS_KEY: _grants_for((OPERATOR_ROLE, ASSISTANT_ROLE), insert=True)}},
    )

    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    target: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(String(32), nullable=False)
    engaged_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    released_by_approval_id: Mapped[Ulid | None] = mapped_column(String(ULID_LEN), nullable=True)
    event_id: Mapped[Ulid | None] = mapped_column(String(ULID_LEN), nullable=True)


class AuditChainHeadRow(Base, TimestampMixin):
    """DESIGN §3.11 ``AuditLog.append``: the one row (``singleton = 1``) every appender locks
    (``BEGIN IMMEDIATE`` on SQLite, ``SELECT … FOR UPDATE`` on Postgres) so two writers never
    fork the chain; carries the last ``entry_hash`` and ``seq``."""

    __tablename__ = "audit_chain_head"
    __scope__ = Scope.SHARED
    __table_args__ = (CheckConstraint("singleton = 1", name="singleton"),)

    singleton: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    last_hash: Mapped[str] = mapped_column(Text, nullable=False)
    last_seq: Mapped[int] = mapped_column(Integer, nullable=False)


class PassphraseAttemptRow(Base, RecordMixin):
    """DESIGN §3.12 ``PassphraseVerifier`` (SPEC §6 §13): one append-only row per attempt with
    its outcome — no body, no candidate, ever; written by ingress before the event is published
    (DESIGN §5.2 "SHARED (ingress INSERT)": the desk roles and the scheduler may not INSERT)."""

    __tablename__ = "passphrase_attempt"
    __scope__ = Scope.SHARED
    __append_only__ = True
    __table_args__ = (
        CheckConstraint(_in("outcome", PASSPHRASE_OUTCOMES), name="outcome"),
        {"info": {GRANTS_KEY: _grants_for((OPERATOR_ROLE, ASSISTANT_ROLE, SCHEDULER_ROLE))}},
    )

    event_id: Mapped[Ulid | None] = mapped_column(String(ULID_LEN), nullable=True)
    sender: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)


class FoundInstructionRow(Base, RecordMixin, DeskMixin, CoatMixin):
    """DESIGN §4e (SPEC §12 §13): an instruction the scanner found inside observed content,
    written at Authenticate; append-only — reporting it is a ``pending_owner_message``, never
    an update. ``quote`` is redacted text. No foreign key on ``coat_id``."""

    __tablename__ = "found_instruction"
    __scope__ = Scope.SHARED
    __append_only__ = True

    event_id: Mapped[Ulid] = mapped_column(String(ULID_LEN), nullable=False, index=True)
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[str] = mapped_column(Text, nullable=False)
    mentions_money: Mapped[bool] = mapped_column(Boolean, nullable=False)
    pattern: Mapped[str] = mapped_column(String(64), nullable=False)


class HandoffRow(Base, RecordMixin):
    """DESIGN §3.16 ``HandoffQueue`` (SPEC §5 one-way gate): the Assistant inserts
    (``CHECK (pushed_by = 'assistant')`` and, by grant, only the Assistant role may INSERT), the
    Operator takes exactly once (``taken_at`` is a single transition and the only column the
    Operator role may UPDATE); nobody else writes and nobody deletes; the body
    (``handoff_json``, the validated ``TaskHandoff``), ``source_event_id`` and ``pushed_by`` are
    ``__immutable__``. Enforced by ``DeskWallGuard`` on SQLite and by grants on Postgres."""

    __tablename__ = "handoff"
    __scope__ = Scope.SHARED
    __single_transition__ = ("taken_at",)
    __immutable__ = (
        "id",
        "created_at",
        "coat_id",
        "handoff_json",
        "source_event_id",
        "pushed_by",
    )
    __table_args__ = (
        ForeignKeyConstraint(["coat_id"], ["coat.id"]),
        CheckConstraint("pushed_by = 'assistant'", name="pushed_by"),
        {
            "info": {
                GRANTS_KEY: {
                    ASSISTANT_ROLE: _grant(insert=True),
                    OPERATOR_ROLE: _grant(update=("taken_at",)),
                    INGRESS_ROLE: _grant(),
                    SCHEDULER_ROLE: _grant(),
                }
            }
        },
    )

    coat_id: Mapped[CoatId] = mapped_column(String(COAT_ID_LEN), nullable=False, index=True)
    handoff_json: Mapped[dict[str, Any]] = mapped_column(JSONCol, nullable=False)
    source_event_id: Mapped[Ulid] = mapped_column(String(ULID_LEN), nullable=False)
    pushed_by: Mapped[str] = mapped_column(String(16), nullable=False)
    taken_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class ReadbackPendingRow(Base, RecordMixin, DeskMixin):
    """DESIGN §3.12 ``ReadBackLedger`` (SPEC §9): a voice command waiting for its text read-back
    confirmation; partitioned per desk, confirmed exactly once."""

    __tablename__ = "readback_pending"
    __scope__ = Scope.DESK_ROW
    __single_transition__ = ("confirmed_at", "confirmed_by_event_id")

    event_id: Mapped[Ulid] = mapped_column(String(ULID_LEN), nullable=False)
    proposal_json: Mapped[dict[str, Any]] = mapped_column(JSONCol, nullable=False)
    understood: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    confirmed_by_event_id: Mapped[Ulid | None] = mapped_column(String(ULID_LEN), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class CardRow(Base, RecordMixin, DeskMixin):
    """DESIGN §3.14 ``CardService`` (SPEC §10): one capped card per ``BudgetHolder``; the cap is
    enforced by the issuer, this row mirrors it for briefs and the watchdog. GOVERNANCE_ONLY
    (DESIGN §5.2 "mutable (governance)"): the owner CLI registers cards and the kill switch
    freezes and thaws them; a desk reads ``card_for`` / ``remaining`` and can neither unfreeze
    its mirror nor raise the mirrored cap (a reported narrowing of §5.2's SHARED scope: the
    auditor, which has no §3.11 check over cards, loses its read)."""

    __tablename__ = "card"
    __scope__ = Scope.GOVERNANCE_ONLY

    holder: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    card_ref: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    monthly_cap: Mapped[int] = mapped_column(MoneyCol, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    frozen: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class CardAuthorizationRow(Base, RecordMixin, AuditedMixin):
    """DESIGN §4f (SPEC §10): every issuer answer, approved or declined, append-only;
    ``auth_ref`` is unique where not null and is what ``transaction.card_auth_ref`` points at."""

    __tablename__ = "card_authorization"
    __scope__ = Scope.SHARED
    __append_only__ = True
    __table_args__ = (
        UniqueConstraint("auth_ref"),
        Index("ix_card_authorization_card_ref_created_at", "card_ref", "created_at"),
    )

    card_ref: Mapped[str] = mapped_column(Text, nullable=False)
    holder: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[int] = mapped_column(MoneyCol, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    merchant: Mapped[str] = mapped_column(Text, nullable=False)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    decline_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    call_id: Mapped[Ulid] = mapped_column(String(ULID_LEN), nullable=False)


class SecondChannelChallengeRow(Base, RecordMixin):
    """DESIGN §3.12 ``SecondChannelConfirmations`` (SPEC §6 §12): a challenge bound to one
    ``ref`` (approval id, ``kill_switch_release``, ``constitution:<hash>``,
    ``deputy_activation``); only the keyed ``token_hash`` is stored; consumed exactly once."""

    __tablename__ = "second_channel_challenge"
    __scope__ = Scope.SHARED
    __single_transition__ = ("consumed_at", "consumed_by", "approved")

    ref: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    issued_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    consumed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class PendingOwnerMessageRow(Base, RecordMixin, DeskMixin, AuditedMixin):
    """DESIGN §3.17 ``OwnerChannel`` (SPEC §12 quiet hours, initiative budget): a message for the
    owner's thread, parked until ``parked_until`` when it is not an emergency and sent exactly
    once; rows are never deleted. ``text`` is ``SafeStr`` text rendered by code for money items.

    ``__desk_partitioned__`` (SPEC §5: the Operator "sees Tier 0 facts only"; DESIGN §4a; fixer
    round 2): what the Assistant drafts for the owner — a brief with his calendar, the read-back
    of a personal action, an instruction quoted from his mailbox, a reply on his thread — is
    Tier 1 text derived from the very sources the Operator may never read, so a desk token
    reads and transitions (``sent_at`` / ``provider_msg_id``) its own partition only, exactly
    like ``inbox_event``, while governance and the auditor see every row. Consequences the
    governance module carries: each desk's ``OwnerChannel.flush_due`` sends the rows that desk
    authored, so the ``notify_sweep`` timer reaches both desks (the per-desk
    ``readback_pending`` expiry it also drives needs that anyway), and the owner-wide 5/day
    budget of SPEC §12 is counted across desks from ``audit_event`` — SHARED, append-only,
    carrying ``desk`` and ``action`` but no message text — while ``initiative_used`` under a
    desk token counts that desk's rows. A reported narrowing of DESIGN §5's bare "SHARED": the
    scope itself stays SHARED (grants unchanged), only the desk tokens' rows are partitioned."""

    __tablename__ = "pending_owner_message"
    __scope__ = Scope.SHARED
    __desk_partitioned__ = True
    __single_transition__ = ("sent_at", "provider_msg_id")
    __table_args__ = (
        Index(
            "ix_pending_owner_message_sent_at",
            "sent_at",
            postgresql_where=sql_text("sent_at IS NULL"),
        ),
    )

    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    emergency: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    parked_until: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    provider_msg_id: Mapped[str | None] = mapped_column(Text, nullable=True)


class CategoryStateRow(Base, RecordMixin, DeskMixin):
    """DESIGN §3.16 ``CategoryState`` (SPEC §6 graduated autonomy): the current tier per
    ``(coat, category, desk)`` with the acceptance counters promotion reads; ``reduce_to_k``
    is the incident demotion."""

    __tablename__ = "category_state"
    __scope__ = Scope.DESK_ROW
    __table_args__ = (
        ForeignKeyConstraint(["coat_id"], ["coat.id"]),
        UniqueConstraint("coat_id", "category", "desk"),
        CheckConstraint(_in("tier", ACTION_TIERS), name="tier"),
    )

    coat_id: Mapped[CoatId] = mapped_column(String(COAT_ID_LEN), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[str] = mapped_column(String(1), nullable=False)
    started_at: Mapped[date] = mapped_column(Date, nullable=False)
    promoted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    demoted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unedited: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reduce_to_k: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ModelTraceRow(Base, RecordMixin, DeskMixin):
    """DESIGN §3.11 ``Replayer`` (SPEC §12 §14): the model's tool calls and text per request,
    LeakGuard-scrubbed, never observed text; append-only, partitioned per desk and purged by
    retention after ``expires_at`` (30 days)."""

    __tablename__ = "model_trace"
    __scope__ = Scope.DESK_ROW
    __append_only__ = True

    event_id: Mapped[Ulid] = mapped_column(String(ULID_LEN), nullable=False, index=True)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSONCol, nullable=False)
    vendor: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class AuditorReportRow(Base, RecordMixin):
    """DESIGN §2.5 §3.11 ``AuditorRunner.run_day`` (SPEC §12): the auditor's nightly report —
    the only table its role may insert into, append-only, in the Postgres schema ``auditor``
    (desk roles have no grant on it; the brain ORM maps it for the harness only)."""

    __tablename__ = "auditor_report"
    __scope__ = Scope.AUDITOR_WRITE
    __append_only__ = True
    __table_args__ = {"schema": AUDITOR_SCHEMA}

    day: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    findings: Mapped[list[Any]] = mapped_column(JSONCol, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    vendor: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


# =========================================================================== module-level API

ENTITY_TABLES: frozenset[str] = frozenset(
    {
        "owner",
        "coat",
        "contact",
        "conversation",
        "message",
        "task",
        "approval",
        "decision_journal",
        "audit_event",
        "transaction",
        "beneficiary",
        "document",
        "memory_record",
        "experiment",
        "skill",
        "incident",
    }
)
"""The sixteen SPEC §15 entities, by table name."""

INFRA_TABLES: frozenset[str] = frozenset(
    {
        "dnc_entry",
        "vault_secret",
        "inbox_event",
        "timer_slot",
        "release",
        "freeze_state",
        "audit_chain_head",
        "passphrase_attempt",
        "found_instruction",
        "handoff",
        "readback_pending",
        "card",
        "card_authorization",
        "second_channel_challenge",
        "pending_owner_message",
        "category_state",
        "model_trace",
        "auditor_report",
    }
)
"""DESIGN §5.2's infrastructure tables (plus ``vault_secret`` from §3.8), by table name."""

APPEND_ONLY_TABLES: frozenset[str] = frozenset(
    {
        "audit_event",
        "decision_journal",
        "passphrase_attempt",
        "found_instruction",
        "card_authorization",
        "timer_slot",
        "model_trace",
        "auditor_report",
    }
)
"""DESIGN §3.8: tables whose rows never change (trigger + REVOKE + ORM guard)."""

DESK_PARTITIONED_TABLES: frozenset[str] = frozenset({"inbox_event", "pending_owner_message"})
"""SHARED tables a desk token reads, updates and deletes in its own ``desk`` partition only
(``__desk_partitioned__``, DESIGN §4a): forced row-level security on Postgres, loader criteria
on SQLite; governance and the auditor see every partition."""


_NO_SCHEMA: Any = None
"""``Table.to_metadata(schema=None)`` puts the copy in the target's default schema (documented);
the stubs type the parameter as ``str | RETAIN_SCHEMA`` only, hence the ``Any``."""


def _copy_tables(select: Callable[[Table], bool], *, drop_schema: bool = False) -> MetaData:
    """A fresh ``MetaData`` holding copies of the ``Base.metadata`` tables ``select`` picks,
    with the wall markers, the mapper link and the grant hints carried over (``Table.to_metadata``
    copies columns, constraints and indexes but not ``info``). ``drop_schema`` puts every copy
    in the default schema."""
    target = MetaData(naming_convention=NAMING_CONVENTION)
    for table in Base.metadata.sorted_tables:
        if not select(table):
            continue
        copy = (
            table.to_metadata(target, schema=_NO_SCHEMA)
            if drop_schema
            else table.to_metadata(target)
        )
        for key in (TABLE_INFO_KEY, MAPPER_INFO_KEY, GRANTS_KEY):
            if key in table.info:
                copy.info[key] = table.info[key]
        _align_index_names(table, copy)
    return target


def _align_index_names(original: Table, copy: Table) -> None:
    """Give the copy's indexes the original's names. ``Column(index=True)`` indexes are
    regenerated by the copy with a fresh conventional name, and ``ix_%(column_0_label)s``
    embeds the schema (``ix_auditor_auditor_report_day``), so a schema-free copy would
    otherwise rename them — and the database built by ``create_schema`` or the migration
    carries the original names."""
    names = {tuple(c.name for c in index.columns): index.name for index in original.indexes}
    for index in copy.indexes:
        name = names.get(tuple(c.name for c in index.columns))
        if name is not None and index.name != name:
            index.name = name


def metadata_for_dialect(dialect: str) -> MetaData:
    """``Base.metadata`` as ``dialect`` stores it. SQLite has no schemas, so there every table —
    ``auditor_report`` included — lives in the one database file, which is what
    ``install_sqlite_schema_translation`` arranges at runtime; a schema-free copy is therefore
    the right thing to compare a SQLite database against (alembic autogenerate does not apply
    the translate map). Any other dialect gets ``Base.metadata`` itself."""
    if dialect != "sqlite":
        return Base.metadata
    return _copy_tables(lambda table: True, drop_schema=True)


brain_metadata: MetaData = _copy_tables(lambda table: table.schema is None)
"""DESIGN §3.8: every brain table (Postgres schema ``public``); a split copy of ``Base.metadata``
for schema generation and migration review — the ORM maps ``Base.metadata``."""

auditor_metadata: MetaData = _copy_tables(lambda table: table.schema == AUDITOR_SCHEMA)
"""DESIGN §3.8: the tables of the Postgres schema ``auditor`` (``auditor_report``)."""


def _holds_text(column: Column[Any]) -> bool:
    """String-ish or JSON columns: everything a canary could hide in as text."""
    return isinstance(column.type, String | JSON)


def all_text_columns() -> list[tuple[Table, Column[Any]]]:
    """Every column of every table that stores text (``TEXT``/``VARCHAR``, the non-native enums
    and JSON), for ``Harness.db_dump_text`` (DESIGN §3.21): the leak canary grep covers the
    whole database. Encrypted blobs, fingerprints, numbers and timestamps are left out."""
    return [
        (table, column)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if _holds_text(column)
    ]


__all__ = [
    "APPEND_ONLY_TABLES",
    "APPROVAL_DECISION_COLUMNS",
    "AUDITOR_SCHEMA",
    "DESK_PARTITIONED_TABLES",
    "ENTITY_TABLES",
    "INBOX_ACK_COLUMNS",
    "INBOX_AUTH_COLUMNS",
    "INBOX_DESK_INSERT_COLUMNS",
    "INFRA_TABLES",
    "RUNTIME_ROLES",
    "ApprovalRow",
    "AuditChainHeadRow",
    "AuditEventRow",
    "AuditorReportRow",
    "BeneficiaryRow",
    "CardAuthorizationRow",
    "CardRow",
    "CategoryStateRow",
    "CoatRow",
    "ContactRow",
    "ConversationRow",
    "DecisionJournalRow",
    "DncEntryRow",
    "DocumentRow",
    "ExperimentRow",
    "FoundInstructionRow",
    "FreezeStateRow",
    "HandoffRow",
    "InboxEventRow",
    "IncidentRow",
    "MemoryRecordRow",
    "MessageRow",
    "ModelTraceRow",
    "OwnerRow",
    "PassphraseAttemptRow",
    "PendingOwnerMessageRow",
    "ReadbackPendingRow",
    "ReleaseRow",
    "SecondChannelChallengeRow",
    "SkillRow",
    "TaskRow",
    "TimerSlotRow",
    "TransactionRow",
    "VaultSecretRow",
    "all_text_columns",
    "auditor_metadata",
    "brain_metadata",
    "metadata_for_dialect",
]
