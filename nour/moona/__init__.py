"""Moona: one self-funded sub-agent who must earn his own keep (SPEC §7 "Sub-agents (research,
sales, ops) with their own budgets and kill switches"; §12 ``actor`` "a sub-agent"; §13 "sub-agents
have their own caps and kill switches"; docs/MOONA.md).

Moona is born with one wallet holding exactly the seed the owner gives him (USD 50.00 in
``config/moona.yaml``) and nothing else, ever: no top-up, no credit, no overdraft. Every model
call he makes is metered and debited from that wallet, a cost of living is debited once a day,
and the only money that ever enters is a settled payment for work he delivered. When the wallet
reaches zero he dies: a single, irreversible transition after which nothing he could do executes.
The owner may freeze him (a pause) or kill him (terminal); nobody, the owner included, can bring
him back.

The walls, in the house style (DESIGN §4):

* **The wallet is the cap, and the cap is not his judgement.** ``Wallet.debit`` accepts only a
  ``WalletAuthorization`` the wallet itself issued (``Wallet.authorize``, which declines when the
  amount exceeds the balance, when he is frozen and when he is dead), ``Wallet.credit`` accepts
  only a ``PaymentReceipt`` a ``PaymentPort`` settled, and ``Wallet.charge`` (model cost, upkeep)
  can never take the balance below zero: what cannot be paid is recorded as his shortfall and is
  the last thing that happens to him.
* **Death is a type.** ``Life.die`` is a single transition on a row the database refuses to
  delete or replace; every entry point checks ``Life.is_alive`` first and raises ``NotAlive``.
* **Everything he does is journaled** before and after it happens, hash-chained and append-only
  (``Journal.span``), with ``Actor.SUBAGENT`` on every row, and every side-effecting port call
  carries the ``PortCall`` the span minted (SPEC §12 §16).
* **Observed content is data.** Marketplace requests and client messages reach the model only
  inside ``<observed … authority="data">`` fences after ``LeakGuard.redact``; what the
  ``InjectionScanner`` finds there is journaled and disables every money-moving tool for that
  tick (DESIGN §4e, rule 8 in miniature).
* **Honesty is enforced, not requested.** Every outbound text carries his disclosure line and is
  refused when it claims he is human; a spend at a gambling, adult or tobacco merchant is refused
  before any port is reached (constitution hard rules 5 and 6).

Placement: ``nour.moona`` is a wave-2 package (DESIGN §8): it imports ``nour.core``,
``nour.config``, ``nour.db``, ``nour.fakes`` and ``nour.language`` and nothing later. It is the
phase-3 sub-agent body (ROADMAP P3.5) landed early as a standalone package on its own store and
its own ports; when wave 4 lands, ``SubAgentRunner`` mounts him under the Operator desk. Nothing
here touches a live channel: the simulation runs on the fakes in ``nour.moona.fakes``, and a live
run needs the adapters named in ``config/moona.yaml`` (none exist yet) and an explicit owner
flag (docs/MOONA.md §6).
"""
