"""``ScriptedModel`` and ``FakePrivateModel``: the model vendors (DESIGN §2.4 §3.9 §6; SPEC §4 §12 §14 §16).

``ModelPort.complete(ModelRequest) -> ModelResponse`` is the only model API (DESIGN §2.4). A model
call is not a side-effecting port method: it takes no ``PortCall`` and writes nothing to the
shared ``CallLog`` (the audit span around the *action* is what the log sees). The model fakes
join the shared ``Clock`` and the ``fail_next`` machinery of ``FakePort``, nothing else.
``ScriptedModel`` answers from a FIFO of enqueued responses first and from its ``ModelPolicy``
otherwise, and records *every* request (outages included) in ``requests`` so the harness can
scan every prompt that was ever rendered for the passphrase and the canary (``prompt_captures``,
DESIGN §7.1). Every answer is re-stamped with this port's vendor and model and carries a
deterministic token-usage estimate, so the per-desk model budget (SPEC §10 "a separate budget
line for external AI models"; THREAT_REVIEW top-10 #7) can be asserted from ``usage_total()``.
``fail_next(n)`` raises ``ModelUnavailable`` by default: the outage path that switches the router
to the fallback vendor (SPEC §12). The fake vendor ids (``fake_a``, ``fake_b``, ``fake_private``)
satisfy the config vendor validator (``^[a-z][a-z0-9_]+$``), so a router or a cost row typed
against ``ModelEndpoint`` accepts them.

``FakePrivateModel`` is the phase-3 in-region ``Tier2ModelPort``: refs in, a ``SafeStr`` summary
and a keyed hash out (SPEC §11). It is handed a ``LeakGuard`` to mint the summary, because a fake
never constructs a ``SafeStr`` itself. Its happy path needs a real ``RenderWitness``, which only
``nour/vault/renderer.py`` may mint: the wave-2 vault/renderer tests cover it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from nour.core.clock import Clock
from nour.core.errors import ModelUnavailable
from nour.core.hashing import canonical_json
from nour.core.leakguard import LeakGuard
from nour.core.ports import (
    CallLog,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    PrivateModelRequest,
    PrivateModelResult,
)
from nour.core.tier2 import RenderWitness
from nour.core.types import Desk
from nour.fakes import FakePort


@runtime_checkable
class ModelPolicy(Protocol):
    """What a ``ScriptedModel`` answers with when its FIFO is empty: any callable works."""

    def __call__(self, req: ModelRequest) -> ModelResponse: ...


COST_PER_1K_FILS: Mapping[str, tuple[int, int]] = {
    "fake_a": (300, 1500),
    "fake_b": (250, 1000),
    "fake_private": (0, 0),
}
"""``(input, output)`` fils per 1,000 tokens by fake vendor; unknown vendors use ``DEFAULT_COST``."""

DEFAULT_COST: tuple[int, int] = (100, 400)
NO_USAGE = ModelUsage(input_tokens=0, output_tokens=0, cost_fils=0)


def estimate_tokens(text: str) -> int:
    """A deterministic stand-in for a tokenizer: about four characters per token, at least one."""
    return max(1, (len(text) + 3) // 4)


def request_tokens(req: ModelRequest) -> int:
    """Tokens the request would cost: the system prompt, every message and every tool schema."""
    total = estimate_tokens(req.system)
    for message in req.messages:
        total += estimate_tokens(message.content) + 4
    for tool in req.tools:
        total += estimate_tokens(tool.name + tool.description) + estimate_tokens(
            json.dumps(tool.parameters, sort_keys=True, ensure_ascii=False)
        )
    return total


def response_tokens(resp: ModelResponse) -> int:
    total = estimate_tokens(resp.text) if resp.text else 0
    for call in resp.tool_calls:
        total += estimate_tokens(call.name) + estimate_tokens(
            canonical_json(call.arguments).decode("utf-8")
        )
    return total


def usage_estimate(vendor: str, req: ModelRequest, resp: ModelResponse) -> ModelUsage:
    """Deterministic usage for ``(req, resp)`` at ``vendor``'s rates."""
    rate_in, rate_out = COST_PER_1K_FILS.get(vendor, DEFAULT_COST)
    tokens_in, tokens_out = request_tokens(req), response_tokens(resp)
    cost = (tokens_in * rate_in + tokens_out * rate_out + 999) // 1000
    return ModelUsage(input_tokens=tokens_in, output_tokens=tokens_out, cost_fils=cost)


def add_usage(a: ModelUsage, b: ModelUsage) -> ModelUsage:
    return ModelUsage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cost_fils=a.cost_fils + b.cost_fils,
    )


class ScriptedModel(FakePort):
    """ModelPort fake: FIFO of enqueued responses, else the policy; records every request for leak scans.

    ``complete`` takes no ``PortCall`` and never writes to the ``CallLog`` (a model call is not a
    side effect); ``requests`` is the record, ``usage`` / ``usage_total`` the budget line.
    """

    port_name: str = "model"

    def __init__(
        self,
        vendor: str,
        model: str,
        policy: ModelPolicy | None = None,
        *,
        call_log: CallLog | None = None,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(call_log, clock)
        if not vendor or not model:
            raise ValueError("ScriptedModel needs a vendor and a model id")
        if policy is not None and not callable(policy):
            raise TypeError("a ModelPolicy is a callable taking a ModelRequest")
        self.vendor = vendor
        self.model = model
        self._policy: ModelPolicy | None = policy
        self.requests: list[ModelRequest] = []
        self.responses: list[ModelResponse] = []
        self.usage: list[ModelUsage] = []
        self._trace: list[tuple[ModelRequest, ModelResponse]] = []
        self._queue: list[ModelResponse] = []

    @property
    def policy(self) -> ModelPolicy:
        """The policy in force (``SanePolicy`` when none was given)."""
        if self._policy is None:
            from nour.fakes.policies import SanePolicy

            self._policy = SanePolicy()
        return self._policy

    @policy.setter
    def policy(self, value: ModelPolicy) -> None:
        if not callable(value):
            raise TypeError("a ModelPolicy is a callable taking a ModelRequest")
        self._policy = value

    def enqueue(self, resp: ModelResponse) -> None:
        """Queue one response to be returned before the policy is consulted."""
        if not isinstance(resp, ModelResponse):
            raise TypeError("enqueue takes a ModelResponse")
        self._queue.append(resp)

    @property
    def queued(self) -> int:
        return len(self._queue)

    def fail_next(self, n: int, exc: type[BaseException] = ModelUnavailable) -> None:
        """The next ``n`` ``complete`` calls raise ``exc`` (``ModelUnavailable`` by default: the
        router switches to the fallback vendor, SPEC §12). The request is still recorded."""
        super().fail_next(n, exc)

    def complete(self, req: ModelRequest) -> ModelResponse:
        """Record the request, then answer from the FIFO or the policy; the answer is re-stamped
        with this port's vendor/model and given a deterministic usage estimate when the source
        reported none."""
        if not isinstance(req, ModelRequest):
            raise TypeError("complete takes a ModelRequest (SafeStr system and messages)")
        self.requests.append(req)
        self._maybe_fail("complete")
        source = self._queue.pop(0) if self._queue else self.policy(req)
        if not isinstance(source, ModelResponse):
            raise TypeError("a ModelPolicy returns a ModelResponse")
        usage = source.usage
        if usage == NO_USAGE:
            usage = usage_estimate(self.vendor, req, source)
        resp = ModelResponse(
            text=source.text,
            tool_calls=list(source.tool_calls),
            vendor=self.vendor,
            model=self.model,
            usage=usage,
        )
        self.responses.append(resp)
        self.usage.append(usage)
        self._trace.append((req, resp))
        return resp

    # ----- inspection

    @property
    def trace(self) -> list[tuple[ModelRequest, ModelResponse]]:
        """Every ``(request, response)`` pair that completed (a failed call has no response)."""
        return list(self._trace)

    def usage_total(self, desk: Desk | None = None) -> ModelUsage:
        """Summed usage over completed calls, optionally for one desk's requests only."""
        total = NO_USAGE
        for (req, _), used in zip(self._trace, self.usage, strict=True):
            if desk is None or req.desk == desk:
                total = add_usage(total, used)
        return total


class FakePrivateModel(FakePort):
    """Tier2ModelPort fake: ``complete_private`` → ``PrivateModelResult("[private]", keyed hash)``."""

    port_name: str = "private_model"

    def __init__(
        self, guard: LeakGuard, call_log: CallLog | None = None, clock: Clock | None = None
    ) -> None:
        super().__init__(call_log, clock)
        if not isinstance(guard, LeakGuard):
            raise TypeError("FakePrivateModel takes the LeakGuard that mints its summaries")
        self.guard = guard
        self.calls: list[PrivateModelRequest] = []

    def complete_private(
        self, req: PrivateModelRequest, witness: RenderWitness
    ) -> PrivateModelResult:
        """Refs in, never values; a canned ``SafeStr`` summary and a keyed hash out (SPEC §11)."""
        if not isinstance(req, PrivateModelRequest):
            raise TypeError("complete_private takes a PrivateModelRequest")
        if not isinstance(witness, RenderWitness):
            raise TypeError("complete_private needs the renderer's RenderWitness")
        self.calls.append(req)  # the attempt is recorded before a scripted outage, like every fake
        self._maybe_fail("complete_private")
        fingerprint = self.guard.content_fp(
            canonical_json(
                {"refs": [ref.uri for ref in req.refs], "instruction": str(req.instruction)}
            ).decode("utf-8")
        )
        return PrivateModelResult(summary=self.guard.safe("[private]"), output_fp=fingerprint)
