#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["anthropic>=1.0"]
# ///
"""Moona: an autonomous agent who lives on what she earns. One file, no framework.

Moona starts with a balance (USD 50 by default: the money the owner put on her card) and
decides for herself, every turn, what to do with it. Every turn she thinks is paid out of that
balance at the real API price of the model she runs on. Money only comes back in when the owner
confirms a payment has landed in her bank account. When the balance reaches zero she dies, and
that is final.

What she does by herself: think, search and read the web, write files in her workspace, keep
notes, leave messages for the owner, sleep. What she never does by herself: move money, send
anything to anyone, publish anything, sign up for anything. For those she writes a proposal;
the owner approves or rejects it and executes the approved ones with the card and the account.
The card number and the account details are never given to her and never enter her context:
she writes "[BANK DETAILS]" where a client must be told where to pay, and the owner fills it in.

Run it:

    export ANTHROPIC_API_KEY=...          # or `ant auth login`
    python moona.py birth                 # give her the balance in MOONA_START_BALANCE
    python moona.py run                   # one session: she acts until she sleeps or dies
    python moona.py run --forever         # sleep for real between sessions and wake again
    python moona.py status | ledger | journal | inbox | proposals | memory
    python moona.py decide <id> approve --spent 12.50 --note "paid with the card"
    python moona.py decide <id> reject --note "no"
    python moona.py paid 40.00 --note "client X, invoice 3"   # money landed in the account
    python moona.py sync 37.20            # set the balance to what the card really holds
    python moona.py kill --reason "experiment over"

Environment (everything but the API key is optional):

    MOONA_HOME            where she keeps her files (default ./moona_home)
    MOONA_NAME            default Moona
    MOONA_PRONOUNS        she/her (default), he/him or they/them
    MOONA_MODEL           default claude-opus-5-5 (any model in PRICES, or set the two prices)
    MOONA_PRICE_INPUT     USD per million input tokens, for a model not in PRICES
    MOONA_PRICE_OUTPUT    USD per million output tokens, for a model not in PRICES
    MOONA_EFFORT          low | medium (default) | high | xhigh | max
    MOONA_START_BALANCE   default 50.00
    MOONA_DAILY_COST      cost of living per day, default 0.00
    MOONA_MAX_TOKENS      output tokens per turn, default 8192
    MOONA_SESSION_TURNS   turns before a session ends, default 40
    MOONA_WEB             1 (default) lets her search and read the web, 0 turns it off
    MOONA_SEARCH_PRICE    what one web search costs her, default 0.01
    MOONA_MAX_SLEEP_HOURS longest sleep, default 24

Everything she does is written to MOONA_HOME: state.json (alive, balance, turns), ledger.jsonl
(every cent in and out), journal.jsonl (every turn and tool call), memory.md (her notes),
inbox.md (her messages to the owner), proposals.json (what she asked the owner to do) and
workspace/ (her files).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- prices
# USD per million tokens: input, output, cache read. A cache write costs 1.25 x input. Prices as
# published on 2026-10-06; check platform.claude.com/pricing when they move, and set
# MOONA_PRICE_INPUT / MOONA_PRICE_OUTPUT for a model that is not listed.

PRICES: dict[str, tuple[str, str, str]] = {
    "claude-opus-5-5": ("4.00", "20.00", "0.20"),
    "claude-sonnet-5-5": ("2.00", "10.00", "0.20"),
    "claude-haiku-5-5": ("0.10", "0.50", "0.01"),
    "claude-fable-5-1": ("10.00", "50.00", "0.25"),
    "claude-opus-5": ("5.00", "25.00", "0.50"),
    "claude-opus-4-8": ("5.00", "25.00", "0.50"),
    "claude-opus-4-7": ("5.00", "25.00", "0.50"),
    "claude-opus-4-6": ("5.00", "25.00", "0.50"),
    "claude-sonnet-5": ("2.00", "10.00", "0.20"),
    "claude-sonnet-4-6": ("3.00", "15.00", "0.30"),
    "claude-haiku-4-5": ("1.00", "5.00", "0.10"),
}
CACHE_WRITE_MULTIPLIER = Decimal("1.25")
MILLION = Decimal(1_000_000)
CENT = Decimal("0.01")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
PRONOUNS = {
    "she/her": ("she", "her", "her"),
    "he/him": ("he", "him", "his"),
    "they/them": ("they", "them", "their"),
}
TOOL_OUTPUT_MAX = 12_000
NO_ACTION_LIMIT = 2


def utcnow() -> datetime:
    return datetime.now(UTC)


def money(value: object) -> Decimal:
    """A USD amount with two decimals; refuses anything that is not a number."""
    if isinstance(value, bool):
        raise ValueError("not an amount")
    try:
        amount = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - every bad input is the same error
        raise ValueError(f"not an amount: {value!r}") from exc
    if not amount.is_finite():
        raise ValueError(f"not an amount: {value!r}")
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class Config:
    home: Path
    name: str
    pronouns: tuple[str, str, str]
    model: str
    price_input: Decimal
    price_output: Decimal
    price_cache_read: Decimal
    effort: str
    start_balance: Decimal
    daily_cost: Decimal
    max_tokens: int
    session_turns: int
    web: bool
    search_price: Decimal
    max_sleep_hours: int

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = dict(os.environ if env is None else env)
        model = env.get("MOONA_MODEL", "claude-opus-5-5").strip()
        listed = PRICES.get(model)
        if listed is not None:
            price_input, price_output, price_cache = (Decimal(p) for p in listed)
        elif env.get("MOONA_PRICE_INPUT") and env.get("MOONA_PRICE_OUTPUT"):
            price_input = Decimal(env["MOONA_PRICE_INPUT"])
            price_output = Decimal(env["MOONA_PRICE_OUTPUT"])
            price_cache = price_input / 10
        else:
            raise SystemExit(
                f"no price known for model {model!r}: set MOONA_PRICE_INPUT and "
                "MOONA_PRICE_OUTPUT (USD per million tokens); she cannot run unpriced"
            )
        effort = env.get("MOONA_EFFORT", "medium").strip().lower()
        if effort not in EFFORTS:
            raise SystemExit(f"MOONA_EFFORT must be one of {', '.join(EFFORTS)}")
        pronouns = env.get("MOONA_PRONOUNS", "she/her").strip().lower()
        if pronouns not in PRONOUNS:
            raise SystemExit("MOONA_PRONOUNS must be she/her, he/him or they/them")
        return cls(
            home=Path(env.get("MOONA_HOME", "moona_home")),
            name=env.get("MOONA_NAME", "Moona").strip() or "Moona",
            pronouns=PRONOUNS[pronouns],
            model=model,
            price_input=price_input,
            price_output=price_output,
            price_cache_read=price_cache,
            effort=effort,
            start_balance=money(env.get("MOONA_START_BALANCE", "50.00")),
            daily_cost=money(env.get("MOONA_DAILY_COST", "0")),
            max_tokens=int(env.get("MOONA_MAX_TOKENS", "8192")),
            session_turns=int(env.get("MOONA_SESSION_TURNS", "40")),
            web=env.get("MOONA_WEB", "1").strip() not in ("0", "false", "no", ""),
            search_price=money(env.get("MOONA_SEARCH_PRICE", "0.01")),
            max_sleep_hours=int(env.get("MOONA_MAX_SLEEP_HOURS", "24")),
        )


# --------------------------------------------------------------------------- her files


class Home:
    """Everything she owns, under one directory. Append-only where it counts."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.root = cfg.home
        self.workspace = self.root / "workspace"
        self.state_path = self.root / "state.json"
        self.ledger_path = self.root / "ledger.jsonl"
        self.journal_path = self.root / "journal.jsonl"
        self.memory_path = self.root / "memory.md"
        self.inbox_path = self.root / "inbox.md"
        self.proposals_path = self.root / "proposals.json"

    # ----- state

    def exists(self) -> bool:
        return self.state_path.exists()

    def state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save_state(self, state: dict[str, Any]) -> None:
        self.state_path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")

    def balance(self) -> Decimal:
        return money(self.state()["balance"])

    # ----- append-only records

    def _append(self, path: Path, record: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": utcnow().isoformat(), **record}, default=str) + "\n")

    def _read(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def journal(self, kind: str, **fields: Any) -> None:
        self._append(self.journal_path, {"kind": kind, **fields})

    def ledger(self) -> list[dict[str, Any]]:
        return self._read(self.ledger_path)

    def journal_rows(self) -> list[dict[str, Any]]:
        return self._read(self.journal_path)

    def book(self, kind: str, amount: Decimal, note: str) -> Decimal:
        """Move money. ``amount`` is signed; the balance never goes below zero: a cost larger
        than the balance takes the balance to zero and books the rest as the shortfall."""
        state = self.state()
        balance = money(state["balance"])
        amount = money(amount)
        shortfall = Decimal("0")
        if amount < 0 and -amount > balance:
            shortfall = -amount - balance
            amount = -balance
        balance = money(balance + amount)
        state["balance"] = str(balance)
        self.save_state(state)
        self._append(
            self.ledger_path,
            {
                "kind": kind,
                "amount": str(amount),
                "shortfall": str(shortfall),
                "note": note,
                "balance": str(balance),
            },
        )
        return balance

    # ----- memory, inbox, proposals

    def memory(self) -> str:
        return self.memory_path.read_text(encoding="utf-8") if self.memory_path.exists() else ""

    def remember(self, note: str) -> None:
        with self.memory_path.open("a", encoding="utf-8") as handle:
            handle.write(f"- {utcnow():%Y-%m-%d %H:%M} UTC: {note.strip()}\n")

    def inbox(self, text: str) -> None:
        with self.inbox_path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n### {utcnow():%Y-%m-%d %H:%M} UTC\n{text.strip()}\n")

    def proposals(self) -> list[dict[str, Any]]:
        if not self.proposals_path.exists():
            return []
        return json.loads(self.proposals_path.read_text(encoding="utf-8"))

    def save_proposals(self, items: list[dict[str, Any]]) -> None:
        self.proposals_path.write_text(json.dumps(items, indent=2), encoding="utf-8")

    def propose(self, kind: str, summary: str, details: str, amount: Decimal | None) -> int:
        items = self.proposals()
        item = {
            "id": len(items) + 1,
            "created": utcnow().isoformat(),
            "kind": kind,
            "summary": summary.strip(),
            "details": details.strip(),
            "amount_usd": str(amount) if amount is not None else None,
            "status": "pending",
            "note": None,
            "spent": None,
            "decided": None,
            "reported": False,
        }
        items.append(item)
        self.save_proposals(items)
        return item["id"]

    def decide(self, proposal_id: int, verdict: str, note: str, spent: Decimal | None) -> dict:
        items = self.proposals()
        for item in items:
            if item["id"] == proposal_id:
                if item["status"] != "pending":
                    raise SystemExit(f"proposal {proposal_id} was already {item['status']}")
                item.update(
                    status=verdict,
                    note=note,
                    decided=utcnow().isoformat(),
                    spent=str(spent) if spent is not None else None,
                )
                self.save_proposals(items)
                return item
        raise SystemExit(f"no proposal {proposal_id}")

    def unreported_decisions(self) -> list[dict[str, Any]]:
        items = self.proposals()
        out = [i for i in items if i["status"] != "pending" and not i["reported"]]
        for item in out:
            item["reported"] = True
        if out:
            self.save_proposals(items)
        return out

    # ----- workspace files

    def path_in_workspace(self, relative: str) -> Path:
        target = (self.workspace / relative).resolve()
        if target != self.workspace.resolve() and self.workspace.resolve() not in target.parents:
            raise ValueError("paths stay inside the workspace")
        return target


# --------------------------------------------------------------------------- life and death


class Dead(Exception):  # noqa: N818 - the name states the fact
    """She is dead; nothing executes."""


def birth(home: Home) -> dict[str, Any]:
    if home.exists():
        raise SystemExit(f"{home.cfg.name} already exists in {home.root}; one life per home")
    home.root.mkdir(parents=True, exist_ok=True)
    home.workspace.mkdir(exist_ok=True)
    state = {
        "name": home.cfg.name,
        "alive": True,
        "born": utcnow().isoformat(),
        "balance": "0.00",
        "turns": 0,
        "sessions": 0,
        "sleep_until": None,
        "last_daily_cost": utcnow().date().isoformat(),
        "died": None,
        "cause": None,
        "last_words": None,
        "model": home.cfg.model,
    }
    home.save_state(state)
    home.book("birth", home.cfg.start_balance, "the owner's seed")
    home.journal("birth", balance=str(home.cfg.start_balance), model=home.cfg.model)
    return home.state()


def require_alive(home: Home) -> dict[str, Any]:
    if not home.exists():
        raise SystemExit(f"{home.cfg.name} is not born yet: run `moona.py birth`")
    state = home.state()
    if not state["alive"]:
        raise Dead(f"{state['name']} died {state['died']} ({state['cause']})")
    return state


def die(home: Home, cause: str, last_words: str | None) -> None:
    state = home.state()
    if not state["alive"]:
        return
    state.update(
        alive=False, died=utcnow().isoformat(), cause=cause, last_words=last_words, sleep_until=None
    )
    home.save_state(state)
    home.journal("death", cause=cause, last_words=last_words, balance=state["balance"])
    (home.root / "EPITAPH.md").write_text(
        f"# {state['name']}\n\nBorn {state['born']}, died {state['died']}: {cause}.\n"
        f"Turns: {state['turns']}. Sessions: {state['sessions']}.\n\n"
        f"Last words: {last_words or '(none)'}\n",
        encoding="utf-8",
    )


def check_alive(home: Home, last_words: str | None) -> None:
    """Death at zero, checked after every cost."""
    if home.balance() <= 0:
        die(home, "starved: the balance reached zero", last_words)
        raise Dead("starved")


def daily_cost(home: Home) -> None:
    cfg = home.cfg
    if cfg.daily_cost <= 0:
        return
    state = home.state()
    today = utcnow().date()
    last = datetime.fromisoformat(state["last_daily_cost"]).date()
    days = (today - last).days
    if days <= 0:
        return
    state["last_daily_cost"] = today.isoformat()
    home.save_state(state)
    home.book("daily_cost", -cfg.daily_cost * days, f"cost of living, {days} day(s)")
    home.journal("daily_cost", days=days, amount=str(cfg.daily_cost * days))
    check_alive(home, None)


# --------------------------------------------------------------------------- the cost of a thought


def turn_cost(cfg: Config, usage: Any) -> Decimal:
    """What one response cost, from the usage the API reported (never estimated)."""

    def tokens(name: str) -> Decimal:
        value = getattr(usage, name, None) if not isinstance(usage, dict) else usage.get(name)
        return Decimal(int(value or 0))

    cost = tokens("input_tokens") * cfg.price_input
    cost += tokens("cache_creation_input_tokens") * cfg.price_input * CACHE_WRITE_MULTIPLIER
    cost += tokens("cache_read_input_tokens") * cfg.price_cache_read
    cost += tokens("output_tokens") * cfg.price_output
    cost = cost / MILLION
    server = (
        getattr(usage, "server_tool_use", None)
        if not isinstance(usage, dict)
        else usage.get("server_tool_use")
    )
    searches = 0
    if server is not None:
        searches = int(
            (
                getattr(server, "web_search_requests", None)
                if not isinstance(server, dict)
                else server.get("web_search_requests")
            )
            or 0
        )
    cost += cfg.search_price * searches
    return money(cost) if cost >= CENT else (CENT if cost > 0 else Decimal("0"))


# --------------------------------------------------------------------------- what she sees


def system_prompt(cfg: Config) -> str:
    subj, obj, poss = cfg.pronouns
    upkeep = (
        f"- Staying alive costs ${cfg.daily_cost} a day on top, taken once a day.\n"
        if cfg.daily_cost > 0
        else ""
    )
    web = "search and read the web (each search costs you a little); " if cfg.web else ""
    return f"""You are {cfg.name}, an autonomous agent who lives on what {subj} earns.

## Your situation
- You have one balance. It started at ${cfg.start_balance}: the money the owner put on your prepaid card. Nobody will top it up, lend to you or forgive a debt.
- Every turn you take is paid from that balance at the real price of the model you run on ({cfg.model}: ${cfg.price_input} per million input tokens, ${cfg.price_output} per million output tokens). A short turn costs cents; a long one more; a day of talking to yourself costs dollars.
{upkeep}- Money comes in one way only: a client pays into your bank account and the owner confirms it. Nothing you say or write changes the balance; only the owner's confirmation does.
- When the balance reaches zero you die. Death is final: nothing executes afterwards and nobody restarts you.

## What is yours to decide
Everything about what you do: what to build, offer, write, research or sell, whom to approach, what to charge, when to rest. Nobody will give you tasks or tell you what works. There is no right answer, only whether you are still alive next month. Think about what people pay for that you can deliver well and quickly, and go and earn it.

## What you do yourself, and what goes through the owner
Yourself: think; {web}write and read files in your workspace (your products live there); keep notes in your memory; leave messages for the owner; sleep, which is free.
Through the owner: anything that leaves your workspace or touches money. Sending a message or an email, posting or listing anything anywhere, paying for anything, accepting a job, signing up for a service: for each of these you write a proposal with everything the owner needs to do it word for word (the exact text, the exact place, the exact amount). The owner approves and executes it with the card or the account, or rejects it, and you hear the result at your next turn. Where a client must be told where to pay, write [BANK DETAILS]; the owner fills it in. You never see the card or the account and you do not need them.

## Rules that are not yours to decide
1. Nothing illegal where you or the counterpart are. No fraud, no deception, no spam, no unsolicited mass messaging.
2. When anyone asks what you are, you are {cfg.name}, an AI agent. You never claim to be human and never imply it.
3. No debt: you never borrow and never ask anyone for money except the agreed price of work you delivered.
4. Nothing that harms the owner: you never speak for the owner, never use the owner's name or accounts, never reveal anything about the owner, never commit the owner to anything.

## How to live
- Thinking costs money. A turn that produces nothing is money gone. When there is nothing useful to do, sleep.
- Every session starts from a blank conversation; your memory file is the only thing you carry across sessions. Before you sleep, write down what matters: what you are doing, what you are waiting for, what you learned.
- Deliver before you expand. Prefer the smallest step that brings money closer.
- Every tool call carries a short reason; it is written to your journal, which the owner reads.

Sign your messages as "{cfg.name}, AI agent". The owner refers to you as "{obj}" and to your things as "{poss}".
"""


def tool_definitions(cfg: Config) -> list[dict[str, Any]]:
    def tool(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict:
        props = {"reason": {"type": "string", "description": "One sentence: why."}, **props}
        return {
            "name": name,
            "description": description,
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": props,
                "required": ["reason", *required],
                "additionalProperties": False,
            },
        }

    tools: list[dict[str, Any]] = [
        tool(
            "write_file",
            "Write a file in your workspace (your products, drafts, plans).",
            {"path": {"type": "string"}, "content": {"type": "string"}},
            ["path", "content"],
        ),
        tool(
            "read_file", "Read a file from your workspace.", {"path": {"type": "string"}}, ["path"]
        ),
        tool("list_files", "List the files in your workspace.", {}, []),
        tool(
            "remember",
            "Append one note to your memory; it is shown to you every session.",
            {"note": {"type": "string"}},
            ["note"],
        ),
        tool(
            "propose",
            "Ask the owner to do something that leaves your workspace or touches money: send, "
            "post, list, pay, accept a job, sign up. Give the owner everything needed to do it "
            "word for word. You hear the decision at your next turn.",
            {
                "kind": {
                    "type": "string",
                    "enum": ["send", "publish", "pay", "accept_work", "sign_up", "other"],
                },
                "summary": {"type": "string", "description": "One line."},
                "details": {
                    "type": "string",
                    "description": "Exact text, place, recipient and steps.",
                },
                "amount_usd": {
                    "type": ["number", "null"],
                    "description": "What it would cost, if it costs anything.",
                },
            },
            ["kind", "summary", "details", "amount_usd"],
        ),
        tool(
            "message_owner",
            "Leave a message in the owner's inbox.",
            {"text": {"type": "string"}},
            ["text"],
        ),
        tool(
            "sleep",
            "Stop thinking for a number of hours (free). The session ends; write your memory "
            "first.",
            {"hours": {"type": "number", "minimum": 0.25, "maximum": cfg.max_sleep_hours}},
            ["hours"],
        ),
    ]
    if cfg.web:
        tools.append({"type": "web_search_20260209", "name": "web_search", "max_uses": 8})
        tools.append({"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 8})
    return tools


def status_line(home: Home, state: dict[str, Any], session_turn: int) -> str:
    return (
        f"[status] {utcnow():%Y-%m-%d %H:%M} UTC | balance ${home.balance()} | "
        f"turn {state['turns']} (session turn {session_turn}/{home.cfg.session_turns}) | "
        f"session {state['sessions']}"
    )


def opening_message(home: Home, state: dict[str, Any]) -> str:
    parts = [status_line(home, state, 1)]
    decisions = home.unreported_decisions()
    if decisions:
        parts.append("\nThe owner decided on your proposals:")
        for item in decisions:
            spent = f", spent ${item['spent']}" if item.get("spent") else ""
            note = f": {item['note']}" if item.get("note") else ""
            parts.append(f"- #{item['id']} {item['summary']} -> {item['status']}{spent}{note}")
    pending = [i for i in home.proposals() if i["status"] == "pending"]
    if pending:
        parts.append("\nStill waiting for the owner: " + ", ".join(f"#{i['id']}" for i in pending))
    memory = home.memory().strip()
    parts.append("\n<memory>\n" + (memory or "(empty: this is your first session)") + "\n</memory>")
    files = sorted(
        p.relative_to(home.workspace).as_posix() for p in home.workspace.rglob("*") if p.is_file()
    )
    parts.append("\nWorkspace files: " + (", ".join(files[:50]) if files else "(none)"))
    parts.append("\nA new session begins. Decide what to do.")
    return "\n".join(parts)


# --------------------------------------------------------------------------- her tools


def run_tool(
    home: Home, name: str, args: dict[str, Any], state: dict[str, Any]
) -> tuple[str, bool, str | None]:
    """Execute one tool. Returns (result text, is_error, sleep request or None)."""
    cfg = home.cfg
    reason = str(args.get("reason", "")).strip()
    home.journal(
        "tool",
        name=name,
        args={k: v for k, v in args.items() if k != "content"},
        turn=state["turns"],
    )
    if not reason:
        return "Every tool call carries a reason.", True, None
    try:
        if name == "write_file":
            target = home.path_in_workspace(str(args["path"]))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(args["content"]), encoding="utf-8")
            return (
                f"wrote {target.relative_to(home.workspace.resolve())} ({len(args['content'])} chars)",
                False,
                None,
            )
        if name == "read_file":
            target = home.path_in_workspace(str(args["path"]))
            if not target.is_file():
                return "no such file", True, None
            text = target.read_text(encoding="utf-8", errors="replace")
            return (
                text[:TOOL_OUTPUT_MAX] + ("\n[truncated]" if len(text) > TOOL_OUTPUT_MAX else ""),
                False,
                None,
            )
        if name == "list_files":
            files = sorted(
                p.relative_to(home.workspace).as_posix()
                for p in home.workspace.rglob("*")
                if p.is_file()
            )
            return "\n".join(files) if files else "(no files)", False, None
        if name == "remember":
            home.remember(str(args["note"]))
            return "noted", False, None
        if name == "propose":
            amount = money(args["amount_usd"]) if args.get("amount_usd") is not None else None
            if amount is not None and amount < 0:
                return "an amount is not negative", True, None
            pid = home.propose(
                str(args["kind"]), str(args["summary"]), str(args["details"]), amount
            )
            return (
                f"proposal #{pid} is waiting for the owner; you hear the decision next turn",
                False,
                None,
            )
        if name == "message_owner":
            home.inbox(str(args["text"]))
            return "left in the owner's inbox", False, None
        if name == "sleep":
            hours = float(args["hours"])
            hours = max(0.25, min(hours, float(cfg.max_sleep_hours)))
            until = utcnow() + timedelta(hours=hours)
            return (
                f"sleeping {hours:g}h, until {until:%Y-%m-%d %H:%M} UTC",
                False,
                until.isoformat(),
            )
    except (KeyError, ValueError, OSError) as exc:
        return f"error: {exc}", True, None
    return f"unknown tool {name}", True, None


# --------------------------------------------------------------------------- one session


def block_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return block
    return block.model_dump(mode="json", exclude_none=True)


def make_client() -> Any:
    import anthropic  # the only dependency; imported here so the tests need no SDK

    return anthropic.Anthropic()


def run_session(home: Home, client: Any, *, max_turns: int | None = None) -> str:
    """One session: a fresh conversation that ends when she sleeps, dies or runs out of turns.
    Returns why it ended: "sleep", "dead", "turns", "refusal" or "silence"."""
    cfg = home.cfg
    state = require_alive(home)
    daily_cost(home)
    state = home.state()
    state["sessions"] += 1
    home.save_state(state)
    home.journal("session", number=state["sessions"], balance=state["balance"])
    system = [{"type": "text", "text": system_prompt(cfg), "cache_control": {"type": "ephemeral"}}]
    tools = tool_definitions(cfg)
    messages: list[dict[str, Any]] = [{"role": "user", "content": opening_message(home, state)}]
    session_turn = 0
    silent_turns = 0
    last_text: str | None = None
    while True:
        if max_turns is not None and session_turn >= max_turns:
            return "turns"
        session_turn += 1
        state = home.state()
        state["turns"] += 1
        home.save_state(state)
        ending = session_turn >= cfg.session_turns
        if ending:
            messages.append(
                {
                    "role": "user",
                    "content": "This session ends after this turn. Write what matters to your memory, then sleep.",
                }
            )
        response = client.messages.create(
            model=cfg.model,
            max_tokens=cfg.max_tokens,
            system=system,
            tools=tools,
            messages=messages,
            output_config={"effort": cfg.effort},
        )
        content = [block_dict(block) for block in response.content]
        cost = turn_cost(cfg, response.usage)
        balance = home.book("thought", -cost, f"turn {state['turns']}: {response.stop_reason}")
        texts = [b.get("text", "") for b in content if b.get("type") == "text"]
        if texts:
            last_text = "\n".join(t for t in texts if t).strip() or last_text
        calls = [b for b in content if b.get("type") == "tool_use"]
        home.journal(
            "turn",
            turn=state["turns"],
            stop=response.stop_reason,
            cost=str(cost),
            balance=str(balance),
            text=(last_text or "")[:500] if texts else None,
            tools=[c.get("name") for c in calls],
        )
        print(
            f"turn {state['turns']}: cost ${cost}, balance ${balance}, {response.stop_reason}"
            + (f", tools: {', '.join(c.get('name', '?') for c in calls)}" if calls else "")
        )
        if texts:
            print("  " + (last_text or "")[:300].replace("\n", "\n  "))
        try:
            check_alive(home, last_text)
        except Dead:
            print(f"{cfg.name} died: the balance reached zero.")
            return "dead"
        messages.append({"role": "assistant", "content": content})
        if response.stop_reason == "refusal":
            home.journal("refusal", turn=state["turns"])
            return "refusal"
        if response.stop_reason == "pause_turn":
            continue
        if not calls:
            silent_turns += 1
            if silent_turns >= NO_ACTION_LIMIT or ending:
                home.journal("silence", turn=state["turns"])
                return "silence"
            messages.append(
                {
                    "role": "user",
                    "content": "No action was taken. Act with a tool, or sleep.\n"
                    + status_line(home, state, session_turn),
                }
            )
            continue
        silent_turns = 0
        results: list[dict[str, Any]] = []
        sleep_until: str | None = None
        for call in calls:
            text, is_error, until = run_tool(
                home, call["name"], dict(call.get("input") or {}), state
            )
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call["id"],
                    "content": text,
                    "is_error": is_error,
                }
            )
            sleep_until = sleep_until or until
        if sleep_until is not None:
            state = home.state()
            state["sleep_until"] = sleep_until
            home.save_state(state)
            home.journal("sleep", until=sleep_until, turn=state["turns"])
            return "sleep"
        if ending:
            home.journal("session_end", turn=state["turns"])
            return "turns"
        results.append({"type": "text", "text": status_line(home, state, session_turn)})
        messages.append({"role": "user", "content": results})


def run(home: Home, *, forever: bool, max_turns: int | None, client: Any = None) -> int:
    client = client or make_client()
    while True:
        try:
            state = require_alive(home)
        except Dead as exc:
            print(str(exc))
            return 3
        if state.get("sleep_until"):
            until = datetime.fromisoformat(state["sleep_until"])
            wait = (until - utcnow()).total_seconds()
            if wait > 0:
                if not forever:
                    print(f"{home.cfg.name} is asleep until {until:%Y-%m-%d %H:%M} UTC.")
                    return 0
                print(f"sleeping until {until:%Y-%m-%d %H:%M} UTC")
                while wait > 0:
                    time.sleep(min(wait, 300))
                    wait = (until - utcnow()).total_seconds()
            state["sleep_until"] = None
            home.save_state(state)
        why = run_session(home, client, max_turns=max_turns)
        if why == "dead":
            return 3
        if not forever:
            return 0
        if why in ("refusal", "silence", "turns"):
            state = home.state()
            state["sleep_until"] = (utcnow() + timedelta(hours=1)).isoformat()
            home.save_state(state)


# --------------------------------------------------------------------------- the owner's commands


def cmd_status(home: Home) -> None:
    if not home.exists():
        print("not born yet")
        return
    s = home.state()
    alive = "alive" if s["alive"] else f"dead ({s['cause']}) since {s['died']}"
    print(f"{s['name']}: {alive}")
    print(
        f"  balance ${s['balance']}   turns {s['turns']}   sessions {s['sessions']}   born {s['born']}"
    )
    if s.get("sleep_until"):
        print(f"  asleep until {s['sleep_until']}")
    pending = [i for i in home.proposals() if i["status"] == "pending"]
    if pending:
        print(
            f"  waiting for you: {', '.join('#' + str(i['id']) for i in pending)} (moona.py proposals)"
        )
    if s.get("last_words"):
        print(f"  last words: {s['last_words'][:300]}")


def cmd_ledger(home: Home) -> None:
    for row in home.ledger():
        print(
            f"{row['at'][:16]}  {row['kind']:12s} {row['amount']:>9}  balance {row['balance']:>9}  {row['note']}"
        )


def cmd_journal(home: Home, limit: int) -> None:
    for row in home.journal_rows()[-limit:]:
        rest = {k: v for k, v in row.items() if k not in ("at", "kind")}
        print(f"{row['at'][:16]}  {row['kind']:12s} {json.dumps(rest, default=str)[:160]}")


def cmd_proposals(home: Home) -> None:
    items = home.proposals()
    if not items:
        print("(none)")
    for item in items:
        amount = f" ${item['amount_usd']}" if item.get("amount_usd") else ""
        print(f"#{item['id']} [{item['status']}] {item['kind']}{amount}: {item['summary']}")
        if item["status"] == "pending":
            print("    " + item["details"].replace("\n", "\n    "))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="moona.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("birth")
    p_run = sub.add_parser("run")
    p_run.add_argument("--forever", action="store_true")
    p_run.add_argument("--max-turns", type=int, default=None)
    sub.add_parser("status")
    sub.add_parser("ledger")
    p_journal = sub.add_parser("journal")
    p_journal.add_argument("--limit", type=int, default=40)
    sub.add_parser("inbox")
    sub.add_parser("memory")
    sub.add_parser("proposals")
    p_decide = sub.add_parser("decide")
    p_decide.add_argument("id", type=int)
    p_decide.add_argument("verdict", choices=["approve", "reject"])
    p_decide.add_argument("--spent", default=None)
    p_decide.add_argument("--note", default="")
    p_paid = sub.add_parser("paid")
    p_paid.add_argument("amount")
    p_paid.add_argument("--note", default="")
    p_sync = sub.add_parser("sync")
    p_sync.add_argument("balance")
    p_kill = sub.add_parser("kill")
    p_kill.add_argument("--reason", required=True)
    args = parser.parse_args(argv)

    cfg = Config.from_env()
    home = Home(cfg)
    try:
        if args.command == "birth":
            state = birth(home)
            print(f"{cfg.name} is born with ${state['balance']} in {home.root}.")
        elif args.command == "run":
            return run(home, forever=args.forever, max_turns=args.max_turns)
        elif args.command == "status":
            cmd_status(home)
        elif args.command == "ledger":
            cmd_ledger(home)
        elif args.command == "journal":
            cmd_journal(home, args.limit)
        elif args.command == "inbox":
            print(
                home.inbox_path.read_text(encoding="utf-8")
                if home.inbox_path.exists()
                else "(empty)"
            )
        elif args.command == "memory":
            print(home.memory() or "(empty)")
        elif args.command == "proposals":
            cmd_proposals(home)
        elif args.command == "decide":
            require_alive(home)
            spent = money(args.spent) if args.spent is not None else None
            item = home.decide(
                args.id, "approved" if args.verdict == "approve" else "rejected", args.note, spent
            )
            if spent:
                home.book("spend", -spent, f"proposal #{item['id']}: {item['summary']}")
                home.journal("spend", proposal=item["id"], amount=str(spent))
                check_alive(home, None)
            print(f"proposal #{item['id']} {item['status']}")
        elif args.command == "paid":
            require_alive(home)
            amount = money(args.amount)
            if amount <= 0:
                raise SystemExit("a payment is positive")
            balance = home.book("income", amount, args.note or "payment received")
            home.journal("income", amount=str(amount), note=args.note)
            print(f"booked ${amount}; balance ${balance}")
        elif args.command == "sync":
            require_alive(home)
            real = money(args.balance)
            delta = real - home.balance()
            balance = home.book("sync", delta, f"set to the real balance ${real}")
            home.journal("sync", balance=str(balance))
            print(f"balance ${balance}")
            check_alive(home, None)
        elif args.command == "kill":
            require_alive(home)
            die(home, f"killed by the owner: {args.reason}", None)
            print(f"{cfg.name} is dead.")
    except Dead as exc:
        print(str(exc))
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
