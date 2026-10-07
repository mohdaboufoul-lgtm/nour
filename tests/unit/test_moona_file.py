"""moona.py, the standalone agent: birth once, every turn is charged at the model's price, tools
act only inside her home, money moves only through the owner's commands, death at zero is final
and nothing runs afterwards. A scripted client stands in for the API; no network."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("moona_file", REPO_ROOT / "moona.py")
assert spec is not None and spec.loader is not None
moona = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = moona  # dataclasses resolve annotations through sys.modules
spec.loader.exec_module(moona)


class FakeClient:
    """Answers ``messages.create`` from a FIFO of scripted responses and records every request."""

    def __init__(self, responses: list[Any]) -> None:
        self.queue = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(copy.deepcopy(kwargs))  # the loop mutates its message list later
        if not self.queue:
            raise AssertionError("the fake client ran out of scripted responses")
        return self.queue.pop(0)


def usage(**fields: int) -> SimpleNamespace:
    base = {
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "server_tool_use": None,
    }
    base.update(fields)
    return SimpleNamespace(**base)


def reply(
    *blocks: dict[str, Any], stop: str = "tool_use", use: SimpleNamespace | None = None
) -> SimpleNamespace:
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=use or usage())


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


_ids = iter(range(1, 10_000))


def call(name: str, **args: Any) -> dict[str, Any]:
    args.setdefault("reason", "Because it moves money closer.")
    return {"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": name, "input": args}


def env_for(tmp_path: Path, **extra: str) -> dict[str, str]:
    base = {"MOONA_HOME": str(tmp_path / "home"), "MOONA_MODEL": "claude-opus-5-5"}
    base.update(extra)
    return base


@pytest.fixture
def home(tmp_path: Path) -> Any:
    cfg = moona.Config.from_env(env_for(tmp_path))
    home = moona.Home(cfg)
    moona.birth(home)
    return home


def test_birth_once(tmp_path: Path) -> None:
    cfg = moona.Config.from_env(env_for(tmp_path, MOONA_START_BALANCE="50"))
    home = moona.Home(cfg)
    state = moona.birth(home)
    assert state["alive"] and state["balance"] == "50.00" and state["turns"] == 0
    assert [row["kind"] for row in home.ledger()] == ["birth"]
    with pytest.raises(SystemExit, match="one life per home"):
        moona.birth(home)
    assert home.balance() == Decimal("50.00")


def test_unpriced_model_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="no price known"):
        moona.Config.from_env(env_for(tmp_path, MOONA_MODEL="claude-mystery-9"))
    cfg = moona.Config.from_env(
        env_for(
            tmp_path, MOONA_MODEL="claude-mystery-9", MOONA_PRICE_INPUT="1", MOONA_PRICE_OUTPUT="5"
        )
    )
    assert cfg.price_input == Decimal("1") and cfg.price_cache_read == Decimal("0.1")


def test_turn_cost_follows_the_price_table(tmp_path: Path) -> None:
    cfg = moona.Config.from_env(env_for(tmp_path))
    cost = moona.turn_cost(
        cfg,
        usage(
            input_tokens=100_000,
            output_tokens=10_000,
            cache_creation_input_tokens=20_000,
            cache_read_input_tokens=50_000,
            server_tool_use=SimpleNamespace(web_search_requests=3),
        ),
    )
    # 0.4 + 0.2 + 0.1 + 0.01 (cache read 50k x 0.20/M) + 3 searches x 0.01
    assert cost == Decimal("0.74")
    assert moona.turn_cost(cfg, usage(input_tokens=10, output_tokens=1)) == Decimal(
        "0.01"
    )  # never free
    assert moona.turn_cost(cfg, usage(input_tokens=0, output_tokens=0)) == Decimal("0")


def test_a_session_acts_pays_and_sleeps(home: Any) -> None:
    client = FakeClient(
        [
            reply(
                text("First, a product."),
                call("write_file", path="offer.md", content="# Translation, 24h turnaround"),
                call("remember", note="Day 1: wrote the offer."),
                call(
                    "propose",
                    kind="publish",
                    summary="List the offer on a freelance site",
                    details="Post offer.md as a gig; pay with [BANK DETAILS] on delivery.",
                    amount_usd=None,
                ),
            ),
            reply(call("sleep", hours=6), use=usage(input_tokens=2000, output_tokens=100)),
        ]
    )
    assert moona.run(home, forever=False, max_turns=None, client=client) == 0
    state = home.state()
    expected = (
        Decimal("50.00") - Decimal("0.01") - Decimal("0.01")
    )  # 1000 in + 200 out; 2000 in + 100 out
    assert home.balance() == expected and state["turns"] == 2 and state["sessions"] == 1
    assert (home.workspace / "offer.md").read_text(encoding="utf-8").startswith("# Translation")
    assert "Day 1: wrote the offer." in home.memory()
    proposals = home.proposals()
    assert (
        len(proposals) == 1
        and proposals[0]["status"] == "decided"
        and proposals[0]["kind"] == "publish"
    )
    assert state["sleep_until"] is not None
    kinds = [row["kind"] for row in home.journal_rows()]
    assert kinds[:2] == ["birth", "session"] and "sleep" in kinds and kinds.count("tool") == 4
    # the request shape: cached system prompt, strict client tools plus the web tools, effort
    request = client.requests[0]
    assert request["model"] == "claude-opus-5-5" and request["max_tokens"] == 8192
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert request["output_config"] == {"effort": "medium"}
    names = [tool.get("name") for tool in request["tools"]]
    assert {
        "write_file",
        "read_file",
        "list_files",
        "remember",
        "propose",
        "message_owner",
        "sleep",
    } <= set(names)
    assert "web_search" in names and "web_fetch" in names
    assert all(tool.get("strict") for tool in request["tools"] if "input_schema" in tool)
    assert "[BANK DETAILS]" in request["system"][0]["text"]
    # the second request carried the tool results and the status line in one user message
    second = client.requests[1]["messages"]
    assert second[1]["role"] == "assistant" and second[2]["role"] == "user"
    results = second[2]["content"]
    assert [block["type"] for block in results] == ["tool_result"] * 3 + ["text"]
    assert all(not block["is_error"] for block in results[:3])
    assert results[3]["text"].startswith("[status]")
    # asleep: another run does nothing and makes no request
    assert moona.run(home, forever=False, max_turns=None, client=client) == 0
    assert len(client.requests) == 2


def test_the_thought_she_cannot_pay_for_is_her_last(tmp_path: Path) -> None:
    cfg = moona.Config.from_env(env_for(tmp_path, MOONA_START_BALANCE="0.05"))
    home = moona.Home(cfg)
    moona.birth(home)
    client = FakeClient(
        [
            reply(
                text("I will build something grand."),
                call("write_file", path="plan.md", content="x"),
                use=usage(input_tokens=5000, output_tokens=5000),
            )
        ]
    )
    assert moona.run(home, forever=False, max_turns=None, client=client) == 3
    state = home.state()
    assert (
        not state["alive"] and state["cause"].startswith("starved") and state["balance"] == "0.00"
    )
    assert state["last_words"] == "I will build something grand."
    assert not (home.workspace / "plan.md").exists()  # the tool never ran
    assert home.ledger()[-1]["shortfall"] == "0.07"  # 0.02 + 0.10 asked, 0.05 paid
    assert (home.root / "EPITAPH.md").exists()
    assert moona.run(home, forever=False, max_turns=None, client=client) == 3  # dead: no request
    assert len(client.requests) == 1
    with pytest.raises(moona.Dead):
        moona.require_alive(home)


def test_money_moves_only_through_the_owner(home: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOONA_HOME", str(home.root))
    monkeypatch.setenv("MOONA_MODEL", "claude-opus-5-5")
    pid = home.propose(
        "pay", "Buy a domain", "namecheap.com, moona-writes.com, 1 year", Decimal("12.50")
    )
    assert moona.main(["done", str(pid), "--spent", "12.50", "--note", "done with the card"]) == 0
    assert home.balance() == Decimal("37.50")
    assert moona.main(["paid", "40", "--note", "client X, invoice 1"]) == 0
    assert home.balance() == Decimal("77.50")
    assert moona.main(["sync", "70.10"]) == 0
    assert home.balance() == Decimal("70.10")
    kinds = [row["kind"] for row in home.ledger()]
    assert kinds == ["birth", "spend", "income", "sync"]
    with pytest.raises(SystemExit, match="already done"):
        moona.main(["refuse", str(pid), "--note", "too late"])
    # her next session is told what was carried out, once
    opening = moona.opening_message(home, home.state())
    assert f"#{pid} Buy a domain -> done, spent $12.50: done with the card" in opening
    assert f"#{pid}" not in moona.opening_message(home, home.state())
    assert moona.main(["kill", "--reason", "experiment over"]) == 0
    assert not home.state()["alive"] and moona.main(["status"]) == 0
    assert moona.main(["paid", "5"]) == 3  # dead: refused


def test_a_purchase_is_her_decision_within_her_balance(
    home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = home.state()
    # more than the card holds: refused by the tool itself, nothing is recorded
    text_out, is_error, _ = moona.run_tool(
        home,
        "propose",
        {
            "reason": "r",
            "kind": "pay",
            "summary": "A laptop",
            "details": "any shop",
            "amount_usd": 50.01,
        },
        state,
    )
    assert is_error and "you cannot afford that" in text_out and home.proposals() == []
    # within it: decided the moment she writes it, and the owner only carries it out
    text_out, is_error, _ = moona.run_tool(
        home,
        "propose",
        {
            "reason": "r",
            "kind": "sign_up",
            "summary": "A freelance account",
            "details": "site X, username moona, pay with [BANK DETAILS]",
            "amount_usd": None,
        },
        state,
    )
    assert not is_error and text_out.startswith("decided: #1")
    assert home.proposals()[0]["status"] == "decided"
    assert "#1" in moona.opening_message(home, home.state())  # decided, not yet carried out
    monkeypatch.setenv("MOONA_HOME", str(home.root))
    monkeypatch.setenv("MOONA_MODEL", "claude-opus-5-5")
    assert moona.main(["refuse", "1", "--note", "the site needs a passport"]) == 0
    assert home.proposals()[0]["status"] == "refused" and home.balance() == Decimal("50.00")
    opening = moona.opening_message(home, home.state())
    assert "#1 A freelance account -> refused: the site needs a passport" in opening
    assert home.journal_rows()[-1]["kind"] == "refused"
    with pytest.raises(SystemExit, match="no proposal 7"):
        moona.main(["done", "7"])


def test_tools_stay_inside_her_home(home: Any) -> None:
    state = home.state()
    text_out, is_error, _ = moona.run_tool(
        home, "write_file", {"reason": "r", "path": "../escape.txt", "content": "x"}, state
    )
    assert is_error and "inside the workspace" in text_out
    assert not (home.root / "escape.txt").exists()
    text_out, is_error, _ = moona.run_tool(
        home, "write_file", {"path": "a.txt", "content": "x"}, state
    )
    assert is_error and "reason" in text_out
    text_out, is_error, _ = moona.run_tool(
        home, "read_file", {"reason": "r", "path": "missing.txt"}, state
    )
    assert is_error
    text_out, is_error, until = moona.run_tool(home, "sleep", {"reason": "r", "hours": 999}, state)
    assert not is_error and until is not None and "24h" in text_out  # capped
    text_out, is_error, _ = moona.run_tool(
        home,
        "propose",
        {"reason": "r", "kind": "pay", "summary": "s", "details": "d", "amount_usd": -3},
        state,
    )
    assert is_error


def test_silence_and_refusal_end_the_session(home: Any) -> None:
    client = FakeClient(
        [reply(text("Hmm."), stop="end_turn"), reply(text("Still thinking."), stop="end_turn")]
    )
    assert moona.run_session(home, client) == "silence"
    assert len(client.requests) == 2 and home.state()["turns"] == 2
    assert client.requests[1]["messages"][-1]["content"].startswith("No action was taken.")
    client = FakeClient([reply(text("No."), stop="refusal")])
    assert moona.run_session(home, client) == "refusal"
    assert home.journal_rows()[-1]["kind"] == "refusal"
    assert home.balance() < Decimal("50")  # every turn was paid for


def test_session_turn_limit_asks_her_to_sleep(tmp_path: Path) -> None:
    cfg = moona.Config.from_env(env_for(tmp_path, MOONA_SESSION_TURNS="2"))
    home = moona.Home(cfg)
    moona.birth(home)
    client = FakeClient(
        [reply(call("list_files")), reply(call("remember", note="bye"), call("sleep", hours=1))]
    )
    assert moona.run_session(home, client) == "sleep"
    last_user = client.requests[1]["messages"][-1]
    assert (
        last_user["role"] == "user" and "This session ends after this turn" in last_user["content"]
    )


def test_daily_cost_is_charged_once_per_day(tmp_path: Path) -> None:
    cfg = moona.Config.from_env(env_for(tmp_path, MOONA_DAILY_COST="1.00"))
    home = moona.Home(cfg)
    moona.birth(home)
    state = home.state()
    state["last_daily_cost"] = (moona.utcnow() - timedelta(days=3)).date().isoformat()
    home.save_state(state)
    moona.daily_cost(home)
    assert home.balance() == Decimal("47.00")
    moona.daily_cost(home)
    assert home.balance() == Decimal("47.00")
    assert "Staying alive costs $1.00 a day" in moona.system_prompt(cfg)


def test_the_prompt_states_the_contract(tmp_path: Path) -> None:
    cfg = moona.Config.from_env(env_for(tmp_path, MOONA_PRONOUNS="he/him", MOONA_NAME="Moona"))
    prompt = moona.system_prompt(cfg)
    for needle in (
        "lives on what he earns",
        "$50.00",
        "claude-opus-5-5",
        "$4.00 per million input tokens",
        "When the balance reached zero you die".replace("reached", "reaches"),
        "Nothing illegal",
        "never claim to be human",
        "No debt",
        "[BANK DETAILS]",
        "nobody approves it",
        "the card holds nothing more",
        "sleep, which is free",
    ):
        assert needle in prompt, needle
    assert json.dumps(moona.tool_definitions(cfg))  # serialisable as sent
