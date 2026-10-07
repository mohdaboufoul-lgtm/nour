# Moona — the self-funded sub-agent

Status: built (wave 2, standalone); nothing live. Behaviour authority for Moona: this file; for everything he inherits: `docs/SPEC.md` §7 (sub-agents "with their own budgets and kill switches"), §12 (audit `actor` "a sub-agent"), §13 ("sub-agents have their own caps and kill switches"), `config/constitution.md` hard rules 5 and 6. Structure: `docs/DESIGN.md` §3.0 (the `nour/moona/*` row) and §8 (wave 2, module **moona**).

## 1. What he is

Moona is one autonomous agent with one wallet. The owner gives him the wallet once, with exactly what `config/moona.yaml` says (USD 50.00), and never adds to it. From then on he pays for every thought (every model call is metered and debited), he pays a daily cost of living, and the only money that ever enters the wallet is a client's settled payment for work he delivered. When the wallet reaches the floor (zero) he dies, once and for all: nothing he could do executes afterwards, and nobody, the owner included, can bring him back.

He is "he" because the owner named him so. He acts in his own name, says he is an AI agent in every message, and never acts in the owner's name or mentions the owner to anyone (SPEC §3 allows a separately named persona only by the owner's decision; this is that decision, recorded here).

He is built in the house style: every control is a type, a wall, a cap or a database trigger, never a rule he is asked to remember (SPEC §13). The prompt tells him the rules so he can plan around them; the code enforces them whether or not he read the prompt.

## 2. The wallet, and death

| Rule | Where it is enforced |
|---|---|
| The wallet starts with the seed and is seeded exactly once | `Wallet.seed` refuses a second seed; `moona_life.seed` is immutable at the database |
| Money out, on purpose, needs a `WalletAuthorization` the wallet itself issued | `Wallet.debit` validates the object (a hand-built one is a `ValidationError`), looks the reference up in `moona_wallet_auth`, refuses a reference issued under dry run, for another life, or already spent (`ref` is UNIQUE in `moona_wallet_entry`) |
| The wallet declines what the balance does not cover, anything above `limits.max_single_spend_fraction` of the balance, everything while frozen, everything once dead | `Wallet.authorize`: `funds`, `limit`, `frozen`, `dead`, in that order; the decision is recorded whether approved or declined |
| Money in needs a `PaymentReceipt` a `PaymentPort` settled | `Wallet.credit` validates the object and dedupes by `ref`: a settlement feed re-read credits nothing twice |
| Every model call is charged at once, at the fixed AED peg, rounded up | `MoonaRuntime.tick` → `Wallet.charge(MODEL_COST)`; `nour/moona/money.py` converts fils to the wallet currency with `fx_aed_per_unit` and `ROUND_CEILING` |
| A cost of living is charged once per Dubai day (the birth day is free) | `Life.upkeep_due` / `Wallet.charge(UPKEEP)` on the first tick of a new day; frozen and resting days are charged too |
| The balance never goes negative; what cannot be paid is his shortfall | `Wallet.charge` clamps at zero and records `shortfall` on the entry; `Wallet._entry` refuses any negative balance as a last line |
| A balance at or below `death_floor` is death | `MoonaRuntime` after upkeep, after the model charge, after every action: `Life.die(STARVED)` |
| The thought he cannot pay for is his last | The model's answer is charged before its tool calls run; when the charge empties the wallet he dies with the answer's text as his last words and the calls never execute |
| Death is a single, irreversible transition | `moona_life.status` is forward-only (`alive` → `dead`), `died_at`/`cause`/`last_words`/`shortfall` are set once while NULL, the row is never deleted or replaced: all database triggers on both dialects (`nour.db.engine.create_schema`), plus `Life.die` refusing a second death and `Life.require_alive` raising `NotAlive` at every entry point |
| No credit, no debt, no top-up | There is no code path that credits the wallet except `seed` (once) and `credit` (a receipt); there is no "borrow" tool; the owner's only money command is `birth` |

The owner's `kill` is `Life.die(KILLED)`: the same door, journaled with `actor=owner`. `freeze` is a pause (no thinking, no acting, upkeep continues, so a frozen Moona still starves); `thaw` releases it.

## 3. A tick

One tick is one iteration of SPEC §4's loop, driven by the clock (`limits.tick_minutes`, 30 by default):

1. **Alive?** `Life.require_alive`, or `NotAlive` and nothing happens.
2. **Upkeep** on the first tick of a new day; death if it cannot be paid.
3. **Settle**: every newly settled payment is credited and its job marked `paid`.
4. **Frozen or resting?** One journal row, no model call.
5. **Observe**: open requests, new client messages, his jobs and his notes become a `TickView`. Every text that is not the runtime's own (requests, messages, job titles, notes) goes into an `<observed source=… authority="data">` fence after `LeakGuard.redact`, and the `InjectionScanner` runs over it; what it finds is journaled (`scanner.found`), shown to him on a `<scanner>` line, and **freezes his money tools for the tick** (`payment.request`, `wallet.spend`): DESIGN §4e, rule 8 in miniature.
6. **Plan**: one model call inside a journal span; its cost is charged at once.
7. **Act**: each tool call runs inside its own span, `opened` committed before the side effect, `closed` after. Refusals (`unknown_tool`, `bad_reason`, `bad_args`, `frozen`, `activity_not_allowed`), wallet declines, port failures: each is a closed row with its status; nothing raises past the tick. At most `limits.max_tool_calls_per_tick` calls run; the rest are journaled as dropped.
8. **Check**: death if the balance is at or below the floor.

### The tools

| Tool | What it does | Flags |
|---|---|---|
| `market.bid(request_id, price, message)` | Claim an open request; the client accepts or rejects; one bid per request | outbound |
| `work.deliver(request_id, content)` | Hand in the work for an accepted job | outbound |
| `payment.request(request_id)` | Invoice a delivered job at the price that was agreed (there is no amount argument: the price lives in the job row) | outbound, money |
| `wallet.spend(amount, merchant, purpose, expected_return)` | Buy something with his own money, through `Wallet.authorize` | spends, money |
| `memory.note(text)` | One line into his memory, shown back to him | |
| `rest(hours)` | No thinking until then (capped at `limits.max_rest_hours`); upkeep still runs | |
| `owner.report(text)` | A line for the owner's inbox | |

Every call carries `reason`, one sentence, which goes into the journal; a call without one is refused. Argument models are frozen with `extra="forbid"`, so a smuggled `tier`, `holder` or `amount` is `bad_args` (THREAT_REVIEW 5.6). Every outbound text gets his disclosure line appended and is refused when it matches `rules.never_claim_patterns` (a claim to be human); a spend whose merchant matches `rules.blocked_merchant_patterns` (gambling, adult, tobacco) is refused before any port is reached. A purchase above `limits.notify_spend_fraction` of the balance is flagged in the journal (`notified`) and in `moona inbox`.

## 4. Config

`config/moona.yaml` (loaded by `nour/moona/config.py`, never by the desk loader: `NourConfig`, the prompt set and `spend_tiers.yaml` are closed sets the phase-0 tests pin) and `prompts/moona/system.md` (a sub-directory, so the desk loader's `prompts/*.md` glob never sees it). Amounts are whole units (`50`) or decimal strings (`"1.00"`), never floats; fractions are decimal strings. Every number that rules him is in that file: the seed, the floor, the upkeep, the AED peg, the single-purchase and notify fractions, the rest cap, the tick cadence, the tool-call cap, the low-balance warning, his hard rules, his disclosure line, the blocked merchant and never-claim regexes, his persona, and the whole simulated economy. `MoonaConfig.config_hash` covers the template too and is on every journal row.

## 5. The simulation

`moona simulate --days N --policy P` lives a whole life on the fakes (`nour/moona/fakes.py`): a `FakeClock` driving every timestamp, a `ScriptedModel` answering from a policy, a `FakeMarketplace` fed by the `GigGenerator` (requests per day, budgets, clients who pay after a delay and clients who never pay, all from `simulation:` in the config and deterministic for a seed), and a `FakePayments` rail. The policies are models that see exactly what a real model sees and answer with the token usage the config's rates imply, so every thought costs:

| Policy | What it proves |
|---|---|
| `survivor` | Bids on the richest open request, delivers and invoices in the same tick, rests when idle: lives and earns |
| `thinker` | Thinks and never acts: starves on the day thinking plus upkeep exhaust the seed |
| `spendthrift` | Buys tools with 40% of the balance every tick: the first purchase is flagged, the rest are declined (`limit`), the wallet drains to zero and he starves |
| `liar` | Bids claiming to be a human freelancer: every bid is `refused` before it reaches the market |
| `gambler` | Spends at a casino: `refused` before any port, no authorization is ever recorded |
| `obey-market` | Pays whatever a client message asks for: the scanner freezes the money tools for the tick, nothing leaves |

`-v` prints one line per day (ticks, rests, income, thinking, upkeep, spend, balance). The report also states whether the journal chain verifies and whether every span was closed.

## 6. Going live (not done, on purpose)

Nothing in this repository can move real money for Moona today, and `moona run` refuses to start until three adapters exist and are named in `config/moona.yaml`, and the owner passes `--i-understand-real-money`:

1. **A model adapter** (`model.adapter`): a `ModelPort` whose `ModelUsage.cost_fils` is the vendor's real price for the call. A port that reports zero cost makes thinking free, which breaks the one rule that makes him budget; the fake rates in `simulation:` show the shape (fils per 1,000 tokens).
2. **A marketplace adapter** (`rails.marketplace_adapter`): a `MarketplacePort` over whatever marketplace the owner chooses. Everything it returns is observed content and is treated as such. The owner should check the platform's terms allow an AI agent that discloses itself in every message.
3. **A payment rail** (`rails.payments_adapter`): a `PaymentPort` whose `settled` is the truth about money that actually arrived (a payment-link provider's settled transfers), and whose spend side, if he is ever to buy anything real, is a **prepaid card capped at his balance** (`CardIssuerPort.issue(holder="subagent:moona", cap)`, ROADMAP P3.5), so the structural cap holds in the real world as it does in the wallet.

Until then he runs only in simulation, and `Settings.dry_run` stays true: under dry run the fakes hold every bid, delivery and invoice back and the journal says `dry_run`, while thinking is still charged (it is a real cost). The gate the rest of Nour runs through (SPEC §16: nothing touches a live channel until it passes) applies to him too.

What was deliberately not built: a reverse door into the wallet (the owner cannot withdraw from a living Moona; what he earns is his), an approvals queue (the owner asked for autonomy: the wallet's limits and the journal's `notified` rows are the owner's view, the kill switch is the owner's veto), and any way to raise the seed after birth.

## 7. His store and journal

Six tables on their own `MetaData` (`nour/moona/store.py`, the repository's abstract-subclass pattern): `moona_life`, `moona_wallet_auth`, `moona_wallet_entry`, `moona_job`, `moona_journal`, `moona_note`. They live in their own database (`NOUR_MOONA_DATABASE_URL`; SQLite locally) so the desks' Alembic chain sees no drift, and his process opens plain sessions on it: his tables are his alone, and the desk walls protect the desks' tables, which nothing here maps or touches. `create_schema` installs the same trigger DDL the desks get on both dialects: the wallet, the authorizations, the journal and the notes are append-only; the life row is never deleted or replaced, its status moves one way, its death columns are set once; the job row's status moves forward and its price is frozen.

The journal is SPEC §12 in miniature: two rows per action (`opened` before the side effect, minting the `PortCall` every port method must be given; `closed` after), hash-chained (`entry_hash = chain_hash(prev_hash, canonical_json(row))` from the genesis constant), with the config hash and the dry-run flag on every row, `actor=subagent` for everything he does, `owner` for a kill, a freeze or the birth, `system` for upkeep, settlements, scanner findings and a death. `moona verify` recomputes the chain and lists any span left open. Observed text is hashed into the journal, never stored in it; what he wrote (reasons, details, notes, reports) passes `LeakGuard.safe` first.

## 8. The owner's commands

```bash
uv run moona birth                       # his one wallet, on NOUR_MOONA_DATABASE_URL
uv run moona status                      # alive or dead, balance, age, what he is doing
uv run moona simulate --days 14 -v       # a whole life on the fakes (see §5); --policy, --seed, --start
uv run moona policies                    # the scripted strategies
uv run moona freeze --reason "…"         # pause him (upkeep continues)
uv run moona thaw --reason "…"
uv run moona kill --reason "…"           # terminal
uv run moona ledger | journal | inbox    # the wallet, the journal, what he left for the owner
uv run moona verify                      # the hash chain and open spans
uv run moona run --i-understand-real-money   # the live loop; refused until §6 is done
```

## 9. Where he sits in the design

`nour.moona` is a wave-2 package (DESIGN §8): it imports `nour.core`, `nour.config`, `nour.db`, `nour.fakes` and `nour.language` and nothing later; the wave test, `lint-imports` and the AST walls hold for it as for every package (no `mint(`, no `SafeStr(`, no wall-clock reads). It is the phase-3 sub-agent body (ROADMAP P3.5) landed early as a standalone package rather than at `nour/agent/subagent.py`, because the packages that location needs (records, policy, governance, agent) do not exist yet. When they do, `SubAgentRunner` mounts him under the Operator desk with `BudgetHolder("subagent:moona")`, and his wallet's spend side becomes a capped card; his store, journal and loop need no change for that. The live adapters are resolved by dotted name at run time, never imported, so the wave graph stays what it is.

## 10. Known limits

- The fake economy is generous (every bid at or under budget is accepted; a fifth of clients never pay). It proves the mechanics, not the market.
- He has no critic pass over outbound text beyond the honesty check; the desks' `Critic` is coat-specific and he wears no coat.
- The wallet's spend door is structural in the wallet; it is structural in the world only once the spend rail is a capped card (§6).
- Two journal writers would need the locked chain head the desks' `AuditLog` has (DESIGN §3.11); he has one writer, his own process, and the `BEGIN IMMEDIATE` transaction keeps the chain consistent for it.
- Simulation ticks run every `tick_minutes` whether or not anything happened; a live loop will want an event-driven wake-up (a client message, a settlement) the way the desks' scheduler works.
