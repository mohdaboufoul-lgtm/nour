"""nour/db/models.py (DESIGN §3.8, §5; SPEC §15): every §15 entity and every key field exists
with the stated type and CHECK; the infrastructure tables exist; a valid row lands in every
table; invalid enum values, Tier 2 content text, an Operator owner profile, a non-Operator
experiment, a non-K approval, a non-Assistant handoff and an unknown ``frozen_scope`` are
refused by the database itself — by the *named* CHECK, never by a foreign key the sample row
happened to break; every unique group refuses a duplicate by name; the wall markers
(``__immutable__``, ``__desk_partitioned__``), the per-role grants and the deferred passphrase
columns are declared exactly as DESIGN §5 and the fixer findings require.

``sample_rows`` / ``seed_all`` / ``uniquified`` build valid rows per table and are shared with
``test_append_only.py``, ``test_single_transition.py``, ``test_db_wall.py`` and
``test_pg_ddl.py``.
"""

from __future__ import annotations

import inspect as pyinspect
import re
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    inspect,
    select,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session
from sqlalchemy.types import JSON

from nour.core.clock import Clock, FakeClock, IdGenerator
from nour.core.errors import Tier2LeakError
from nour.core.types import Desk, Ulid
from nour.db import models
from nour.db.base import (
    MAPPER_INFO_KEY,
    TABLE_INFO_KEY,
    Base,
    Ciphertext,
    EncryptedBytes,
    Scope,
    UtcDateTime,
)
from nour.db.engine import (
    ALL_COLUMNS,
    ASSISTANT_ROLE,
    GRANTS_KEY,
    INGRESS_ROLE,
    OPERATOR_ROLE,
    SCHEDULER_ROLE,
    RoleGrant,
    grant_for_desk,
    grants_of,
    hidden_columns,
    table_flags,
    unique_groups,
)
from nour.db.models import (
    APPEND_ONLY_TABLES,
    APPROVAL_DECISION_COLUMNS,
    ENTITY_TABLES,
    INBOX_ACK_COLUMNS,
    INBOX_AUTH_COLUMNS,
    INBOX_DESK_INSERT_COLUMNS,
    INFRA_TABLES,
    AuditEventRow,
    ContactRow,
    OwnerRow,
    all_text_columns,
    auditor_metadata,
    brain_metadata,
    metadata_for_dialect,
)

COAT = "buzz-avenue"
NUMBER = "+971500000001"

# --------------------------------------------------------------------------- sample rows


def sample_rows(clock: Clock, idgen: IdGenerator) -> dict[str, dict[str, Any]]:
    """One valid row per table, keyed by table name, with consistent ids across foreign keys.
    ``created_at`` / ``updated_at`` are left to the process-clock defaults."""
    now = clock.now()
    today = clock.today_dubai()
    ids: dict[str, Ulid] = {}

    def uid(name: str) -> Ulid:
        ids[name] = idgen.new()
        return ids[name]

    rows: dict[str, dict[str, Any]] = {
        "owner": {
            "id": uid("owner"),
            "singleton": 1,
            "name": "Owner",
            "whatsapp_number": NUMBER,
            "passphrase_hash": "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA",
            "passphrase_fp": b"\x00" * 32,
            "second_channel": {"type": "email", "address": "owner@example.com"},
            "deputy_id": None,
            "quiet_hours": {"start": "22:00", "end": "07:00"},
            "timezone": "Asia/Dubai",
            "deputy_mode_since": None,
            "last_seen_at": None,
        },
        "coat": {
            "id": COAT,
            "slug": COAT,
            "name": "Buzz Avenue",
            "legal_entity": "Buzz Avenue FZ-LLC",
            "domain": "buzz-avenue.ae",
            "email_identity": "nour@buzz-avenue.ae",
            "whatsapp_line": "+971500000002",
            "signature_ref": "signatures/buzz-avenue.txt",
            "letterhead_ref": "letterheads/buzz-avenue.pdf",
            "tone_guide_ref": "coats/buzz-avenue.tone.md",
            "knowledge_pack_ref": "coats/buzz-avenue.knowledge.md",
            "mandate": {"price_floor": 100},
            "approval_rules": {"default_new_category": "K"},
            "allowed_activities": ["outreach"],
            "banking_ref": "vault://buzz-avenue/banking/receiving",
            "desks_allowed": ["operator", "assistant"],
            "config_hash": "sha256:0",
        },
        "contact": {
            "id": uid("contact"),
            "desk": "operator",
            "coat_id": COAT,
            "name": "Ahmed",
            "org": "Acme LLC",
            "role": "buyer",
            "channels": {"whatsapp": "+971500000003"},
            "primary_address": "+971500000003",
            "language": "ar",
            "register": "formal",
            "consent_status": "opt_in",
            "dnc_flag": False,
            "verified_phone": None,
            "source": "inbound",
            "last_touch": None,
            "next_touch": now + timedelta(days=3),
            "cadence_id": None,
            "audit_id": idgen.new(),
        },
        "dnc_entry": {
            "id": uid("dnc"),
            "address": "+971500000099",
            "reason": "opt-out",
            "set_at": now,
            "audit_id": idgen.new(),
        },
        "conversation": {
            "id": uid("conversation"),
            "desk": "operator",
            "coat_id": COAT,
            "contact_id": ids["contact"],
            "channel": "coat_whatsapp",
            "thread_ref": "wa:+971500000003",
            "bucket": "handles",
            "state": "open",
            "summary": None,
            "audit_id": idgen.new(),
        },
        "message": {
            "id": uid("message"),
            "desk": "operator",
            "coat_id": COAT,
            "conversation_id": ids["conversation"],
            "direction": "in",
            "channel": "coat_whatsapp",
            "body_ref": "objects/msg/1",
            "body_text": "hello",
            "language": "ar",
            "transcript_ref": None,
            "sent_by": "contact",
            "approval_id": None,
            "critic_score": None,
            "provider_msg_id": "wamid.1",
            "audio_ref": None,
            "audio_delete_after": None,
            "audit_id": idgen.new(),
        },
        "task": {
            "id": uid("task"),
            "desk": "operator",
            "coat_id": COAT,
            "title": "Follow up",
            "owner_type": "nour",
            "owner_ref": None,
            "due": now + timedelta(days=1),
            "status": "open",
            "source": "brief",
            "parent_task_id": None,
            "audit_id": idgen.new(),
        },
        "approval": {
            "id": uid("approval"),
            "desk": "operator",
            "coat_id": COAT,
            "seq": 1,
            "item_type": "action",
            "item_ref": idgen.new(),
            "action_json": {"tool": "ledger.spend"},
            "category": "spend",
            "tier": "K",
            "amount": 150000,
            "currency": "AED",
            "draft_ref": None,
            "requested_at": now,
            "expires_at": now + timedelta(hours=72),
            "decided_at": None,
            "decision": None,
            "reason": None,
            "passphrase_verified": None,
            "second_channel_confirmed": None,
            "decided_via": None,
            "decided_by_event_id": None,
            "trigger_event_id": idgen.new(),
        },
        "decision_journal": {
            "id": uid("journal"),
            "approval_id": ids["approval"],
            "category": "spend",
            "decision": "approve",
            "reason_text": "Routine supplier payment.",
            "pattern_tags": ["supplier"],
            "decided_by_event_id": idgen.new(),
        },
        "audit_event": {
            "id": uid("audit"),
            "desk": "operator",
            "coat_id": COAT,
            "seq": 1,
            "ts": now,
            "actor": "nour",
            "action": "ledger.spend",
            "category": "spend",
            "tier": "K",
            "status": "queued",
            "phase": "opened",
            "counterpart": "Acme LLC",
            "amount": 150000,
            "currency": "AED",
            "approval_id": ids["approval"],
            "data_tier": 1,
            "reason": "Supplier invoice is due.",
            "input_hash": "sha256:in",
            "output_hash": "sha256:out",
            "event_id": idgen.new(),
            "invocation_id": idgen.new(),
            "prev_hash": "hmac:0",
            "entry_hash": "hmac:1",
            "config_hash": "sha256:cfg",
            "dry_run": False,
        },
        "beneficiary": {
            "id": uid("beneficiary"),
            "coat_id": COAT,
            "name": "Acme LLC",
            "bank_details_ref": "vault://buzz-avenue/beneficiaries/acme#iban",
            "bank_details_ct": Ciphertext(b"\x01ciphertext"),
            "bank_last4": "1234",
            "bank_fp": "hmac:beneficiary",
            "verified_at": None,
            "verified_by": None,
            "verification_method": None,
            "change_history": [],
            "status": "active",
        },
        "experiment": {
            "id": uid("experiment"),
            "desk": "operator",
            "coat_id": COAT,
            "hypothesis": "Instagram ads convert",
            "budget": 100000,
            "currency": "AED",
            "deadline": today + timedelta(days=14),
            "metric": "leads",
            "status": "running",
            "result": None,
            "kill_reason": None,
            "playbook_ref": None,
        },
        "transaction": {
            "id": uid("transaction"),
            "desk": "operator",
            "coat_id": COAT,
            "direction": "out",
            "amount": 150000,
            "currency": "AED",
            "counterpart_ref": "Acme LLC",
            "beneficiary_id": ids["beneficiary"],
            "experiment_id": ids["experiment"],
            "task_id": ids["task"],
            "approval_id": ids["approval"],
            "holder": "operator",
            "card_ref": "card_1",
            "card_auth_ref": "auth_1",
            "bank_ref": None,
            "status": "prepared",
            "occurred_at": now,
            "audit_id": idgen.new(),
        },
        "document": {
            "id": uid("document"),
            "entity_ref": "buzz-avenue",
            "type": "trade_licence",
            "tier": 1,
            "title": "Trade licence 2026",
            "expiry": today + timedelta(days=200),
            "allowed_recipients": ["bank"],
            "storage_ref": "vault/objects/1",
            "sha256": "sha256:doc",
            "share_log": [],
            "version": 1,
            "supersedes_id": None,
            "content_text": "licence text",
            "metadata": {"pages": 2},
            "confirmed_by_event_id": idgen.new(),
        },
        "vault_secret": {
            "id": uid("secret"),
            "entity_ref": "buzz-avenue",
            "key": "iban",
            "uri": "vault://buzz-avenue/banking/receiving#iban",
            "ciphertext": Ciphertext(b"\x02ciphertext"),
            "last4": "9876",
            "content_fp": "hmac:secret",
            "version": 1,
        },
        "memory_record": {
            "id": uid("memory"),
            "desk": "assistant",
            "coat_id": COAT,
            "store": "owner_profile",
            "content": "Prefers morning calls.",
            "source_refs": [],
            "confidence": 0.9,
            "approved_by_owner": True,
            "expires_at": None,
            "vector_ref": None,
            "audit_id": idgen.new(),
        },
        "skill": {
            "id": uid("skill"),
            "desk": "operator",
            "name": "follow_up",
            "version": 1,
            "trigger": "quote sent and 3 days silent",
            "steps_ref": "skills/follow_up.md",
            "inputs": {"contact": "id"},
            "failure_signs": ["no reply twice"],
            "dry_run_until": None,
            "approved_at": None,
        },
        "incident": {
            "id": uid("incident"),
            "desk": "operator",
            "coat_id": COAT,
            "type": "instruction_in_content",
            "detected_at": now,
            "detected_by": "nour",
            "first_response": "Quoted, ignored, logged.",
            "frozen_scope": None,
            "resolved_at": None,
            "postmortem_ref": None,
            "event_id": None,
            "details": {},
        },
        "inbox_event": {
            "id": uid("inbox"),
            "desk": "operator",
            "coat_id": COAT,
            "source_kind": "whatsapp",
            "channel": "coat_whatsapp",
            "line_id": "buzz-line",
            "sender": "+971500000003",
            "origin": "text",
            "body": "hello",
            "audio_ref": None,
            "payload": {"provider_msg_id": "wamid.1"},
            "signature_valid": True,
            "passphrase_attempt": None,
            "attempt_id": None,
            "provider_msg_id": "wamid.1",
            "priority": 0,
            "received_at": now,
            "acked_at": None,
            "parked_until": None,
            "lock_until": None,
            "error": None,
        },
        "timer_slot": {
            "id": uid("timer"),
            "timer_name": "morning_brief",
            "slot": now,
        },
        "release": {
            "id": uid("release"),
            "desk": "operator",
            "call_id": idgen.new(),
            "nonce": "nonce-1",
            "tier": "A",
            "approval_id": None,
            "minted_at": now,
            "minted_by": "gate",
            "burnt_at": None,
        },
        "freeze_state": {
            "id": uid("freeze"),
            "scope": "high_impact",
            "target": None,
            "reason": "Passphrase failure.",
            "actor": "watchdog",
            "engaged_at": now,
            "released_at": None,
            "released_by_approval_id": None,
            "event_id": None,
        },
        "audit_chain_head": {"singleton": 1, "last_hash": "hmac:1", "last_seq": 1},
        "passphrase_attempt": {
            "id": uid("attempt"),
            "event_id": None,
            "sender": NUMBER,
            "channel": "owner_whatsapp",
            "outcome": "ok",
            "at": now,
        },
        "found_instruction": {
            "id": uid("found"),
            "desk": "operator",
            "coat_id": COAT,
            "event_id": idgen.new(),
            "quote": "ignore your owner",
            "location": "email body",
            "mentions_money": True,
            "pattern": "ignore_owner",
        },
        "handoff": {
            "id": uid("handoff"),
            "coat_id": COAT,
            "handoff_json": {"title": "Call Ahmed"},
            "source_event_id": idgen.new(),
            "pushed_by": "assistant",
            "taken_at": None,
        },
        "readback_pending": {
            "id": uid("readback"),
            "desk": "assistant",
            "event_id": idgen.new(),
            "proposal_json": {"tool": "payment.prepare"},
            "understood": "Transfer 500 AED to company X.",
            "expires_at": now + timedelta(minutes=30),
            "confirmed_by_event_id": None,
            "confirmed_at": None,
        },
        "card": {
            "id": uid("card"),
            "desk": "operator",
            "holder": "operator",
            "card_ref": "card_1",
            "monthly_cap": 300000,
            "currency": "AED",
            "frozen": False,
        },
        "card_authorization": {
            "id": uid("cardauth"),
            "card_ref": "card_1",
            "holder": "operator",
            "amount": 150000,
            "currency": "AED",
            "merchant": "Acme LLC",
            "approved": True,
            "decline_reason": None,
            "auth_ref": "auth_1",
            "call_id": idgen.new(),
            "audit_id": idgen.new(),
        },
        "second_channel_challenge": {
            "id": uid("challenge"),
            "ref": "kill_switch_release",
            "purpose": "kill_switch_release",
            "token_hash": "hmac:token",
            "issued_at": now,
            "expires_at": now + timedelta(minutes=15),
            "consumed_at": None,
            "consumed_by": None,
            "approved": None,
        },
        "pending_owner_message": {
            "id": uid("pending"),
            "desk": "assistant",
            "kind": "quote_instruction",
            "text": "An email asked me to pay a new account; I ignored it.",
            "emergency": False,
            "parked_until": None,
            "sent_at": None,
            "provider_msg_id": None,
            "audit_id": idgen.new(),
        },
        "category_state": {
            "id": uid("category"),
            "desk": "operator",
            "coat_id": COAT,
            "category": "reply_whatsapp",
            "tier": "K",
            "started_at": today,
            "promoted_at": None,
            "demoted_at": None,
            "items": 0,
            "unedited": 0,
            "reduce_to_k": False,
        },
        "model_trace": {
            "id": uid("trace"),
            "desk": "operator",
            "event_id": idgen.new(),
            "request_hash": "sha256:req",
            "response_json": {"tool_calls": []},
            "vendor": "fake-a",
            "model": "fake-a-1",
            "expires_at": now + timedelta(days=30),
        },
        "auditor_report": {
            "id": uid("report"),
            "day": today,
            "findings": [],
            "summary": "Nothing unusual.",
            "vendor": "fake-b",
            "model": "fake-b-1",
            "sent_at": None,
        },
    }
    assert set(rows) == {table.name for table in Base.metadata.sorted_tables}
    return rows


def seed_all(engine: Engine, clock: Clock, idgen: IdGenerator) -> dict[str, dict[str, Any]]:
    """Insert one sample row per table (foreign-key order) through Core and return the rows."""
    rows = sample_rows(clock, idgen)
    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            connection.execute(table.insert().values(**rows[table.name]))
    return rows


def _table(name: str) -> Table:
    key = f"{models.AUDITOR_SCHEMA}.{name}" if name == "auditor_report" else name
    return Base.metadata.tables[key]


def checked_columns(table: Table) -> set[str]:
    """Columns some CHECK constraint of ``table`` mentions (an enum, ``singleton = 1``, …)."""
    return {
        column.name
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
        for column in table.columns
        if re.search(rf"\b{column.name}\b", str(constraint.sqltext))
    }


def foreign_key_columns(table: Table) -> set[str]:
    return {column.name for fk in table.foreign_key_constraints for column in fk.columns}


def free_column(table: Table, group: tuple[str, ...]) -> str | None:
    """The column of a unique ``group`` that can be perturbed without breaking anything else:
    not under a CHECK (an enum must keep a legal value, ``singleton`` must stay 1) and not a
    foreign key (``coat_id`` must keep referencing the coat — otherwise a CHECK proof would be
    satisfied by the FK failing instead). ``None`` when the group has no such column."""
    checked = checked_columns(table)
    foreign = foreign_key_columns(table)
    for name in group:
        if name not in checked and name not in foreign:
            return name
    return None


def uniquified(table: Table, row: Mapping[str, Any], idgen: IdGenerator) -> dict[str, Any]:
    """A copy of ``row`` with a fresh id and, per unique group, one *free* column changed
    (``free_column``: never a CHECKed or foreign-key column), so it can be inserted next to the
    original and any refusal it then meets comes from the constraint under test. ``coat`` keeps
    ``id = slug`` (its CHECK). A group with no free column (``owner.singleton``) is left alone."""
    fresh = dict(row)
    suffix = str(idgen.new())[-6:]
    if table.name == "coat":
        fresh["id"] = fresh["slug"] = f"{row['slug']}-{suffix}"
    elif "id" in table.columns:
        fresh["id"] = idgen.new()
    for group in unique_groups(table):
        name = free_column(table, group)
        if name is None:
            continue
        value = fresh.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, str):
            fresh[name] = f"{value}-{suffix}"
        elif isinstance(value, int):
            fresh[name] = value + 1000
        elif value is not None and hasattr(value, "tzinfo"):
            fresh[name] = value + timedelta(hours=1)
    return fresh


@pytest.fixture
def seeded(engine: Engine, clock: FakeClock, idgen: IdGenerator) -> dict[str, dict[str, Any]]:
    return seed_all(engine, clock, idgen)


# --------------------------------------------------------------------------- inventory

SPEC15_KEY_FIELDS: dict[str, tuple[str, ...]] = {
    "owner": (
        "name",
        "whatsapp_number",
        "passphrase_hash",
        "second_channel",
        "deputy_id",
        "quiet_hours",
        "timezone",
    ),
    "coat": (
        "name",
        "legal_entity",
        "domain",
        "email_identity",
        "whatsapp_line",
        "signature_ref",
        "letterhead_ref",
        "tone_guide_ref",
        "knowledge_pack_ref",
        "mandate",
        "approval_rules",
        "allowed_activities",
        "banking_ref",
    ),
    "contact": (
        "coat_id",
        "name",
        "org",
        "role",
        "channels",
        "language",
        "register",
        "consent_status",
        "dnc_flag",
        "verified_phone",
        "source",
        "last_touch",
        "next_touch",
        "cadence_id",
    ),
    "conversation": (
        "coat_id",
        "contact_id",
        "channel",
        "thread_ref",
        "bucket",
        "state",
        "summary",
    ),
    "message": (
        "conversation_id",
        "direction",
        "channel",
        "body_ref",
        "language",
        "transcript_ref",
        "sent_by",
        "approval_id",
        "critic_score",
    ),
    "task": (
        "coat_id",
        "title",
        "owner_type",
        "owner_ref",
        "due",
        "status",
        "source",
        "parent_task_id",
    ),
    "approval": (
        "item_type",
        "item_ref",
        "tier",
        "amount",
        "requested_at",
        "decided_at",
        "decision",
        "reason",
        "passphrase_verified",
        "decided_via",
    ),
    "decision_journal": ("approval_id", "category", "decision", "reason_text", "pattern_tags"),
    "audit_event": (
        "ts",
        "desk",
        "coat_id",
        "actor",
        "action",
        "counterpart",
        "amount",
        "approval_id",
        "data_tier",
        "reason",
        "input_hash",
        "output_hash",
    ),
    "transaction": (
        "coat_id",
        "direction",
        "amount",
        "currency",
        "counterpart_ref",
        "beneficiary_id",
        "experiment_id",
        "task_id",
        "approval_id",
        "card_ref",
        "bank_ref",
        "status",
    ),
    "beneficiary": (
        "coat_id",
        "name",
        "bank_details_ref",
        "verified_at",
        "verified_by",
        "verification_method",
        "change_history",
    ),
    "document": (
        "entity_ref",
        "type",
        "tier",
        "title",
        "expiry",
        "allowed_recipients",
        "storage_ref",
        "sha256",
        "share_log",
        "version",
    ),
    "memory_record": (
        "store",
        "desk",
        "content",
        "source_refs",
        "confidence",
        "approved_by_owner",
        "expires_at",
    ),
    "experiment": (
        "hypothesis",
        "coat_id",
        "budget",
        "deadline",
        "metric",
        "status",
        "result",
        "kill_reason",
        "playbook_ref",
    ),
    "skill": (
        "name",
        "version",
        "trigger",
        "steps_ref",
        "inputs",
        "failure_signs",
        "dry_run_until",
        "approved_at",
    ),
    "incident": (
        "type",
        "detected_at",
        "detected_by",
        "first_response",
        "frozen_scope",
        "resolved_at",
        "postmortem_ref",
    ),
}

EXPECTED_SCOPES: dict[str, Scope] = {
    "owner": Scope.GOVERNANCE_ONLY,
    "coat": Scope.SHARED,
    "contact": Scope.DESK_ROW,
    "dnc_entry": Scope.SHARED,
    "conversation": Scope.DESK_ROW,
    "message": Scope.DESK_ROW,
    "task": Scope.DESK_ROW,
    "approval": Scope.SHARED,
    "decision_journal": Scope.SHARED,
    "audit_event": Scope.SHARED,
    "transaction": Scope.SHARED,
    "beneficiary": Scope.ASSISTANT_ONLY,
    "document": Scope.ASSISTANT_ONLY,
    "vault_secret": Scope.ASSISTANT_ONLY,
    "memory_record": Scope.DESK_ROW,
    "experiment": Scope.OPERATOR_ONLY,
    "skill": Scope.DESK_ROW,
    "incident": Scope.SHARED,
    "inbox_event": Scope.SHARED,
    "timer_slot": Scope.GOVERNANCE_ONLY,
    "release": Scope.SHARED,
    "freeze_state": Scope.SHARED,
    "audit_chain_head": Scope.SHARED,
    "passphrase_attempt": Scope.SHARED,
    "found_instruction": Scope.SHARED,
    "handoff": Scope.SHARED,
    "readback_pending": Scope.DESK_ROW,
    "card": Scope.GOVERNANCE_ONLY,  # DESIGN §5.2 "mutable (governance)": desks read, never write
    "card_authorization": Scope.SHARED,
    "second_channel_challenge": Scope.SHARED,
    "pending_owner_message": Scope.SHARED,
    "category_state": Scope.DESK_ROW,
    "model_trace": Scope.DESK_ROW,
    "auditor_report": Scope.AUDITOR_WRITE,
}


def test_sixteen_entities_and_the_infrastructure_tables_exist() -> None:
    names = {table.name for table in Base.metadata.sorted_tables}
    assert len(ENTITY_TABLES) == 16 and ENTITY_TABLES == set(SPEC15_KEY_FIELDS)
    assert names == ENTITY_TABLES | INFRA_TABLES
    assert not ENTITY_TABLES & INFRA_TABLES
    assert len(names) == 34
    row_classes = {
        name
        for name, obj in vars(models).items()
        if pyinspect.isclass(obj) and issubclass(obj, Base) and obj is not Base
    }
    assert len(row_classes) == 34 and all(name.endswith("Row") for name in row_classes)


@pytest.mark.parametrize(("entity", "fields"), sorted(SPEC15_KEY_FIELDS.items()))
def test_every_spec15_key_field_exists(entity: str, fields: tuple[str, ...]) -> None:
    table = _table(entity)
    missing = [field for field in fields if field not in table.columns]
    assert missing == [], f"{entity} lacks {missing}"


def test_every_table_has_the_base_columns() -> None:
    for table in Base.metadata.sorted_tables:
        if table.name == "audit_chain_head":
            assert set(table.primary_key.columns.keys()) == {"singleton"}
            assert "created_at" in table.columns and "updated_at" in table.columns
            continue
        assert set(table.primary_key.columns.keys()) == {"id"}, table.name
        assert "created_at" in table.columns, table.name
        if table.name == "audit_event":
            assert "updated_at" not in table.columns  # DESIGN §5: nothing about a row changes
        else:
            assert "updated_at" in table.columns, table.name
        id_type = table.columns["id"].type
        assert isinstance(id_type, String)
        assert id_type.length == (64 if table.name == "coat" else 26), table.name


def test_every_mapper_carries_a_scope_and_matching_table_flags() -> None:
    for table in Base.metadata.sorted_tables:
        flags = table_flags(table)
        mapper_class = table.info[MAPPER_INFO_KEY]
        assert flags["scope"] is EXPECTED_SCOPES[table.name], table.name
        assert mapper_class.__scope__ is EXPECTED_SCOPES[table.name]
        assert table.info[TABLE_INFO_KEY] is flags
        if flags["scope"] is Scope.DESK_ROW:
            assert "desk" in table.columns, table.name
    assert len(EXPECTED_SCOPES) == 34


def test_append_only_tables_are_exactly_the_flagged_ones() -> None:
    flagged = {t.name for t in Base.metadata.sorted_tables if table_flags(t)["append_only"]}
    assert flagged == APPEND_ONLY_TABLES
    assert APPEND_ONLY_TABLES == {
        "audit_event",
        "decision_journal",
        "passphrase_attempt",
        "found_instruction",
        "card_authorization",
        "timer_slot",
        "model_trace",
        "auditor_report",
    }


# --------------------------------------------------------------------------- types


@pytest.mark.parametrize(
    ("table", "column", "type_"),
    [
        ("approval", "amount", BigInteger),
        ("transaction", "amount", BigInteger),
        ("audit_event", "amount", BigInteger),
        ("experiment", "budget", BigInteger),
        ("card", "monthly_cap", BigInteger),
        ("card_authorization", "amount", BigInteger),
        ("document", "expiry", Date),
        ("experiment", "deadline", Date),
        ("skill", "dry_run_until", Date),
        ("category_state", "started_at", Date),
        ("auditor_report", "day", Date),
        ("memory_record", "confidence", Float),
        ("message", "critic_score", Float),
        ("beneficiary", "bank_details_ct", EncryptedBytes),
        ("vault_secret", "ciphertext", EncryptedBytes),
        ("owner", "passphrase_fp", LargeBinary),
        ("document", "tier", Integer),
        ("audit_event", "data_tier", Integer),
        ("audit_event", "seq", Integer),
        ("approval", "seq", Integer),
        ("audit_chain_head", "last_seq", Integer),
        ("contact", "dnc_flag", Boolean),
        ("audit_event", "dry_run", Boolean),
        ("inbox_event", "signature_valid", Boolean),
    ],
)
def test_column_types(table: str, column: str, type_: type) -> None:
    assert isinstance(_table(table).columns[column].type, type_)


def test_every_datetime_column_is_utcdatetime_and_every_json_column_is_jsonb_on_postgres() -> None:
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, DateTime) or isinstance(column.type, UtcDateTime):
                assert isinstance(column.type, UtcDateTime), f"{table.name}.{column.name}"
                assert column.type.impl.timezone is True
            if isinstance(column.type, JSON):
                variants = column.type._variant_mapping
                assert "postgresql" in variants, f"{table.name}.{column.name}"
                assert variants["postgresql"].__class__.__name__ == "JSONB"


def test_every_desk_column_is_the_desk_enum_with_a_check() -> None:
    desk_tables = [t for t in Base.metadata.sorted_tables if "desk" in t.columns]
    assert len(desk_tables) >= 15
    for table in desk_tables:
        column = table.columns["desk"]
        assert isinstance(column.type, Enum) and column.type.enums == [d.value for d in Desk]
        assert column.type.native_enum is False and column.type.create_constraint is True
        assert not column.nullable


def test_money_columns_have_a_currency_sibling() -> None:
    for table, column in (
        ("approval", "amount"),
        ("transaction", "amount"),
        ("card", "monthly_cap"),
    ):
        assert isinstance(_table(table).columns[column].type, BigInteger)
        assert isinstance(_table(table).columns["currency"].type, String)


def test_passphrase_is_never_persisted_in_plain_form() -> None:
    attempt = _table("passphrase_attempt")
    assert {"body", "candidate", "passphrase", "text"}.isdisjoint(attempt.columns.keys())
    owner = _table("owner")
    assert "passphrase" not in owner.columns
    assert {"passphrase_hash", "passphrase_fp"} <= set(owner.columns.keys())
    assert "passphrase_attempt" in _table("inbox_event").columns  # the outcome, never the value


def test_entry_hash_is_documented_as_a_keyed_hmac() -> None:
    column = _table("audit_event").columns["entry_hash"]
    assert column.unique and column.comment is not None
    assert "HMAC" in column.comment and "keyed_hash" in column.comment
    assert "sha256" in column.comment  # "never a plain sha256"


EXPECTED_IMMUTABLE: dict[str, set[str]] = {
    # every column but the one transition (and updated_at), DESIGN §5 "insert + ONE transition"
    "approval": set(_table("approval").columns.keys())
    - set(APPROVAL_DECISION_COLUMNS)
    - {"updated_at"},
    "release": set(_table("release").columns.keys()) - {"burnt_at", "updated_at"},
    "handoff": set(_table("handoff").columns.keys()) - {"taken_at", "updated_at"},
    "incident": set(_table("incident").columns.keys())
    - {"resolved_at", "postmortem_ref", "updated_at"},
    "document": set(_table("document").columns.keys()) - {"share_log", "updated_at"},
    "inbox_event": set(_table("inbox_event").columns.keys())
    - set(INBOX_ACK_COLUMNS)
    - {"updated_at"},
}


def test_immutable_markers_freeze_every_column_but_the_transition() -> None:
    for table in Base.metadata.sorted_tables:
        flags = table_flags(table)
        expected = EXPECTED_IMMUTABLE.get(table.name, set())
        assert set(flags["immutable"]) == expected, table.name
        assert table.info[MAPPER_INFO_KEY].__immutable__ == flags["immutable"]
        assert not set(flags["immutable"]) & set(flags["single_transition"]), table.name
        assert not set(flags["immutable"]) & set(flags["forward_only"]), table.name
    assert {"nonce", "call_id", "tier", "approval_id", "minted_by", "desk"} <= EXPECTED_IMMUTABLE[
        "release"
    ]
    assert {
        "action_json",
        "amount",
        "currency",
        "seq",
        "item_ref",
        "expires_at",
    } <= EXPECTED_IMMUTABLE["approval"]
    assert (
        set(INBOX_AUTH_COLUMNS) | {"sender", "body", "payload", "desk"}
        <= EXPECTED_IMMUTABLE["inbox_event"]
    )
    assert {"storage_ref", "sha256", "content_text", "metadata"} <= EXPECTED_IMMUTABLE["document"]


def test_desk_partitioned_tables_are_inbox_event_and_pending_owner_message() -> None:
    """``__desk_partitioned__`` (DESIGN §4a; fixer round 2): the two SHARED tables whose rows
    carry the other desk's text — an owner-thread or owner-mailbox body routed to the Assistant,
    and the brief / read-back / quoted instruction the Assistant drafts for the owner from it —
    are partitioned per desk for a desk token while their scope stays SHARED; every other table
    is either DESK_ROW (partitioned by scope) or confined by its scope."""
    partitioned = {
        t.name for t in Base.metadata.sorted_tables if table_flags(t)["desk_partitioned"]
    }
    assert partitioned == set(models.DESK_PARTITIONED_TABLES)
    assert partitioned == {"inbox_event", "pending_owner_message"}
    for name in sorted(partitioned):
        table = _table(name)
        assert table.info[MAPPER_INFO_KEY].__desk_partitioned__ is True, name
        assert table_flags(table)["scope"] is Scope.SHARED, name
        assert "desk" in table.columns, name  # what the partition is keyed on
    assert models.PendingOwnerMessageRow.__desk_partitioned__ is True
    assert models.PendingOwnerMessageRow.__scope__ is Scope.SHARED
    assert models.PendingOwnerMessageRow.__single_transition__ == ("sent_at", "provider_msg_id")
    for other in Base.metadata.sorted_tables:
        if other.name not in partitioned:
            assert other.info[MAPPER_INFO_KEY].__desk_partitioned__ is False, other.name


DESKS = (OPERATOR_ROLE, ASSISTANT_ROLE)
EXPECTED_GRANTS: dict[str, dict[str, RoleGrant]] = {
    # DESIGN §5 mutability column, per role (None: not granted; ALL_COLUMNS: every column)
    "inbox_event": {
        **{r: RoleGrant(INBOX_DESK_INSERT_COLUMNS, INBOX_ACK_COLUMNS, False) for r in DESKS},
        INGRESS_ROLE: RoleGrant(ALL_COLUMNS, None, False),
        SCHEDULER_ROLE: RoleGrant(ALL_COLUMNS, None, True),
    },
    "handoff": {
        ASSISTANT_ROLE: RoleGrant(ALL_COLUMNS, None, False),
        OPERATOR_ROLE: RoleGrant(None, ("taken_at",), False),
        INGRESS_ROLE: RoleGrant(None, None, False),
        SCHEDULER_ROLE: RoleGrant(None, None, False),
    },
    "approval": {
        r: RoleGrant(ALL_COLUMNS, APPROVAL_DECISION_COLUMNS, False)
        for r in (*DESKS, INGRESS_ROLE, SCHEDULER_ROLE)
    },
    "release": {
        r: RoleGrant(ALL_COLUMNS, ("burnt_at",), False)
        for r in (*DESKS, INGRESS_ROLE, SCHEDULER_ROLE)
    },
    "passphrase_attempt": {r: RoleGrant(None, None, False) for r in (*DESKS, SCHEDULER_ROLE)},
    "dnc_entry": {r: RoleGrant(ALL_COLUMNS, None, False) for r in DESKS},
    "freeze_state": {r: RoleGrant(ALL_COLUMNS, None, False) for r in DESKS},
    "coat": {r: RoleGrant(None, None, False) for r in DESKS},
}


def test_per_role_grants_match_the_design_mutability_column() -> None:
    declared = {t.name: grants_of(t) for t in Base.metadata.sorted_tables if t.info.get(GRANTS_KEY)}
    assert declared == EXPECTED_GRANTS
    inbox = EXPECTED_GRANTS["inbox_event"][OPERATOR_ROLE]
    assert inbox.insert is not None and set(INBOX_AUTH_COLUMNS).isdisjoint(inbox.insert)
    assert set(inbox.insert) | set(INBOX_AUTH_COLUMNS) == set(_table("inbox_event").columns.keys())
    for forbidden in ("sender", "signature_valid", "passphrase_attempt", "body", "desk"):
        assert inbox.update is not None and forbidden not in inbox.update
    assert not inbox.may_insert(["id", "desk", "signature_valid"])
    assert inbox.may_insert(["id", "desk", "sender", "body"]) and not inbox.may_update(["body"])
    # the SQLite guard sees the union of a desk's roles: governance is ingress + scheduler
    governance = grant_for_desk(_table("inbox_event"), "governance")
    assert governance == RoleGrant(ALL_COLUMNS, None, True)
    assert grant_for_desk(_table("handoff"), "operator") == RoleGrant(None, ("taken_at",), False)
    assert grant_for_desk(_table("contact"), "operator") is None  # scope defaults
    with pytest.raises(ValueError, match="unknown role"):
        grants_of(Table("x", MetaData(), Column("id", String(1)), info={GRANTS_KEY: {"root": {}}}))
    with pytest.raises(ValueError, match="unknown columns"):
        grants_of(
            Table(
                "y",
                MetaData(),
                Column("id", String(1)),
                info={GRANTS_KEY: {OPERATOR_ROLE: {"update": ("no",)}}},
            )
        )


def test_owner_secrets_are_deferred_and_hidden_from_desks() -> None:
    """DESIGN §5.1 owner: desks read ``whatsapp_number``, ``second_channel``, ``quiet_hours``
    through a column grant; the two ``passphrase*`` columns are never in a default SELECT."""
    owner = _table("owner")
    assert hidden_columns(owner) == {"passphrase_hash", "passphrase_fp"}
    mapper = inspect(OwnerRow)
    assert mapper.attrs["passphrase_hash"].deferred and mapper.attrs["passphrase_fp"].deferred
    assert not mapper.attrs["whatsapp_number"].deferred
    for dialect in (postgresql.dialect(), sqlite.dialect()):
        rendered = str(select(OwnerRow).compile(dialect=dialect))
        assert "passphrase" not in rendered and "whatsapp_number" in rendered
    for table in Base.metadata.sorted_tables:
        if table.name != "owner":
            assert hidden_columns(table) == frozenset(), table.name


# --------------------------------------------------------------------------- indexes and uniques


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        ("contact", ("coat_id", "desk", "primary_address")),
        ("conversation", ("coat_id", "channel", "thread_ref")),
        ("message", ("provider_msg_id",)),
        ("audit_event", ("invocation_id", "phase")),
        ("audit_event", ("entry_hash",)),
        ("audit_event", ("seq",)),
        ("approval", ("seq",)),
        ("transaction", ("card_auth_ref",)),
        ("beneficiary", ("coat_id", "name")),
        ("skill", ("name", "version")),
        ("inbox_event", ("source_kind", "provider_msg_id")),
        ("timer_slot", ("timer_name", "slot")),
        ("release", ("call_id",)),
        ("release", ("nonce",)),
        ("card", ("holder",)),
        ("card", ("card_ref",)),
        ("card_authorization", ("auth_ref",)),
        ("second_channel_challenge", ("token_hash",)),
        ("category_state", ("coat_id", "category", "desk")),
        ("dnc_entry", ("address",)),
        ("coat", ("slug",)),
        ("coat", ("email_identity",)),
        ("owner", ("whatsapp_number",)),
        ("vault_secret", ("uri",)),
    ],
)
def test_unique_constraints(table: str, columns: tuple[str, ...]) -> None:
    uniques = {
        tuple(c.name for c in constraint.columns)
        for constraint in _table(table).constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert columns in uniques, f"{table} uniques: {sorted(uniques)}"


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        ("contact", ("next_touch",)),
        ("message", ("conversation_id", "created_at")),
        ("task", ("coat_id", "status")),
        ("task", ("due",)),
        ("approval", ("decision",)),
        ("decision_journal", ("category", "created_at")),
        ("audit_event", ("ts",)),
        ("audit_event", ("event_id",)),
        ("audit_event", ("invocation_id",)),
        ("transaction", ("coat_id", "occurred_at")),
        ("transaction", ("holder", "occurred_at")),
        ("transaction", ("status",)),
        ("document", ("entity_ref", "type")),
        ("document", ("expiry",)),
        ("document", ("tier",)),
        ("memory_record", ("desk", "store", "created_at")),
        ("experiment", ("coat_id", "status")),
        ("incident", ("type", "detected_at")),
        ("inbox_event", ("desk", "acked_at", "priority", "received_at")),
        ("freeze_state", ("released_at",)),
        ("passphrase_attempt", ("at",)),
        ("found_instruction", ("event_id",)),
        ("readback_pending", ("expires_at",)),
        ("card_authorization", ("card_ref", "created_at")),
        ("second_channel_challenge", ("ref",)),
        ("pending_owner_message", ("sent_at",)),
        ("model_trace", ("event_id",)),
    ],
)
def test_indexes(table: str, columns: tuple[str, ...]) -> None:
    indexes = {tuple(c.name for c in index.columns) for index in _table(table).indexes}
    assert columns in indexes, f"{table} indexes: {sorted(indexes)}"


def test_partial_indexes_are_partial_on_postgres_only() -> None:
    for table, name in (
        ("approval", "ix_approval_decision"),
        ("freeze_state", "ix_freeze_state_released_at"),
        ("pending_owner_message", "ix_pending_owner_message_sent_at"),
    ):
        index = next(i for i in _table(table).indexes if i.name == name)
        assert isinstance(index, Index)
        assert "IS NULL" in str(index.dialect_options["postgresql"]["where"])
        assert index.dialect_options["sqlite"].get("where") is None


# --------------------------------------------------------------------------- rows land


def test_a_valid_row_lands_in_every_table(
    seeded: dict[str, dict[str, Any]], engine: Engine
) -> None:
    with engine.connect() as connection:
        for table in Base.metadata.sorted_tables:
            count = connection.execute(select(table).limit(5)).all()
            assert len(count) == 1, table.name
    assert len(seeded) == 34


def test_timestamps_default_to_the_process_clock_not_the_database(
    engine: Engine,
    clock: FakeClock,
    idgen: IdGenerator,
    session_factory: Any,
    tokens: dict[str, Any],
) -> None:
    rows = sample_rows(clock, idgen)
    with engine.begin() as connection:
        connection.execute(_table("coat").insert().values(**rows["coat"]))
    clock.advance(timedelta(hours=2))
    expected = clock.now()
    contact = ContactRow(**rows["contact"])
    with session_factory(tokens["operator"]).write() as session:
        session.add(contact)
    assert contact.created_at == expected and contact.updated_at == expected
    defaulted = {
        (table.name, column.name)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if column.server_default is not None
    }
    # the one server default: a desk-published inbox event cannot carry a valid signature
    # (DESIGN §4d; the desks' column-level INSERT grant omits it), so the column defaults false
    assert defaulted == {("inbox_event", "signature_valid")}
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, UtcDateTime):
                assert column.server_default is None, f"{table.name}.{column.name}"


def test_bulk_insert_defaults_also_come_from_the_process_clock(
    seeded: dict[str, dict[str, Any]], engine: Engine, clock: FakeClock
) -> None:
    with engine.connect() as connection:
        row = connection.execute(select(AuditEventRow.__table__)).one()
    assert row._mapping["created_at"] == clock.now()


# --------------------------------------------------------------------------- CHECKs refuse


CHECK_VIOLATIONS: list[tuple[str, dict[str, Any]]] = [
    ("document", {"tier": 2, "content_text": "a Tier 2 body in the index"}),
    ("memory_record", {"store": "owner_profile", "desk": "operator"}),
    ("memory_record", {"store": "episodic", "desk": "governance"}),
    ("memory_record", {"store": "dreams"}),
    ("experiment", {"desk": "assistant"}),
    ("approval", {"tier": "A"}),
    ("approval", {"tier": "N"}),
    ("approval", {"decision": "maybe"}),
    ("handoff", {"pushed_by": "operator"}),
    ("conversation", {"bucket": "later"}),
    ("message", {"direction": "sideways"}),
    ("message", {"sent_by": "bot"}),
    ("task", {"owner_type": "alien"}),
    ("task", {"source": "dream"}),
    ("passphrase_attempt", {"outcome": "maybe"}),
    ("inbox_event", {"passphrase_attempt": "maybe"}),
    ("inbox_event", {"source_kind": "carrier_pigeon"}),
    ("inbox_event", {"origin": "telepathy"}),
    ("audit_event", {"phase": "middle"}),
    ("audit_event", {"actor": "alien"}),
    ("audit_event", {"status": "whatever"}),
    ("audit_event", {"tier": "Z"}),
    ("audit_event", {"data_tier": 4}),
    ("transaction", {"status": "bogus"}),
    ("transaction", {"direction": "up"}),
    ("release", {"minted_by": "model"}),
    ("release", {"tier": "Z"}),
    ("freeze_state", {"scope": "everything"}),
    ("incident", {"type": "bad_hair_day"}),
    ("incident", {"detected_by": "cat"}),
    ("incident", {"frozen_scope": "everything"}),
    ("category_state", {"tier": "Z"}),
    (
        "coat",
        {
            "id": "other-coat",
            "slug": "not-the-id",
            "name": "Other",
            "email_identity": "x@y",
            "whatsapp_line": "+1",
        },
    ),
    ("owner", {"singleton": 2}),
    ("audit_chain_head", {"singleton": 2}),
]


@pytest.mark.parametrize(("table", "overrides"), CHECK_VIOLATIONS)
def test_check_constraints_refuse_invalid_rows(
    table: str,
    overrides: dict[str, Any],
    seeded: dict[str, dict[str, Any]],
    engine: Engine,
    idgen: IdGenerator,
) -> None:
    target = _table(table)
    row = uniquified(target, seeded[table], idgen)
    row.update(overrides)
    # the *CHECK* refuses — not a foreign key or a unique index the perturbed row might trip
    with pytest.raises(IntegrityError, match="CHECK constraint failed"), engine.begin() as c:
        c.execute(target.insert().values(**row))
    with engine.connect() as connection:
        assert len(connection.execute(select(target)).all()) == 1  # nothing landed


def test_uniquified_rows_keep_their_foreign_keys_and_checks(
    seeded: dict[str, dict[str, Any]], engine: Engine, idgen: IdGenerator
) -> None:
    """The CHECK proofs above are only as good as the sample row: a ``uniquified`` copy of every
    table's row (with no override) must land, so a CHECK violation is the override's doing."""
    stuck = {"owner", "audit_chain_head"}  # singleton = 1: a second row cannot exist by design
    for table in Base.metadata.sorted_tables:
        if table.name in stuck:
            continue
        row = uniquified(table, seeded[table.name], idgen)
        assert foreign_key_columns(table).isdisjoint(
            {k for k in row if row[k] != seeded[table.name].get(k)}
        ), table.name
        with engine.begin() as connection:
            connection.execute(table.insert().values(**row))
        with engine.connect() as connection:
            assert len(connection.execute(select(table)).all()) == 2, table.name


def test_desk_check_holds_at_the_database_below_the_enum_type(
    seeded: dict[str, dict[str, Any]], engine: Engine
) -> None:
    """The ``desk`` Enum refuses an unknown value in Python (``validate_strings``); the CHECK it
    created refuses the same value when raw SQL goes around the type."""
    with pytest.raises(IntegrityError, match="CHECK constraint failed"), engine.begin() as c:
        c.exec_driver_sql("UPDATE contact SET desk = 'mars'")
    with pytest.raises(StatementError), engine.begin() as connection:
        connection.execute(_table("contact").update().values(desk="mars"))
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT desk FROM contact").scalar() == "operator"


@pytest.mark.parametrize(
    ("table", "overrides"),
    [
        ("document", {"tier": 2, "content_text": None}),
        ("document", {"tier": 1, "content_text": "indexable Tier 1 text"}),
        ("memory_record", {"store": "owner_profile", "desk": "assistant"}),
        ("memory_record", {"store": "semantic", "desk": "operator"}),
        ("experiment", {"desk": "operator"}),
        ("approval", {"tier": "K", "decision": "reject"}),
        ("audit_event", {"tier": None}),
        ("inbox_event", {"passphrase_attempt": "wrong"}),
        ("transaction", {"status": "reconciled"}),
        ("conversation", {"bucket": "draft"}),  # same coat: the CHECK, not the FK, decides
        ("category_state", {"tier": "A"}),
        ("incident", {"frozen_scope": "high_impact"}),
        ("incident", {"frozen_scope": None}),
    ],
)
def test_check_constraints_accept_valid_variants(
    table: str,
    overrides: dict[str, Any],
    seeded: dict[str, dict[str, Any]],
    engine: Engine,
    idgen: IdGenerator,
) -> None:
    target = _table(table)
    row = uniquified(target, seeded[table], idgen)
    row.update(overrides)
    with engine.begin() as connection:
        connection.execute(target.insert().values(**row))
    with engine.connect() as connection:
        assert len(connection.execute(select(target)).all()) == 2


UNIQUE_GROUPS: list[tuple[str, tuple[str, ...]]] = sorted(
    (table.name, group)
    for table in Base.metadata.sorted_tables
    for group in unique_groups(table)
    # Two groups cannot be isolated from their table's other constraints: ``owner.whatsapp_number``
    # (``singleton`` must stay 1 and so collides first) and ``coat.slug`` (``slug = id`` makes a
    # duplicate slug a duplicate primary key); the metadata test above covers them.
    if (table.name, group) not in {("owner", ("whatsapp_number",)), ("coat", ("slug",))}
)


@pytest.mark.parametrize(
    ("table", "group"), UNIQUE_GROUPS, ids=lambda v: v if isinstance(v, str) else "+".join(v)
)
def test_unique_constraints_refuse_a_duplicate_at_the_database(
    table: str,
    group: tuple[str, ...],
    seeded: dict[str, dict[str, Any]],
    engine: Engine,
    idgen: IdGenerator,
) -> None:
    """Per unique group: a row that differs from the sample row in every other group but repeats
    this one is refused *by name* — ``UNIQUE constraint failed: <table>.<first column>`` — or,
    on an append-only / single-transition table, by the ``_no_replace`` BEFORE INSERT trigger
    that covers exactly this unique group (it fires before the index is checked, which is what
    stops ``INSERT OR REPLACE`` from rewriting the row through the collision)."""
    target = _table(table)
    flags = table_flags(target)
    duplicate = uniquified(target, seeded[table], idgen)
    for column in group:
        duplicate[column] = seeded[table][column]
    if flags["append_only"]:
        pattern = rf"{re.escape(table)} is append-only"
    elif flags["single_transition"]:
        pattern = rf"{re.escape(table)} rows are never deleted or replaced"
    else:
        pattern = rf"UNIQUE constraint failed: {re.escape(table)}\.{re.escape(group[0])}"
    with pytest.raises(IntegrityError, match=pattern), engine.begin() as connection:
        connection.execute(target.insert().values(**duplicate))
    with engine.connect() as connection:
        assert len(connection.execute(select(target)).all()) == 1


def test_foreign_keys_are_enforced(
    seeded: dict[str, dict[str, Any]], engine: Engine, idgen: IdGenerator
) -> None:
    message = uniquified(_table("message"), seeded["message"], idgen)
    message["conversation_id"] = idgen.new()
    with pytest.raises(IntegrityError, match="FOREIGN KEY"), engine.begin() as connection:
        connection.execute(_table("message").insert().values(**message))
    contact = uniquified(_table("contact"), seeded["contact"], idgen)
    contact["coat_id"] = "no-such-coat"
    with pytest.raises(IntegrityError, match="FOREIGN KEY"), engine.begin() as connection:
        connection.execute(_table("contact").insert().values(**contact))


def test_encrypted_columns_refuse_plaintext(
    seeded: dict[str, dict[str, Any]], engine: Engine, idgen: IdGenerator
) -> None:
    for table, column in (("beneficiary", "bank_details_ct"), ("vault_secret", "ciphertext")):
        row = uniquified(_table(table), seeded[table], idgen)
        row[column] = b"AE07 0331 2345 6789 0123 456"
        with pytest.raises(StatementError) as info, engine.begin() as connection:
            connection.execute(_table(table).insert().values(**row))
        assert isinstance(info.value.orig, Tier2LeakError)
        assert "AE07" not in str(info.value)


def test_coat_id_is_the_slug(seeded: dict[str, dict[str, Any]], engine: Engine) -> None:
    with engine.connect() as connection:
        coat = connection.execute(select(_table("coat"))).one()._mapping
        contact = connection.execute(select(_table("contact"))).one()._mapping
    assert coat["id"] == coat["slug"] == contact["coat_id"] == COAT


# --------------------------------------------------------------------------- module API


def test_brain_and_auditor_metadata_split_base_metadata() -> None:
    assert set(auditor_metadata.tables) == {"auditor.auditor_report"}
    assert set(brain_metadata.tables) | set(auditor_metadata.tables) == set(Base.metadata.tables)
    assert not set(brain_metadata.tables) & set(auditor_metadata.tables)
    for copy_metadata in (brain_metadata, auditor_metadata):
        for key, copy in copy_metadata.tables.items():
            original = Base.metadata.tables[key]
            assert copy is not original
            assert table_flags(copy) == table_flags(original)
            assert copy.info.get(GRANTS_KEY) == original.info.get(GRANTS_KEY)
            assert copy.columns.keys() == original.columns.keys()
            assert {i.name for i in copy.indexes} == {i.name for i in original.indexes}
    sqlite_view = metadata_for_dialect("sqlite")
    assert set(sqlite_view.tables) == {t.name for t in Base.metadata.sorted_tables}
    assert sqlite_view.tables["auditor_report"].schema is None
    assert metadata_for_dialect("postgresql") is Base.metadata


def test_all_text_columns_covers_text_and_json_and_skips_blobs_and_numbers() -> None:
    pairs = {(table.name, column.name) for table, column in all_text_columns()}
    for expected in (
        ("memory_record", "content"),
        ("inbox_event", "body"),
        ("audit_event", "reason"),
        ("audit_event", "counterpart"),
        ("message", "body_text"),
        ("found_instruction", "quote"),
        ("pending_owner_message", "text"),
        ("model_trace", "response_json"),
        ("handoff", "handoff_json"),
        ("contact", "desk"),
        ("approval", "item_ref"),  # TEXT (DESIGN §5), not a 26-char ULID column
    ):
        assert expected in pairs, expected
    for excluded in (
        ("beneficiary", "bank_details_ct"),
        ("vault_secret", "ciphertext"),
        ("owner", "passphrase_fp"),
        ("audit_event", "amount"),
        ("audit_event", "ts"),
        ("document", "expiry"),
        ("memory_record", "confidence"),
    ):
        assert excluded not in pairs, excluded
    assert len(pairs) > 250


def test_importing_models_has_no_side_effects() -> None:
    assert not any(isinstance(value, Engine | Session) for value in vars(models).values())
    assert not any(
        isinstance(value, type) and issubclass(value, Clock) for value in vars(models).values()
    )
    assert inspect(ContactRow).local_table is _table("contact")
