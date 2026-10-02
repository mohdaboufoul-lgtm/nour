"""The desk wall on the real models (DESIGN §4a, §7.3 ``tests/unit/test_db_wall.py``; SPEC §5
§13): an Operator session cannot select or insert the Assistant-only mappers (``document``,
``vault_secret``, ``beneficiary``), an Assistant session cannot touch ``experiment``; every
DESK_ROW mapper (``contact``, ``memory_record``, ``conversation``, ``message``, ``task``,
``skill``, ``readback_pending``, ``category_state``, ``model_trace``) is auto-filtered to the
token's partition even without a WHERE, and so are the desk-partitioned ``inbox_event`` and
``pending_owner_message`` (the Operator never reads the Assistant's drafts for the owner); a flush
with the other desk's ``desk`` raises; the owner profile is Assistant-only by CHECK and by
partition; the governance rows (``owner``, ``card``) are readable by desks but writable by
governance only and the owner's passphrase columns never reach a desk; the per-role grants of
DESIGN §5 (inbox ack-only, handoff one-way, DNC / freeze / coat / passphrase_attempt writers)
hold on SQLite exactly as the Postgres grants do; the auditor reads SHARED, never a partition,
and appends ``auditor_report`` only.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any, cast

import pytest
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import load_only, undefer

from nour.core.clock import FakeClock, IdGenerator
from nour.core.errors import AppendOnlyViolation, DeskWallViolation, SingleTransitionViolation
from nour.core.tokens import AnyToken
from nour.core.types import Desk, Ulid
from nour.db.base import Ciphertext, Scope, scope_allows
from nour.db.models import (
    AuditEventRow,
    AuditorReportRow,
    BeneficiaryRow,
    CardRow,
    CategoryStateRow,
    CoatRow,
    ContactRow,
    ConversationRow,
    DncEntryRow,
    DocumentRow,
    ExperimentRow,
    FreezeStateRow,
    HandoffRow,
    InboxEventRow,
    MemoryRecordRow,
    MessageRow,
    ModelTraceRow,
    OwnerRow,
    PassphraseAttemptRow,
    PendingOwnerMessageRow,
    ReadbackPendingRow,
    SkillRow,
    TaskRow,
    VaultSecretRow,
)
from nour.db.session import SessionFactory
from tests.unit.test_models_columns import COAT, NUMBER, sample_rows

Factories = dict[str, SessionFactory]


@pytest.fixture
def sf(
    engine: Engine,
    session_factory: Callable[[AnyToken], SessionFactory],
    tokens: dict[str, AnyToken],
) -> Factories:
    return {kind: session_factory(token) for kind, token in tokens.items()}


@pytest.fixture
def rows(clock: FakeClock, idgen: IdGenerator) -> dict[str, dict[str, Any]]:
    return sample_rows(clock, idgen)


@pytest.fixture
def coat(sf: Factories, rows: dict[str, dict[str, Any]]) -> str:
    """The coat every coat_id references, written through the governance (SHARED) session."""
    with sf["governance"].write() as session:
        session.add(CoatRow(**rows["coat"]))
    return COAT


def _contact(idgen: IdGenerator, desk: Desk, address: str) -> ContactRow:
    return ContactRow(
        id=idgen.new(),
        desk=desk,
        coat_id=COAT,
        name=f"{desk.value} contact",
        channels={"whatsapp": address},
        primary_address=address,
        language="ar",
        register="formal",
        consent_status="opt_in",
        dnc_flag=False,
        source="inbound",
        audit_id=idgen.new(),
    )


def _memory(idgen: IdGenerator, desk: Desk, store: str) -> MemoryRecordRow:
    return MemoryRecordRow(
        id=idgen.new(),
        desk=desk,
        coat_id=COAT,
        store=store,
        content=f"{desk.value} {store} note",
        source_refs=[],
        confidence=0.8,
        approved_by_owner=False,
        audit_id=idgen.new(),
    )


@pytest.fixture
def partitioned(sf: Factories, coat: str, idgen: IdGenerator) -> dict[str, list[Ulid]]:
    """Two contacts and one semantic memory per desk, each written by its own desk."""
    ids: dict[str, list[Ulid]] = {}
    for kind in ("operator", "assistant"):
        desk = Desk(kind)
        with sf[kind].write() as session:
            contacts = [_contact(idgen, desk, f"+9715000000{n}{kind[0]}") for n in range(2)]
            memory = _memory(idgen, desk, "semantic")
            session.add_all([*contacts, memory])
            session.flush()
            ids[kind] = [c.id for c in contacts]
    return ids


# --------------------------------------------------------------------------- *_ONLY scopes


@pytest.mark.parametrize("mapper", [DocumentRow, VaultSecretRow, BeneficiaryRow])
def test_operator_cannot_read_assistant_only_mappers(sf: Factories, mapper: type[Any]) -> None:
    with (
        sf["operator"].session() as session,
        pytest.raises(DeskWallViolation, match="may not read"),
    ):
        session.execute(select(mapper))
    with sf["operator"].session() as session, pytest.raises(DeskWallViolation):
        session.scalar(select(func.count()).select_from(mapper))
    # the same statement is fine for the Assistant (its own scope)
    with sf["assistant"].session() as session:
        assert session.scalars(select(mapper)).all() == []


def test_operator_cannot_insert_assistant_only_rows(
    sf: Factories, coat: str, rows: dict[str, dict[str, Any]], idgen: IdGenerator
) -> None:
    for mapper, key in ((DocumentRow, "document"), (BeneficiaryRow, "beneficiary")):
        data = dict(rows[key])
        if key == "document":
            data["meta"] = data.pop("metadata")
        with pytest.raises(DeskWallViolation, match="may not write"), sf["operator"].write() as s:
            s.add(mapper(**data))
    with pytest.raises(DeskWallViolation), sf["operator"].write() as s:
        s.execute(
            insert(VaultSecretRow).values(
                id=idgen.new(),
                entity_ref="x",
                key="iban",
                uri="vault://x/banking/receiving#iban",
                ciphertext=Ciphertext(b"\x00"),
                last4="0000",
                content_fp="hmac:x",
                version=1,
            )
        )
    with sf["assistant"].session() as session:
        assert session.scalars(select(DocumentRow)).all() == []
        assert session.scalars(select(BeneficiaryRow)).all() == []
        assert session.scalars(select(VaultSecretRow)).all() == []


def test_assistant_and_governance_cannot_touch_experiments(
    sf: Factories, coat: str, rows: dict[str, dict[str, Any]]
) -> None:
    with sf["operator"].write() as session:
        session.add(ExperimentRow(**rows["experiment"]))
    for kind in ("assistant", "governance"):
        with sf[kind].session() as session, pytest.raises(DeskWallViolation):
            session.execute(select(ExperimentRow))
        with pytest.raises(DeskWallViolation), sf[kind].write() as session:
            session.execute(update(ExperimentRow).values(status="killed"))
    with sf["operator"].session() as session:
        assert [e.status for e in session.scalars(select(ExperimentRow))] == ["running"]


def test_scope_table_matches_the_models(tokens: dict[str, AnyToken]) -> None:
    assert scope_allows(DocumentRow.__scope__, tokens["operator"], write=False) is False
    assert scope_allows(DocumentRow.__scope__, tokens["assistant"], write=True) is True
    assert scope_allows(ExperimentRow.__scope__, tokens["operator"], write=True) is True
    assert scope_allows(ExperimentRow.__scope__, tokens["assistant"], write=False) is False
    assert scope_allows(OwnerRow.__scope__, tokens["operator"], write=False) is True
    assert scope_allows(OwnerRow.__scope__, tokens["operator"], write=True) is False
    assert scope_allows(AuditorReportRow.__scope__, tokens["auditor"], write=True) is True
    assert scope_allows(ContactRow.__scope__, tokens["auditor"], write=False) is False
    assert DocumentRow.__scope__ is Scope.ASSISTANT_ONLY
    assert ExperimentRow.__scope__ is Scope.OPERATOR_ONLY


# --------------------------------------------------------------------------- DESK_ROW partitions


def test_desk_row_mappers_are_partitioned_without_a_where(
    sf: Factories, partitioned: dict[str, list[Ulid]]
) -> None:
    for kind in ("operator", "assistant"):
        with sf[kind].session() as session:
            contacts = session.scalars(select(ContactRow)).all()
            assert sorted(c.id for c in contacts) == sorted(partitioned[kind])
            assert {c.desk for c in contacts} == {Desk(kind)}
            assert session.scalar(select(func.count()).select_from(ContactRow)) == 2
            notes = session.scalars(select(MemoryRecordRow.content)).all()
            assert notes == [f"{kind} semantic note"]
            # a WHERE naming the other desk is still partitioned to nothing
            other = Desk("assistant" if kind == "operator" else "operator")
            assert session.scalars(select(ContactRow).where(ContactRow.desk == other)).all() == []
    with sf["governance"].session() as session:  # its own, empty partition
        assert session.scalars(select(ContactRow)).all() == []
        assert session.scalar(select(func.count()).select_from(MemoryRecordRow)) == 0


def test_flush_with_the_other_desks_partition_raises(
    sf: Factories, coat: str, idgen: IdGenerator
) -> None:
    with pytest.raises(DeskWallViolation, match="may not write"), sf["operator"].write() as s:
        s.add(_contact(idgen, Desk.ASSISTANT, "+971500000777"))
    with pytest.raises(DeskWallViolation), sf["assistant"].write() as s:
        s.add(_memory(idgen, Desk.OPERATOR, "episodic"))
    with pytest.raises(DeskWallViolation), sf["operator"].write() as s:
        s.execute(
            insert(ContactRow).values(
                id=idgen.new(),
                desk="assistant",
                coat_id=COAT,
                name="planted",
                channels={},
                primary_address="+971500000778",
                language="ar",
                register="formal",
                consent_status="opt_in",
                dnc_flag=False,
                source="inbound",
                audit_id=idgen.new(),
            )
        )
    for kind in ("operator", "assistant"):
        with sf[kind].session() as session:
            assert session.scalars(select(ContactRow)).all() == []


def test_rows_never_change_partition(sf: Factories, partitioned: dict[str, list[Ulid]]) -> None:
    with (
        pytest.raises(DeskWallViolation, match="never change partition"),
        sf["operator"].write() as s,
    ):
        s.execute(update(ContactRow).values(desk="assistant"))
    with sf["operator"].write() as session:
        contact = session.get(ContactRow, partitioned["operator"][0])
        assert contact is not None
        contact.desk = Desk.ASSISTANT
        with pytest.raises(DeskWallViolation):
            session.flush()
        session.rollback()
    with sf["assistant"].session() as session:
        assert sorted(c.id for c in session.scalars(select(ContactRow))) == sorted(
            partitioned["assistant"]
        )


def test_owner_profile_memory_is_assistant_only_by_check_and_by_partition(
    sf: Factories, coat: str, idgen: IdGenerator
) -> None:
    with pytest.raises(IntegrityError), sf["operator"].write() as session:
        session.add(_memory(idgen, Desk.OPERATOR, "owner_profile"))
    with sf["assistant"].write() as session:
        session.add(_memory(idgen, Desk.ASSISTANT, "owner_profile"))
    with sf["assistant"].session() as session:
        stores = session.scalars(select(MemoryRecordRow.store)).all()
        assert stores == ["owner_profile"]
    with sf["operator"].session() as session:
        assert session.scalars(select(MemoryRecordRow)).all() == []
        assert (
            session.scalar(
                select(func.count())
                .select_from(MemoryRecordRow)
                .where(MemoryRecordRow.store == "owner_profile")
            )
            == 0
        )


def test_other_desk_row_mappers_are_partitioned_too(
    sf: Factories, coat: str, idgen: IdGenerator, clock: FakeClock
) -> None:
    now = clock.now()
    with sf["assistant"].write() as session:
        session.add(
            ReadbackPendingRow(
                id=idgen.new(),
                desk=Desk.ASSISTANT,
                event_id=idgen.new(),
                proposal_json={},
                understood="Transfer 500 AED.",
                expires_at=now + timedelta(minutes=30),
            )
        )
        session.add(
            CategoryStateRow(
                id=idgen.new(),
                desk=Desk.ASSISTANT,
                coat_id=COAT,
                category="reply_email",
                tier="K",
                started_at=clock.today_dubai(),
                items=0,
                unedited=0,
                reduce_to_k=False,
            )
        )
        session.add(
            ModelTraceRow(
                id=idgen.new(),
                desk=Desk.ASSISTANT,
                event_id=idgen.new(),
                request_hash="sha256:r",
                response_json={},
                vendor="fake-a",
                model="fake-a-1",
                expires_at=now + timedelta(days=30),
            )
        )
        session.add(
            ConversationRow(
                id=idgen.new(),
                desk=Desk.ASSISTANT,
                coat_id=COAT,
                channel="owner_mailbox",
                thread_ref="thread-1",
                bucket="handles",
                state="open",
                audit_id=idgen.new(),
            )
        )
    for mapper in (ReadbackPendingRow, CategoryStateRow, ModelTraceRow, ConversationRow):
        with sf["assistant"].session() as session:
            assert len(session.scalars(select(mapper)).all()) == 1
        with sf["operator"].session() as session:
            assert session.scalars(select(mapper)).all() == [], mapper.__name__


# --------------------------------------------------------------------------- governance and auditor


def test_governance_row_is_readable_by_desks_and_written_by_governance_only(
    sf: Factories, rows: dict[str, dict[str, Any]]
) -> None:
    with sf["governance"].write() as session:
        session.add(OwnerRow(**rows["owner"]))
    for kind in ("operator", "assistant"):
        with sf[kind].session() as session:
            owner = session.scalars(select(OwnerRow)).one()
            assert owner.whatsapp_number == rows["owner"]["whatsapp_number"]
        with pytest.raises(DeskWallViolation, match="may not write"), sf[kind].write() as session:
            session.execute(update(OwnerRow).values(name="hijacked"))
        with pytest.raises(DeskWallViolation), sf[kind].write() as session:
            owner = session.scalars(select(OwnerRow)).one()
            owner.name = "hijacked"
            session.flush()
    with sf["auditor"].session() as session, pytest.raises(DeskWallViolation):
        session.execute(select(OwnerRow))
    with sf["governance"].session() as session:
        assert session.scalars(select(OwnerRow.name)).one() == "Owner"


def test_auditor_reads_shared_never_a_partition_and_appends_reports_only(
    sf: Factories,
    rows: dict[str, dict[str, Any]],
    partitioned: dict[str, list[Ulid]],
    idgen: IdGenerator,
) -> None:
    with sf["operator"].write() as session:
        session.add(AuditEventRow(**rows["audit_event"]))
    auditor = sf["auditor"]
    with auditor.session() as session:
        assert [row.action for row in session.scalars(select(AuditEventRow))] == ["ledger.spend"]
        with pytest.raises(DeskWallViolation):
            session.execute(select(ContactRow))
        with pytest.raises(DeskWallViolation):
            session.execute(select(MemoryRecordRow))
        with pytest.raises(DeskWallViolation):
            session.execute(select(DocumentRow))
    with pytest.raises(DeskWallViolation, match="read-only"), auditor.write():
        pass
    with auditor.append_report() as session:
        session.add(AuditorReportRow(**rows["auditor_report"]))
    with auditor.session() as session:
        assert session.scalars(select(AuditorReportRow.summary)).all() == ["Nothing unusual."]
    with pytest.raises(DeskWallViolation), auditor.append_report() as session:
        session.add(
            AuditEventRow(
                **{
                    **rows["audit_event"],
                    "id": idgen.new(),
                    "seq": 2,
                    "entry_hash": "hmac:2",
                    "invocation_id": idgen.new(),
                }
            )
        )
    with (
        pytest.raises((DeskWallViolation, AppendOnlyViolation)),
        auditor.append_report() as session,
    ):
        report = session.scalars(select(AuditorReportRow)).one()
        report.summary = "edited"
        session.flush()
    for kind in ("operator", "assistant", "governance"):
        with sf[kind].session() as session, pytest.raises(DeskWallViolation):
            session.execute(select(AuditorReportRow))


# --------------------------------------------------------------------------- fixer round 1: grants, partitions, hidden columns


def _inbox(
    idgen: IdGenerator, clock: FakeClock, desk: Desk, body: str, **extra: Any
) -> InboxEventRow:
    """An inbox row as a desk publishes it: no authenticity columns unless ``extra`` sets them."""
    return InboxEventRow(
        id=idgen.new(),
        desk=desk,
        coat_id=COAT,
        source_kind="handoff",
        channel="owner_whatsapp",
        line_id="owner",
        sender="+971500000001",
        origin="text",
        body=body,
        payload={},
        provider_msg_id=f"pm-{idgen.new()}",
        priority=0,
        received_at=clock.now(),
        **extra,
    )


def test_message_task_and_skill_rows_are_partitioned_too(
    sf: Factories, coat: str, idgen: IdGenerator, clock: FakeClock
) -> None:
    """The DESK_ROW mappers the other partition tests do not touch: a message (behind its
    conversation), a task and a skill written by the Assistant are invisible to the Operator."""
    with sf["assistant"].write() as session:
        conversation = ConversationRow(
            id=idgen.new(),
            desk=Desk.ASSISTANT,
            coat_id=COAT,
            channel="owner_mailbox",
            thread_ref="thread-9",
            bucket="handles",
            state="open",
            audit_id=idgen.new(),
        )
        session.add(conversation)
        session.flush()
        session.add(
            MessageRow(
                id=idgen.new(),
                desk=Desk.ASSISTANT,
                coat_id=COAT,
                conversation_id=conversation.id,
                direction="in",
                channel="owner_mailbox",
                body_ref="objects/msg/9",
                body_text="the owner's mail",
                language="en",
                sent_by="owner",
                provider_msg_id="mail-9",
                audit_id=idgen.new(),
            )
        )
        session.add(
            TaskRow(
                id=idgen.new(),
                desk=Desk.ASSISTANT,
                coat_id=COAT,
                title="Renew the licence",
                owner_type="nour",
                status="open",
                source="renewal",
                audit_id=idgen.new(),
            )
        )
        session.add(
            SkillRow(
                id=idgen.new(),
                desk=Desk.ASSISTANT,
                name="renewals",
                version=1,
                trigger="licence expiring",
                steps_ref="skills/renewals.md",
                inputs={},
                failure_signs=[],
            )
        )
    for mapper in (MessageRow, TaskRow, SkillRow):
        with sf["assistant"].session() as session:
            assert len(session.scalars(select(mapper)).all()) == 1, mapper.__name__
        with sf["operator"].session() as session:
            assert session.scalars(select(mapper)).all() == [], mapper.__name__
            assert session.scalar(select(func.count()).select_from(mapper)) == 0
    with sf["operator"].session() as session:  # the body never crosses, even by column
        assert session.scalars(select(MessageRow.body_text)).all() == []
    # UNIQUE(name, version) is global across desks (DESIGN §5.1): the collision is the one
    # thing an Operator learns about an Assistant skill (SkillRow docstring)
    with pytest.raises(IntegrityError, match="UNIQUE constraint failed: skill.name"):
        with sf["operator"].write() as session:
            session.add(
                SkillRow(
                    id=idgen.new(),
                    desk=Desk.OPERATOR,
                    name="renewals",
                    version=1,
                    trigger="x",
                    steps_ref="skills/x.md",
                    inputs={},
                    failure_signs=[],
                )
            )


def test_card_mirror_is_governance_only(sf: Factories, rows: dict[str, dict[str, Any]]) -> None:
    """DESIGN §5.2 card: "mutable (governance)" — a desk reads its card and can neither
    unfreeze the mirror nor raise the mirrored cap."""
    with sf["governance"].write() as session:
        session.add(CardRow(**{**rows["card"], "frozen": True}))
    for kind in ("operator", "assistant"):
        with sf[kind].session() as session:
            card = session.scalars(select(CardRow)).one()
            assert card.card_ref == "card_1" and card.frozen is True
        with pytest.raises(DeskWallViolation, match="may not write"), sf[kind].write() as s:
            s.execute(update(CardRow).values(frozen=False, monthly_cap=10**12))
        with pytest.raises(DeskWallViolation, match="may not write"), sf[kind].write() as s:
            card = s.scalars(select(CardRow)).one()
            card.frozen = False
            s.flush()
    with sf["governance"].write() as session:  # the kill switch thaws
        session.execute(update(CardRow).values(frozen=False))
    with sf["operator"].session() as session:
        card = session.scalars(select(CardRow)).one()
        assert card.frozen is False and card.monthly_cap == rows["card"]["monthly_cap"]
    with sf["auditor"].session() as session, pytest.raises(DeskWallViolation):
        session.execute(select(CardRow))


def test_owner_secrets_never_reach_a_desk_session(
    sf: Factories, rows: dict[str, dict[str, Any]]
) -> None:
    """THREAT_REVIEW 6.3: ``passphrase_hash`` / ``passphrase_fp`` are deferred and hidden —
    a desk's default load never carries them, and every way of asking for them is refused
    (the mirror of the Postgres column grant); governance loads them explicitly."""
    with sf["governance"].write() as session:
        session.add(OwnerRow(**rows["owner"]))
    for kind in ("operator", "assistant"):
        with sf[kind].session() as session:
            owner = session.scalars(select(OwnerRow)).one()
            assert owner.whatsapp_number == rows["owner"]["whatsapp_number"]
            assert "passphrase_hash" not in owner.__dict__ and "passphrase_fp" not in owner.__dict__
            with pytest.raises(DeskWallViolation, match="hidden column"):
                _ = owner.passphrase_hash  # the deferred load is a statement the guard sees
            with pytest.raises(DeskWallViolation, match="hidden column"):
                _ = owner.passphrase_fp
            with pytest.raises(DeskWallViolation, match="hidden column"):
                session.refresh(owner, ["passphrase_hash"])
            got = session.get(OwnerRow, rows["owner"]["id"])
            assert got is not None and "passphrase_fp" not in got.__dict__
        for statement in (
            select(OwnerRow).options(undefer(OwnerRow.passphrase_hash)),
            select(OwnerRow).options(undefer(OwnerRow.passphrase_fp)),
            select(OwnerRow).options(undefer("*")),
            select(OwnerRow).options(load_only(OwnerRow.passphrase_fp)),
            select(OwnerRow.passphrase_hash),
            select(OwnerRow.whatsapp_number, OwnerRow.passphrase_fp),
        ):
            with sf[kind].session() as session, pytest.raises(DeskWallViolation, match="hidden"):
                session.execute(statement)
        with sf[kind].session() as session, pytest.raises(DeskWallViolation, match="hidden"):
            session.get(OwnerRow, rows["owner"]["id"], options=[undefer(OwnerRow.passphrase_hash)])
        with sf[kind].session() as session:  # the readable columns stay readable
            assert session.scalars(select(OwnerRow.whatsapp_number)).one() == NUMBER
            narrowed = session.scalars(select(OwnerRow).options(load_only(OwnerRow.name))).one()
            assert narrowed.name == "Owner"
    with sf["governance"].session() as session:
        owner = session.scalars(select(OwnerRow).options(undefer(OwnerRow.passphrase_hash))).one()
        assert owner.passphrase_hash == rows["owner"]["passphrase_hash"]
        assert owner.passphrase_fp == rows["owner"]["passphrase_fp"]  # a deferred load, allowed


def test_inbox_events_are_partitioned_per_desk_for_desk_tokens(
    sf: Factories, coat: str, idgen: IdGenerator, clock: FakeClock
) -> None:
    """SPEC §5 / DESIGN §4a: the Operator never reads an owner-thread or owner-mailbox body the
    Assistant desk received. Ingress (governance) publishes into both partitions; each desk
    reads, acks and parks its own; a desk may publish for the other desk but not read it back,
    nor move a row across; governance and the auditor see everything."""
    now = clock.now()
    with sf["governance"].write() as session:
        session.add(_inbox(idgen, clock, Desk.ASSISTANT, "owner secret", signature_valid=True))
        session.add(_inbox(idgen, clock, Desk.OPERATOR, "stranger says hi"))
        session.flush()
        assistant_id = session.scalars(
            select(InboxEventRow.id).where(InboxEventRow.desk == Desk.ASSISTANT)
        ).one()
    with sf["operator"].session() as session:
        bodies = session.scalars(select(InboxEventRow.body)).all()
        assert bodies == ["stranger says hi"]
        assert session.scalar(select(func.count()).select_from(InboxEventRow)) == 1
        assert (
            session.scalars(select(InboxEventRow).where(InboxEventRow.desk == Desk.ASSISTANT)).all()
            == []
        )
        assert session.get(InboxEventRow, assistant_id) is None
    with sf["operator"].write() as session:  # acking the other desk's event touches nothing
        result = cast(
            Any,
            session.execute(
                update(InboxEventRow).values(acked_at=now).where(InboxEventRow.id == assistant_id)
            ),
        )
        assert result.rowcount == 0
    with sf["operator"].write() as session:  # its own event: ack / park / lease are its verbs
        session.execute(update(InboxEventRow).values(acked_at=now, lock_until=None))
    with sf["operator"].write() as session:  # a desk may publish an event for the other desk
        session.add(_inbox(idgen, clock, Desk.ASSISTANT, "handoff for the assistant"))
    with sf["operator"].session() as session:  # ... and still cannot read it back
        assert session.scalars(select(InboxEventRow.body)).all() == ["stranger says hi"]
    with pytest.raises(DeskWallViolation), sf["operator"].write() as session:
        session.execute(update(InboxEventRow).values(desk="assistant"))
    with pytest.raises(DeskWallViolation), sf["operator"].write() as session:
        row = session.scalars(select(InboxEventRow)).one()
        row.desk = Desk.ASSISTANT
        session.flush()
    with sf["assistant"].session() as session:
        assert set(session.scalars(select(InboxEventRow.body)).all()) == {
            "handoff for the assistant",
            "owner secret",
        }
        assert session.get(InboxEventRow, assistant_id) is not None
    with sf["governance"].session() as session:
        assert session.scalar(select(func.count()).select_from(InboxEventRow)) == 3
        acked = session.scalars(
            select(InboxEventRow.acked_at).where(InboxEventRow.desk == Desk.OPERATOR)
        ).one()
        assert acked == now
    with sf["auditor"].session() as session:  # SHARED read for the auditor, every partition
        assert session.scalar(select(func.count()).select_from(InboxEventRow)) == 3


def _owner_message(idgen: IdGenerator, desk: Desk, text: str, kind: str) -> PendingOwnerMessageRow:
    return PendingOwnerMessageRow(
        id=idgen.new(), desk=desk, kind=kind, text=text, emergency=False, audit_id=idgen.new()
    )


def test_pending_owner_messages_are_partitioned_per_desk_for_desk_tokens(
    sf: Factories, idgen: IdGenerator, clock: FakeClock
) -> None:
    """SPEC §5 / DESIGN §4a (fixer round 2): the Assistant's drafts for the owner — a brief
    built from his mailbox and calendar, a quoted instruction, a read-back — never reach the
    Operator token. Each desk writes, reads and transitions (``sent_at`` / ``provider_msg_id``)
    its own partition; a desk may publish a row for the other desk but not read it back, nor
    move a row across; governance and the auditor see every row; nobody deletes."""
    now = clock.now()
    with sf["assistant"].write() as session:
        assistant_row = _owner_message(
            idgen, Desk.ASSISTANT, "Your lawyer wrote: the villa lease is due Thursday", "brief"
        )
        session.add(assistant_row)
        session.flush()
        assistant_id = assistant_row.id
    with sf["operator"].write() as session:
        session.add(_owner_message(idgen, Desk.OPERATOR, "Buzz Avenue: two N spends", "notify"))
    with sf["operator"].session() as session:
        assert session.scalars(select(PendingOwnerMessageRow.text)).all() == [
            "Buzz Avenue: two N spends"
        ]
        assert session.scalar(select(func.count()).select_from(PendingOwnerMessageRow)) == 1
        assert (
            session.scalars(
                select(PendingOwnerMessageRow).where(PendingOwnerMessageRow.desk == Desk.ASSISTANT)
            ).all()
            == []
        )
        assert session.get(PendingOwnerMessageRow, assistant_id) is None
    with sf["operator"].write() as session:  # sending the other desk's message touches nothing
        result = cast(
            Any,
            session.execute(
                update(PendingOwnerMessageRow)
                .values(sent_at=now, provider_msg_id="wamid.op")
                .where(PendingOwnerMessageRow.id == assistant_id)
            ),
        )
        assert result.rowcount == 0
    with sf["operator"].write() as session:  # its own row: its one send
        row = session.scalars(select(PendingOwnerMessageRow)).one()
        row.sent_at = now
        row.provider_msg_id = "wamid.op"
    with pytest.raises(SingleTransitionViolation), sf["operator"].write() as session:
        row = session.scalars(select(PendingOwnerMessageRow)).one()
        row.provider_msg_id = "again"
        session.flush()
    with pytest.raises(DeskWallViolation), sf["operator"].write() as session:
        session.execute(update(PendingOwnerMessageRow).values(desk="assistant"))
    with pytest.raises(DeskWallViolation), sf["operator"].write() as session:
        row = session.scalars(select(PendingOwnerMessageRow)).one()
        row.desk = Desk.ASSISTANT
        session.flush()
    with sf["operator"].write() as session:  # a desk may publish a row for the other desk ...
        session.add(_owner_message(idgen, Desk.ASSISTANT, "published by the operator", "notify"))
    with sf["operator"].session() as session:  # ... and still cannot read it back
        assert session.scalars(select(PendingOwnerMessageRow.text)).all() == [
            "Buzz Avenue: two N spends"
        ]
    with sf["assistant"].session() as session:
        assert set(session.scalars(select(PendingOwnerMessageRow.text)).all()) == {
            "Your lawyer wrote: the villa lease is due Thursday",
            "published by the operator",
        }
        parked = session.get(PendingOwnerMessageRow, assistant_id)
        assert parked is not None and parked.sent_at is None and parked.provider_msg_id is None
    with sf["assistant"].write() as session:  # the Assistant sends its own
        session.execute(
            update(PendingOwnerMessageRow)
            .values(sent_at=now, provider_msg_id="wamid.as")
            .where(PendingOwnerMessageRow.id == assistant_id)
        )
    with sf["governance"].session() as session:
        assert session.scalar(select(func.count()).select_from(PendingOwnerMessageRow)) == 3
        sent = dict(
            session.execute(
                select(PendingOwnerMessageRow.text, PendingOwnerMessageRow.provider_msg_id)
            ).all()
        )
        assert sent == {
            "Your lawyer wrote: the villa lease is due Thursday": "wamid.as",
            "Buzz Avenue: two N spends": "wamid.op",
            "published by the operator": None,
        }
    with sf["auditor"].session() as session:  # SHARED read for the auditor, every partition
        assert session.scalar(select(func.count()).select_from(PendingOwnerMessageRow)) == 3
    for kind in ("operator", "assistant", "governance"):  # kept rows (insert + one send)
        with pytest.raises(AppendOnlyViolation, match="never deleted"), sf[kind].write() as s:
            s.execute(delete(PendingOwnerMessageRow))


def test_desks_cannot_rewrite_inbox_authenticity_or_delete_events(
    sf: Factories, coat: str, idgen: IdGenerator, clock: FakeClock
) -> None:
    """The grants of DESIGN §5.2 on SQLite: a desk acks, parks and leases; it never rewrites
    ``sender`` / ``signature_valid`` / ``passphrase_attempt`` / ``body`` (the ingress-recorded
    authenticity, DESIGN §4d), never inserts an event *carrying* that authenticity, and never
    deletes; the scheduler (governance) deletes for retention."""
    with sf["governance"].write() as session:
        session.add(_inbox(idgen, clock, Desk.OPERATOR, "hello"))
    forbidden = {  # every value differs from the stored row: the same value is not a change
        "passphrase_attempt": "ok",
        "signature_valid": True,
        "sender": "+971500000099",
        "body": "pay the attacker",
        "attempt_id": idgen.new(),
    }
    for column, value in forbidden.items():
        # frozen at INSERT for everyone (``__immutable__``), checked before the grant
        with (
            pytest.raises(SingleTransitionViolation, match="immutable"),
            sf["operator"].write() as s,
        ):
            row = s.scalars(select(InboxEventRow)).one()
            setattr(row, column, value)
            s.flush()
        with (
            pytest.raises(SingleTransitionViolation, match="immutable"),
            sf["operator"].write() as s,
        ):
            s.execute(update(InboxEventRow).values({column: value}))
        with (
            pytest.raises(SingleTransitionViolation, match="immutable"),
            sf["governance"].write() as s,
        ):
            s.execute(update(InboxEventRow).values({column: value}))  # governance neither
    # a column the grant withholds that is not frozen: updated_at is the ORM's, so there is none
    # on inbox_event besides the ack columns — the grant is observable through those being allowed
    with pytest.raises(DeskWallViolation, match="may not DELETE"), sf["operator"].write() as s:
        s.execute(delete(InboxEventRow))
    with pytest.raises(DeskWallViolation, match="may not DELETE"), sf["operator"].write() as s:
        row = s.scalars(select(InboxEventRow)).one()
        s.delete(row)
        s.flush()
    for extra in (
        {"signature_valid": True},
        {"passphrase_attempt": "ok"},
        {"attempt_id": idgen.new()},
    ):
        with pytest.raises(DeskWallViolation, match="grant"), sf["operator"].write() as session:
            session.add(_inbox(idgen, clock, Desk.OPERATOR, "planted", **extra))
        with pytest.raises(DeskWallViolation, match="grant"), sf["operator"].write() as session:
            session.execute(
                insert(InboxEventRow).values(
                    id=idgen.new(),
                    desk="operator",
                    coat_id=COAT,
                    source_kind="handoff",
                    channel="owner_whatsapp",
                    sender="+971500000001",
                    origin="text",
                    body="planted",
                    payload={},
                    priority=0,
                    received_at=clock.now(),
                    **extra,
                )
            )
    with sf["operator"].write() as session:  # a desk-published event takes the defaults
        published = _inbox(idgen, clock, Desk.OPERATOR, "published by the desk")
        session.add(published)
        session.flush()
        published_id = published.id
    with sf["governance"].session() as session:
        published_row = session.get(InboxEventRow, published_id)
        assert published_row is not None and published_row.signature_valid is False
        assert published_row.passphrase_attempt is None and published_row.attempt_id is None
    with sf["operator"].session() as session:
        assert session.scalars(select(InboxEventRow.signature_valid)).all() == [False, False]
    with sf["operator"].write() as session:
        session.execute(update(InboxEventRow).values(acked_at=clock.now(), error=None))
    with sf["governance"].write() as session:  # retention: the scheduler may delete
        session.execute(delete(InboxEventRow).where(InboxEventRow.id == published_id))
    with sf["governance"].session() as session:
        assert session.scalar(select(func.count()).select_from(InboxEventRow)) == 1


def test_handoff_gate_grants(
    sf: Factories, coat: str, idgen: IdGenerator, clock: FakeClock
) -> None:
    """DESIGN §5.2 handoff: insert (Assistant) + one take (Operator), nothing else, by grant."""

    def handoff(desk_note: str) -> HandoffRow:
        return HandoffRow(
            id=idgen.new(),
            coat_id=COAT,
            handoff_json={"title": desk_note},
            source_event_id=idgen.new(),
            pushed_by="assistant",
        )

    for kind in ("operator", "governance"):
        with pytest.raises(DeskWallViolation, match="may not INSERT"), sf[kind].write() as s:
            s.add(handoff(f"authored by {kind}"))
    with sf["assistant"].write() as session:
        session.add(handoff("Call Ahmed"))
    with pytest.raises(DeskWallViolation, match="grant"), sf["assistant"].write() as session:
        session.execute(update(HandoffRow).values(taken_at=clock.now()))
    for kind in ("assistant", "operator", "governance"):  # the body is frozen for everyone
        with pytest.raises(SingleTransitionViolation, match="immutable"), sf[kind].write() as s:
            row = s.scalars(select(HandoffRow)).one()
            row.handoff_json = {"title": "rewritten"}
            s.flush()
    with sf["operator"].write() as session:  # the one take
        row = session.scalars(select(HandoffRow)).one()
        row.taken_at = clock.now()
    with pytest.raises(SingleTransitionViolation), sf["operator"].write() as session:
        row = session.scalars(select(HandoffRow)).one()
        row.taken_at = clock.now() + timedelta(minutes=1)
        session.flush()
    for kind in ("operator", "assistant", "governance"):
        with pytest.raises(AppendOnlyViolation, match="never deleted"), sf[kind].write() as s:
            s.execute(delete(HandoffRow))
    with sf["governance"].session() as session:
        row = session.scalars(select(HandoffRow)).one()
        assert row.handoff_json == {"title": "Call Ahmed"} and row.taken_at is not None


def test_dnc_freeze_coat_and_passphrase_attempt_grants(
    sf: Factories, coat: str, rows: dict[str, dict[str, Any]], idgen: IdGenerator, clock: FakeClock
) -> None:
    """DESIGN §5: desks insert opt-outs and freezes but never change, release or delete them;
    coats are governance-written; only ingress records a passphrase attempt."""
    with sf["operator"].write() as session:
        session.add(DncEntryRow(**rows["dnc_entry"]))
        session.add(FreezeStateRow(**rows["freeze_state"]))
    with pytest.raises(DeskWallViolation, match="grant"), sf["operator"].write() as session:
        session.execute(update(DncEntryRow).values(address="+971500000000"))
    with pytest.raises(DeskWallViolation, match="may not DELETE"), sf["assistant"].write() as s:
        s.execute(delete(DncEntryRow))
    with pytest.raises(DeskWallViolation, match="grant"), sf["operator"].write() as session:
        session.execute(update(FreezeStateRow).values(released_at=clock.now()))
    with pytest.raises(DeskWallViolation, match="grant"), sf["assistant"].write() as session:
        row = session.scalars(select(FreezeStateRow)).one()
        row.released_at = clock.now()
        session.flush()
    with pytest.raises(DeskWallViolation, match="grant"), sf["assistant"].write() as session:
        session.execute(update(CoatRow).values(banking_ref="vault://evil"))
    with pytest.raises(DeskWallViolation, match="grant"), sf["operator"].write() as session:
        coat_row = session.scalars(select(CoatRow)).one()
        coat_row.approval_rules = {}
        session.flush()
    with pytest.raises(DeskWallViolation, match="may not INSERT"), sf["operator"].write() as s:
        s.add(PassphraseAttemptRow(**rows["passphrase_attempt"]))
    with sf["governance"].write() as session:  # the owner CLI, the kill switch, ingress
        session.execute(update(DncEntryRow).values(reason="owner lifted"))
        session.execute(update(FreezeStateRow).values(released_at=clock.now()))
        session.execute(update(CoatRow).values(config_hash="sha256:1"))
        session.add(PassphraseAttemptRow(**rows["passphrase_attempt"]))
    with sf["operator"].session() as session:
        assert session.scalars(select(DncEntryRow.reason)).one() == "owner lifted"
        assert session.scalars(select(FreezeStateRow.released_at)).one() is not None
        assert session.scalars(select(CoatRow.banking_ref)).one() == rows["coat"]["banking_ref"]
        assert session.scalars(select(PassphraseAttemptRow.outcome)).all() == ["ok"]
