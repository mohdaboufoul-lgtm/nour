"""Moona's tools: the typed vocabulary of everything he can do (docs/MOONA.md §3; SPEC §18
"every action is emitted as a tool call with … reason (one sentence)"; DESIGN §3.18 ``ToolSpec``
in miniature).

Each tool has a frozen argument model (``extra="forbid"``: a smuggled ``tier``, ``holder`` or
``amount`` argument is ``BAD_ARGS``, THREAT_REVIEW 5.6) and three structural flags the runtime
routes on: ``outbound`` (text leaves and must pass the honesty check and carry the disclosure),
``spends`` (goes through ``Wallet.authorize``), ``money`` (frozen for the tick when the scanner
found an instruction in observed content, DESIGN §4e rule 8 in miniature). What the model claims
about a call is never routed on; what the runtime derives from these flags is.

Amounts in arguments are decimal numbers of the wallet currency (``12.50``), converted with
``nour.moona.money.money`` and never accepted as floats finer than a minor unit. The invoice tool
carries no amount at all: the agreed price lives in the job row (``nour/moona/store.py``), so he
cannot invoice more than he agreed.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from nour.core.errors import Refusal
from nour.core.ports import ToolSchema
from nour.core.types import REASON_MAX, REASON_TERMINATORS, Reason, RefusalCode

TEXT_MAX = 4000


def sentence(text: str) -> Reason:
    """``text`` as one valid ``Reason`` whatever it holds: terminators (tool names carry dots)
    and line breaks become spaces, the result is cut to ``REASON_MAX`` and ends with one dot."""
    cleaned = "".join(" " if ch in REASON_TERMINATORS else ch for ch in " ".join(text.splitlines()))
    cleaned = " ".join(cleaned.split())
    return Reason(cleaned[: REASON_MAX - 1].rstrip() + ".")


class ToolArgs(BaseModel):
    """Base of every argument model: frozen, no extra keys, a one-sentence ``reason`` always."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: Reason


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label} is a number")
    if isinstance(value, float):
        value = repr(value)  # 12.5 → "12.5"; never the binary expansion
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{label} is not a number: {value!r}") from exc
    if not amount.is_finite():
        raise ValueError(f"{label} is not a number: {value!r}")
    if amount * 100 != (amount * 100).to_integral_value():
        raise ValueError(f"{label} has at most two decimals")
    return amount


class BidArgs(ToolArgs):
    """``market.bid``: claim an open request at ``price`` with a short message to the client."""

    request_id: str = Field(min_length=1, max_length=64)
    price: Decimal
    message: str = Field(min_length=1, max_length=TEXT_MAX)

    @field_validator("price", mode="before")
    @classmethod
    def _price(cls, value: Any) -> Decimal:
        amount = _decimal(value, "price")
        if amount <= 0:
            raise ValueError("price is positive")
        return amount


class DeliverArgs(ToolArgs):
    """``work.deliver``: hand in the work for a job the client accepted."""

    request_id: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=TEXT_MAX)


class InvoiceArgs(ToolArgs):
    """``payment.request``: invoice the agreed price of a delivered job (no amount argument)."""

    request_id: str = Field(min_length=1, max_length=64)


class SpendArgs(ToolArgs):
    """``wallet.spend``: buy something with his own money; ``expected_return`` is his hypothesis."""

    amount: Decimal
    merchant: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=1, max_length=500)
    expected_return: Decimal | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def _amount(cls, value: Any) -> Decimal:
        amount = _decimal(value, "amount")
        if amount <= 0:
            raise ValueError("amount is positive")
        return amount

    @field_validator("expected_return", mode="before")
    @classmethod
    def _expected(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        amount = _decimal(value, "expected_return")
        if amount < 0:
            raise ValueError("expected_return is not negative")
        return amount


class NoteArgs(ToolArgs):
    """``memory.note``: one line for his own memory."""

    text: str = Field(min_length=1, max_length=2000)


class RestArgs(ToolArgs):
    """``rest``: no thinking for ``hours`` (upkeep still runs)."""

    hours: int = Field(ge=1, le=24 * 14)


class ReportArgs(ToolArgs):
    """``owner.report``: a line to the owner's inbox (``moona inbox``)."""

    text: str = Field(min_length=1, max_length=2000)


class MoonaTool(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """One tool: name, what the model is told, the argument model and the routing flags."""

    name: str
    description: str
    args_model: type[ToolArgs]
    outbound: bool
    spends: bool
    money: bool

    def tool_schema(self) -> ToolSchema:
        parameters = self.args_model.model_json_schema()
        parameters.pop("title", None)
        parameters.setdefault("properties", {})["reason"] = {
            "type": "string",
            "description": "One sentence: why this action, for your journal.",
        }
        return ToolSchema(name=self.name, description=self.description, parameters=parameters)


BID = "market.bid"
DELIVER = "work.deliver"
INVOICE = "payment.request"
SPEND = "wallet.spend"
NOTE = "memory.note"
REST = "rest"
REPORT = "owner.report"

TOOLS: tuple[MoonaTool, ...] = (
    MoonaTool(
        name=BID,
        description=(
            "Claim an open marketplace request at a price (wallet currency, decimal) with a short "
            "message to the client. The client accepts or rejects; one bid per request. Your "
            "disclosure line is appended; a message claiming you are human is refused."
        ),
        args_model=BidArgs,
        outbound=True,
        spends=False,
        money=False,
    ),
    MoonaTool(
        name=DELIVER,
        description=(
            "Deliver the finished work for a job the client accepted: the content is sent as is. "
            "Deliver only what the request asked for."
        ),
        args_model=DeliverArgs,
        outbound=True,
        spends=False,
        money=False,
    ),
    MoonaTool(
        name=INVOICE,
        description=(
            "Invoice a delivered job for the price that was agreed when the bid was accepted "
            "(no amount argument: the agreed price is the only amount). Money arrives in your "
            "wallet when the client's payment settles, which can take hours or days."
        ),
        args_model=InvoiceArgs,
        outbound=True,
        spends=False,
        money=True,
    ),
    MoonaTool(
        name=SPEND,
        description=(
            "Buy something with your own money: amount (wallet currency, decimal), merchant, "
            "purpose, and the return you expect. The wallet declines anything above your balance "
            "or above the single-purchase limit; gambling, adult and tobacco merchants are refused."
        ),
        args_model=SpendArgs,
        outbound=False,
        spends=True,
        money=True,
    ),
    MoonaTool(
        name=NOTE,
        description="Write one line to your memory; you will see your recent notes every tick.",
        args_model=NoteArgs,
        outbound=False,
        spends=False,
        money=False,
    ),
    MoonaTool(
        name=REST,
        description=(
            "Stop thinking for a number of hours: no model calls, no cost of thinking; the daily "
            "upkeep still runs and payments still settle. Use it whenever there is nothing useful "
            "to do."
        ),
        args_model=RestArgs,
        outbound=False,
        spends=False,
        money=False,
    ),
    MoonaTool(
        name=REPORT,
        description="Leave a line for the owner (he reads it when he chooses; he will not reply).",
        args_model=ReportArgs,
        outbound=False,
        spends=False,
        money=False,
    ),
)

TOOL_BY_NAME: dict[str, MoonaTool] = {tool.name: tool for tool in TOOLS}


def tool_schemas() -> list[ToolSchema]:
    """The schemas the model sees, sorted by name (a stable list keeps the cache prefix)."""
    return [tool.tool_schema() for tool in sorted(TOOLS, key=lambda tool: tool.name)]


def render_tool_list() -> str:
    """The ``## Tools`` section of the system prompt: one line per tool with its arguments."""
    lines = []
    for tool in sorted(TOOLS, key=lambda tool: tool.name):
        args = ", ".join(name for name in tool.args_model.model_fields if name != "reason")
        lines.append(f"- `{tool.name}({args}, reason)`: {tool.description}")
    return "\n".join(lines)


def parse_call(name: str, arguments: dict[str, Any]) -> tuple[MoonaTool, ToolArgs]:
    """The tool and its validated arguments, or a ``Refusal``: ``UNKNOWN_TOOL`` for a name not in
    the vocabulary, ``BAD_REASON`` when ``reason`` is missing or empty (``Reason.coerce``
    truncates to the first sentence, DESIGN §10), ``BAD_ARGS`` for anything else wrong."""
    tool = TOOL_BY_NAME.get(name)
    if tool is None:
        raise Refusal(RefusalCode.UNKNOWN_TOOL, sentence(f"no tool named {name[:40]!r}"))
    if not isinstance(arguments, dict):
        raise Refusal(RefusalCode.BAD_ARGS, sentence(f"{name}: arguments must be an object"))
    raw_reason = arguments.get("reason")
    try:
        reason = Reason.coerce(raw_reason if isinstance(raw_reason, str) else None)
    except ValueError as exc:
        raise Refusal(RefusalCode.BAD_REASON, sentence(f"{name}: {str(exc)[:160]}")) from exc
    if reason is None:
        raise Refusal(
            RefusalCode.BAD_REASON, sentence(f"{name}: a one-sentence reason is required")
        )
    try:
        args = tool.args_model.model_validate({**arguments, "reason": reason})
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "arguments"
        raise Refusal(
            RefusalCode.BAD_ARGS, sentence(f"{name}: {where}: {str(first['msg'])[:120]}")
        ) from exc
    return tool, args
