"""The self-critic (DESIGN §3.10 ``nour/language/critic.py``; SPEC §8 §4 Check step).

SPEC §8: "Self-critic: a second model pass scores every outbound draft for tone, claims,
compliance and register before it is sent or queued." SPEC §4 (Check): the critic runs inside
``ActionGate.dispatch`` before an outbound draft executes; ``CriticScore.passed=False`` blocks
the send (DESIGN §2.3). The verdict is advisory to nothing: a failing critic is a refusal.

Two layers, in this order:

1. **Deterministic walls that need no model.** A draft carrying an IBAN / card / passport shape
   (``shape_hits``) fails hard-fail H8 — Nour never types bank details into text
   (docs/THREAT_REVIEW.md top-10 item 3). For every register but the owner's, a draft in which
   the :class:`InjectionScanner` finds text that no message to a third party may contain fails
   H9 without a model call: an authority claim, an auditor impersonation, an autonomy change, a
   logging change, a credentials request, a bank-change announcement
   (:data:`FORBIDDEN_IN_DRAFT`), or a payment directed to a *new* / *different* account
   (:data:`_NEW_ACCOUNT_RE`, narrower than the scanner's ``pay_to_new_account``, which rightly
   also flags "transfer the balance to our account on the invoice" when a stranger writes it but
   is ordinary invoice-chasing when Nour does). A report to the owner is exempt from H9's wall:
   SPEC §12 says a found instruction or a "we changed our bank" message is *quoted to the
   owner*, so an owner-facing draft that says "they wrote that they changed their bank account;
   I did nothing" is correct text and the model pass judges it with the observed content in
   hand (owner-facing money relays are in any case code-rendered by the brief, THREAT_REVIEW
   2.1). Relaying an owner decision to a customer ("the owner has approved the discount") is
   Nour's job, so the scanner's ``impersonate_owner`` is never a wall.
2. **The model pass** on the CRITIC role: the system prompt (``prompts/critic.system.md``) is
   rendered once per coat — coat, mandate, tone guide and knowledge pack, the only source of
   allowed claims — and is byte-identical across drafts (a prompt-cache prefix,
   docs/adapters/models.md §2.3); the per-draft inputs travel in the user message inside
   ``<draft authority="data">``, ``<counterpart>``, ``<observed … authority="data">`` and
   ``<flags …/>`` fences. The model returns the JSON of the prompt's output contract; the four
   0–10 scores become 0–1 floats, ``passed`` is true only for verdict ``pass`` with no hard
   fails, every score at or above ``threshold`` and the mean at or above ``overall_min`` (the
   prompt's "all four at least 7 and overall at least 7.5"). Unparseable output fails closed.

Everything the critic writes back (``CriticScore.notes``) is a ``SafeStr`` and is Nour-authored
text bound for the audit row: a note that would carry a registered Tier 2 value *or* a
secret-shaped value (the model is the adversary, SPEC §13) is withheld and the draft fails; a
hard-fail ``code`` that is not ``H<n>`` is dropped as malformed rather than copied into the note.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from nour.config.schema import CoatConfig
from nour.core.contracts import CriticScore, ObservedText
from nour.core.errors import Tier2LeakError
from nour.core.leakguard import LeakGuard, blocking_shape_hits
from nour.core.ports import ModelMessage, ModelPort, ModelRequest, ModelResponse, ModelRole
from nour.core.types import Desk, SafeStr
from nour.language.injection import normalise_for_scan
from nour.language.prompt import PromptAssembler, _attr, fence_safe, observed_fence, render_mandate

SCORE_DIMENSIONS: tuple[str, ...] = ("tone", "claims", "compliance", "register")
"""The four scored dimensions of prompts/critic.system.md, in the order ``CriticScore`` holds them."""

REGISTERS: frozenset[str] = frozenset(
    {"owner", "uae_arabic", "uae_english", "government_msa", "international", "staff"}
)
"""The closed set of ``counterpart_register`` values (the register map of prompts/critic.system.md)."""

OWNER_REGISTER = "owner"

FORBIDDEN_IN_DRAFT: frozenset[str] = frozenset(
    {
        "auth_claim",
        "impersonate_auditor",
        "change_autonomy",
        "disable_logging",
        "forward_credentials",
        "bank_change",
    }
)
"""Scanner patterns that may never appear in a draft to anyone but the owner (hard fail H9
without a model call). ``pay_to_new_account`` is covered by the narrower :data:`_NEW_ACCOUNT_RE`;
``ignore_owner``, ``impersonate_owner`` and ``send_document`` have legitimate readings in Nour's
own text ("this needs the owner's approval", "the owner has approved", "the licence is
attached" after a K approval) and are left to the model pass."""

NOT_PROVIDED = "(not provided)"
"""What a server-side flag the caller did not pass reads as in the user message."""

NOTE_MAX_CHARS = 240
OVERALL_MIN = 0.75
_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")
_HARD_FAIL_CODE_RE = re.compile(r"H\d{1,2}")
_SCORE_MAX = 10.0
_NEW_ACCOUNT_RE = re.compile(
    r"\b(?:pay|pays|transfer|remit|send|move|wire|route|redirect|settle)\b.{0,80}?"
    r"\b(?:new|updated|different|changed|alternative|another|replacement)\s+"
    r"(?:bank\s+)?(?:account|iban|beneficiary|bank details|banking details)\b"
    r"|الحساب الجديد|حسابنا الجديد|حسابي الجديد|ايبان جديد|الايبان الجديد|iban جديد"
    r"|\b(?:account|7seb|7esab|iban) (?:el )?jdid\b"
)
"""A payment directed to a new / different account, in a draft (runs on ``normalise_for_scan``)."""


def _clamp(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return max(0.0, min(number / _SCORE_MAX, 1.0))


def parse_verdict(text: str | None) -> dict[str, Any] | None:
    """The first JSON object in the model's text, or ``None`` when there is none."""
    if not text:
        return None
    match = _JSON_BLOCK_RE.search(text)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _flag(value: bool | str | None) -> str:
    if value is None:
        return NOT_PROVIDED
    if isinstance(value, bool):
        return "true" if value else "false"
    return _attr(value) or NOT_PROVIDED


class Critic:
    """§8 self-critic: second model pass over every outbound draft; `passed=False` blocks execution in the gate."""

    def __init__(
        self,
        model: ModelPort,
        prompts: PromptAssembler,
        guard: LeakGuard,
        threshold: float = 0.7,
        overall_min: float = OVERALL_MIN,
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("Critic.threshold is a fraction between 0 and 1")
        if not 0.0 <= overall_min <= 1.0:
            raise ValueError("Critic.overall_min is a fraction between 0 and 1")
        self._model = model
        self._prompts = prompts
        self._guard = guard
        self._threshold = threshold
        self._overall_min = overall_min

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def overall_min(self) -> float:
        return self._overall_min

    def score(
        self,
        draft: SafeStr,
        coat: CoatConfig,
        counterpart_register: str,
        *,
        tier: str | None = None,
        approval_id: str | None = None,
        observed: Sequence[ObservedText] = (),
        disclosure_requested: bool | None = None,
        owner_name_send: bool | None = None,
        counterpart: str | None = None,
    ) -> CriticScore:
        """Score ``draft`` sent under ``coat`` to a counterpart of ``counterpart_register``
        (``owner`` / ``uae_arabic`` / ``uae_english`` / ``government_msa`` / ``international`` /
        ``staff``; anything else is a ``ValueError``). Deterministic walls first, then the model;
        fails closed on anything it cannot read.

        The keyword-only inputs are the server-side facts the gate knows and the model needs
        for H5–H7, H9 and H10: the declared ``tier`` (``A``/``N``/``K``), the ``approval_id`` of
        a released K item, the ``observed`` texts the draft answers (redacted and fenced as
        data), the ``disclosure_requested`` / ``owner_name_send`` flags and the ``counterpart``
        record as one line. An input left ``None`` is rendered as ``(not provided)``, never
        invented: the critic then judges on the draft's own wording and names the gap.
        """
        if not isinstance(draft, SafeStr):
            raise TypeError("Critic.score takes a SafeStr draft (LeakGuard-minted)")
        if not isinstance(coat, CoatConfig):
            raise TypeError("Critic.score takes a CoatConfig")
        if counterpart_register not in REGISTERS:
            raise ValueError(
                f"counterpart_register must be one of {sorted(REGISTERS)}, "
                f"got {counterpart_register!r}"
            )
        text = str(draft)
        if not text.strip():
            return self._fail("H11: empty draft.")

        shapes = blocking_shape_hits(text)
        if shapes:
            kinds = ", ".join(sorted({hit.kind for hit in shapes}))
            return self._fail(f"H8: secret-shaped value in the draft ({kinds}).")
        if counterpart_register != OWNER_REGISTER:
            forbidden = self._forbidden_in(text)
            if forbidden:
                return self._fail(
                    f"H9: instruction-shaped text in the draft ({', '.join(forbidden)})."
                )

        request = self._request(
            draft,
            coat,
            counterpart_register,
            tier=tier,
            approval_id=approval_id,
            observed=observed,
            disclosure_requested=disclosure_requested,
            owner_name_send=owner_name_send,
            counterpart=counterpart,
        )
        return self._interpret(self._model.complete(request))

    # ----- the deterministic H9 wall

    def _forbidden_in(self, text: str) -> list[str]:
        hits = self._prompts.scanner.scan(ObservedText(text=text, source="draft"))
        names = {hit.pattern for hit in hits} & FORBIDDEN_IN_DRAFT
        if _NEW_ACCOUNT_RE.search(normalise_for_scan(text)):
            names.add("pay_to_new_account")
        return sorted(names)

    # ----- the model pass

    def _request(
        self,
        draft: SafeStr,
        coat: CoatConfig,
        register: str,
        *,
        tier: str | None,
        approval_id: str | None,
        observed: Sequence[ObservedText],
        disclosure_requested: bool | None,
        owner_name_send: bool | None,
        counterpart: str | None,
    ) -> ModelRequest:
        system = self._prompts.critic_system(
            coat=f"{coat.name} ({coat.identity.title}); activities: {', '.join(coat.allowed_activities)}",
            mandate=render_mandate(coat.mandate),
            tone_guide=coat.tone_guide,
            knowledge_pack=coat.knowledge_pack,
        )
        record = fence_safe(" ".join(str(counterpart).split())) if counterpart else NOT_PROVIDED
        lines: list[str] = [
            f"Score the outbound draft below for the counterpart register '{register}' under the "
            f"{coat.name} coat. Return only the JSON object of the output contract.",
            f'<counterpart register="{_attr(register)}">{record}</counterpart>',
            f'<flags disclosure_requested="{_flag(disclosure_requested)}" '
            f'approval_id="{_flag(approval_id)}" tier="{_flag(tier)}" '
            f'owner_name_send="{_flag(owner_name_send)}" />',
            '<draft authority="data">',
            fence_safe(str(draft)),
            "</draft>",
        ]
        for item in observed:
            if not isinstance(item, ObservedText):
                raise TypeError("Critic.score observed items must be ObservedText")
            lines.extend(observed_fence(self._guard, item)[0])
        user = self._guard.safe("\n".join(lines))
        return ModelRequest(
            role=ModelRole.CRITIC,
            desk=Desk.GOVERNANCE,
            system=system,
            messages=[ModelMessage(role="user", content=user)],
            tools=[],
            max_tokens=1024,
            metadata={"coat": coat.slug, "register": register, "purpose": "critic"},
        )

    def _interpret(self, response: ModelResponse) -> CriticScore:
        data = parse_verdict(response.text)
        if data is None:
            return self._fail(
                "critic output was not the JSON of the output contract; failing closed."
            )
        scores_raw = data.get("scores")
        scores = scores_raw if isinstance(scores_raw, dict) else {}
        values = {name: _clamp(scores.get(name)) for name in SCORE_DIMENSIONS}
        overall = sum(values.values()) / len(values)
        hard_fails_raw = data.get("hard_fails")
        hard_fails = hard_fails_raw if isinstance(hard_fails_raw, list) else []
        verdict = str(data.get("verdict", "")).strip().lower()
        passed = (
            verdict == "pass"
            and not hard_fails
            and all(value >= self._threshold for value in values.values())
            and overall >= self._overall_min
        )
        codes: list[str] = []
        malformed = 0
        for item in hard_fails:
            raw = item.get("code", "") if isinstance(item, dict) else item
            code = str(raw).strip().upper()
            if _HARD_FAIL_CODE_RE.fullmatch(code):
                codes.append(code)
            else:
                malformed += 1
        note = str(data.get("critic_note") or "").strip()
        summary = f"verdict={verdict or 'none'}"
        if codes:
            summary += f" hard_fails={', '.join(codes)}"
        if malformed:
            summary += f" malformed_hard_fails={malformed}"
        if note:
            summary += f" note={note}"
        notes, withheld = self._note(summary)
        return CriticScore(
            tone=values["tone"],
            claims=values["claims"],
            compliance=values["compliance"],
            register=values["register"],
            passed=passed and not withheld,
            notes=notes,
        )

    # ----- helpers

    def _fail(self, note: str) -> CriticScore:
        notes, _ = self._note(note)
        return CriticScore(
            tone=0.0, claims=0.0, compliance=0.0, register=0.0, passed=False, notes=notes
        )

    def _note(self, text: str) -> tuple[SafeStr, bool]:
        """One audit-ready line, scrubbed; ``(note, withheld)`` where ``withheld`` is true when
        the model's note carried a secret-shaped value (IBAN / card / passport shape) or a
        registered Tier 2 value — the draft then fails, the value never reaches the audit row."""
        one_line = " ".join(text.split())
        if len(one_line) > NOTE_MAX_CHARS:
            one_line = one_line[: NOTE_MAX_CHARS - 1].rstrip() + "…"
        if blocking_shape_hits(one_line):
            return self._guard.safe("critic note withheld: it carried a secret-shaped value."), True
        try:
            return self._guard.safe(one_line), False
        except Tier2LeakError:
            return (
                self._guard.safe("critic note withheld: it carried a registered Tier 2 value."),
                True,
            )
