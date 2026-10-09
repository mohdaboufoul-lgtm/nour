# Card issuer adapter (`CardIssuerPort`)

Engineering reference for the capped card in SPEC §2 (hard rules), §6 (Tier 3: card numbers never
held), §10 (one card per desk, AED 3,000/month Operator test capital, AI-model budget line inside the
Operator cap, maker-checker, ledger records every dirham), §12 (kill switch freezes both cards),
§13 (runaway spend: card caps) and §16 (phase 0 gate: "card declines above cap").
Researched 2026-10-02. Each claim is tagged **[V]** verified against the cited official page,
**[P]** partly verified (official page summarised by search, or a third-party page), or **[U]** unverified.

## 1. The rule the adapter exists to keep

"Spending caps are enforced by the card, not by judgment" (§2). The invariant is: *no matter what the
model does, the issuer's authorization engine will not approve more than the cap in a calendar month.*
A provider therefore qualifies only if it has **all** of:

| # | Requirement | Why |
|---|---|---|
| R1 | Per-card monthly amount limit settable **by API**, enforced at authorization by the issuer | §2, §10, §16 gate |
| R2 | Freeze and unfreeze by API, taking effect on the next authorization | §12 kill switch, §13 watchdog |
| R3 | Transaction feed by API (authorizations, captures, refunds, declines) with stable ids | §10 ledger, §12 auditor |
| R4 | Card number never returned to Nour's process in the normal path (masked / tokenised / iframe) | §6 Tier 3 |
| R5 | Several cards under one account, each with its own limit | one per desk + AI-model sub-line |
| R6 | Operable by a UAE company (licensed in the UAE, or legally usable from it) | §14 UAE rails |
| opt | Real-time authorization webhook (approve/decline in ~2 s) | second layer: MCC, per-task budget, kill switch |
| opt | AED card currency | avoids FX drift against an AED cap |

A provider with a cap that exists only in a dashboard, or only as a *soft* alert, does **not** qualify
(see §6 below). AED/USD: the dirham is pegged at 3.6725 per USD [U, common knowledge]; a USD card
with a 816 USD monthly limit is an acceptable stand-in for AED 3,000 if no AED issuer qualifies.

## 2. Provider survey

| Provider | UAE | R1 API monthly cap | R2 API freeze | Real-time auth hook | R4 PAN hidden | R5 multi-card | AED | Verdict |
|---|---|---|---|---|---|---|---|---|
| Stripe Issuing | No (22 countries: EU/EEA, GB, CA) [V] | Yes, `monthly` [V] | Yes, `status=inactive` [V] | Yes, 2 s [V] | Yes unless `expand[]=number` [V] | Yes [V] | No (usd/eur/gbp) [V] | Reference API design; not usable by a UAE entity today |
| Marqeta | Not listed (US, CA, EU, AU/NZ/SG/PH/TH) [P] | Per user/card product, `MONTH` [V] (not per card) | Yes, `SUSPENDED` [V] | Yes, JIT gateway, 3 s [V] | Via Marqeta.js widget [P] | Yes [V] | Unknown [U] | Not usable from UAE; useful as a model |
| Airwallex | In-principle CBUAE SVF + RPS Cat II (Sep 2025), corporate cards planned [V]; go-live "later in 2026" [P] | Yes, `MONTHLY` [V] | Yes, `INACTIVE`/`ACTIVE` [V] | Yes, 2.5 s [V] | Yes; PAN needs PCI or iframe [V] | Yes [V] | Not in listed control currencies [V] | **Best candidate once UAE entity is live**; re-check |
| Revolut Business | Consumer licences only (Jun 2026); business not announced [P] | Yes (API spend limits) [P] | Yes freeze/unfreeze [P] | Not found [U] | Not fetched [U] | Yes (200/member) [P] | n/a | Not usable from UAE yet |
| NymCard (nCore) | Yes: CBUAE RPSCS Cat II, Open Finance, SVF in-principle (Sep 2026) [V] | Yes, `/velocitylimits` `period: MONTHLY` [V] | Yes, Suspended reversible [V]; endpoint not located [U] | Not found in public docs [U] | PCI widget / LUT iframe [V] | Yes [V] | Yes (AED examples) [V] | Qualifies technically; it is a B2B processor (you run a card programme) |
| Alaan | Yes; Visa card issued by NymCard Payment Services LLC [V] | App: daily/monthly limits [P] | App [P] | — | — | Yes [P] | Yes | **No public card-control API found** [U] → does not qualify today |
| Qashio | Yes; cards issued by NymCard Payment Services LLC [V] | App: per txn/day/month, lock/unlock [V] | App [V] | — | — | Yes | Yes | No public API found [U] → does not qualify today |
| Pemo | Yes; Mastercard [V] | App: daily/weekly/monthly/yearly cycle limit, declines at limit [V] | App [P] | — | — | Yes | Yes | API is **read-only** (transactions, expenses, receipts) [V] → does not qualify for R1/R2 |
| Mamo Business | Yes | `POST /partner_cards`, `PATCH /partner_cards/{id}` exist [P]; limit fields unverified [U] | `PATCH .../cancel` (cancel, not freeze) [P] | — | Unknown [U] | Yes | Yes | Possible; needs the OpenAPI spec and a sandbox test |
| Wio Business | Yes (bank) | App only; developer portal is Open Banking AIS/PIS [P] | App | — | — | Yes ("as many as you need") [V] | Yes | No card-control API → does not qualify |

Notes and sources:

- **Stripe Issuing.** Local issuing in AT BE BG CA CY DE EE ES FI FR GB GR HR IE IT LT LU LV MT NL PT SI SK; cards
  denominated in USD/EUR/GBP; cross-border programmes are for US multinationals or stablecoin cards in LatAm/Africa
  (https://docs.stripe.com/issuing/global). `spending_controls.spending_limits[]{amount, interval, categories}` with
  `interval` in `per_authorization | daily | weekly | monthly | yearly | all_time`; `monthly` starts "on the 1st at
  midnight UTC"; cardholder-level limits apply across all of a cardholder's cards; aggregation is best-effort with up
  to 30 s lag; default 500 USD/day if none set; unconfigurable 10,000 USD per authorization
  (https://docs.stripe.com/issuing/controls/spending-controls, https://docs.stripe.com/api/issuing/cards/create).
  `status` is `active | inactive | canceled`; `inactive` declines with `card_inactive` and is reversible; `canceled`
  is permanent (https://docs.stripe.com/api/issuing/cards/object). `number`/`cvc` are returned only on Retrieve with
  `expand`, never on list (same page). Real-time `issuing_authorization.request` must be answered in 2 s or Stripe
  applies the account's timeout setting (https://docs.stripe.com/issuing/controls/real-time-authorizations).
  Agent-specific guidance, single-use cards via `lifecycle_controls` (https://docs.stripe.com/issuing/agents).
  Expansion to "100 countries" was announced at Sessions 2026 [P, third-party coverage]; the docs still list 22.
- **Marqeta.** `/velocitycontrols` `{amount_limit, velocity_window: DAY|WEEK|MONTH|LIFETIME|TRANSACTION, usage_limit,
  association: {user_token | card_product_token}, merchant_scope: {mcc|mcc_group|mid}, currency_code}` — scoped to a
  user or card product, not a single card (https://www.marqeta.com/docs/core-api/velocity-controls). Card states
  `UNACTIVATED | ACTIVE | SUSPENDED | TERMINATED` via `POST /cardtransitions {card_token, state, channel}`
  (https://www.marqeta.com/docs/core-api/card-transitions). JIT gateway: no answer in 3 s → decline to network
  (https://www.marqeta.com/docs/developer-guides/managing-timeouts). Decline codes 1834 "Exceeds withdrawal amount
  limit", 1817 count limit, 1841 per-auth limit, 1003 "Card suspended", 1832 MCC/MID restriction
  (https://www.marqeta.com/docs/developer-guides/mq-eu-transaction-decline-codes). Countries: community thread
  https://community.marqeta.com/t5/discussions/which-countries-does-marqeta-support/m-p/729 [P].
- **Airwallex.** Create/update card: `authorization_controls.transaction_limits.limits[]{amount, interval:
  PER_TRANSACTION|DAILY|WEEKLY|MONTHLY|ALL_TIME}`, `allowed_merchant_categories` (MCCs), `allowed_merchant_countries`,
  `blocked_transaction_usages`; `MONTHLY` "begins on the 1st of the month", weekly Sunday 00:00 UTC; update accepts
  `card_status: INACTIVE|ACTIVE|CLOSED`; responses carry a masked number
  (https://www.airwallex.com/docs/api/issuing/cards/create, .../update,
  https://airwallex.com/docs/issuing__authorization-controls__transaction-limits). Remote authorization: respond in
  2.5 s with `response_status: AUTHORIZED|DECLINED`; on timeout "the configured default action will be performed"
  (https://www.airwallex.com/docs/issuing__remote-authorization__respond-to-authorization-requests). Webhooks
  `issuing.card_transaction.authorized|cleared|reversed|expired|declined`, `issuing.transaction.succeeded|failed`,
  `issuing.card.active|inactive|blocked|closed`, `issuing.card.low_remaining_transaction_limit`
  (https://www.airwallex.com/docs/developer-tools/webhooks/listen-for-webhook-events/issuing). Full PAN only via
  the PCI-scoped details API or a secure iframe
  (https://www.airwallex.com/docs/issuing/manage-cards/retrieve-sensitive-card-details). UAE: in-principle approval
  announced 25 Sep 2025 (https://www.airwallex.com/newsroom/airwallex-accelerates-market-entry-in-the-middle-east-with-multiple).
- **Revolut Business.** developer.revolut.com returned 403 to automated fetches; facts above come from search
  summaries of https://developer.revolut.com/docs/guides/manage-accounts/cards/manage-cards (cards API needs
  support enablement; "available in the UK, US and the EEA"). UAE: CBUAE SVF/RPS licences 17 Jun 2026, retail focus
  (https://www.agbi.com/banking-finance/2026/06/revolut-cleared-for-uae-launch-after-central-bank-approval/) [P].
- **NymCard.** Velocity limits `POST /velocitylimits {type, period: DAILY|MONTHLY|YEARLY|LIFETIME, max_amount,
  currency (AED/USD), transaction_scope}` linked via `POST /cardproducts/{id}/velocitylimits:link`; per-card linking
  is mentioned but not documented on the page (https://docs.nymcard.com/get-started/product-management/velocity-limits).
  Allowed/blocked MCC lists `/allowedmccs` (max 1,000), merchant lists per card product or card
  (https://docs.nymcard.com/get-started/product-management/authorization-controls). Card statuses Inactive/Active/
  Suspended/Terminated, "temporarily suspended and can be resumed" (https://docs.nymcard.com/get-started/issuance/cards).
  Webhooks `TRANSACTION`, `EXPIRED_AUTH`, `CARD_STATUS_CHANGE` (https://docs.nymcard.com/get-started/webhooks/ncore-webhook-events).
  PCI widget: LUT + iframe so "sensitive data is never stored, processed and broadcasted through your systems"
  (https://docs.nymcard.com/get-started/security/pci-widget). Licences: https://nymcard.com/company/newsroom/nymcard-receives-in-principle-approval-cbuae-stored-value-facility-licence.
- **Alaan / Qashio / Pemo / Wio / Mamo.** Alaan: https://www.alaan.com/lp/custom-controls (NymCard-issued Visa).
  Qashio: https://qashio.com/features/spend-controls-and-limits (NymCard-issued; lock/unlock, per txn/day/month).
  Pemo: https://help.pemo.io/wallet-and-cards/pemo-cards/about-your-card-spending-limits and
  https://help.pemo.io/en/articles/13600225-pemo-api-overview (API retrieves data only). Wio: https://wio.io/business,
  developer portal https://ob.business.wio.io (open banking). Mamo cards API listing:
  https://apis.apievangelist.com/store/mamo-cards-api/ (third party) [P].

**Recommendation.** Build against the Protocol in §4 with the fake. For phase 0 (§16) use whichever UAE provider
passes the qualification test in §6 at the time; today the only UAE-licensed path with documented API caps is a
NymCard-backed programme (direct, or through a spend platform if one exposes its API). Re-check Airwallex UAE and
Mamo before phase 2; Stripe is the design reference but not an option for a UAE entity.

## 3. Authorization and transaction data model

Lifecycle (network terms; Stripe names in brackets, Airwallex in braces):

1. **Authorization** — merchant asks the issuer to hold an amount. Issuer checks card status, balance and controls,
   optionally asks the real-time webhook, then approves or declines. [`issuing_authorization.request` →
   `issuing_authorization.created`, `status: pending`] {`issuing.card_transaction.authorized|declined`}.
   Incremental authorizations re-use the same object with a new `pending_request`; partial authorizations set
   `is_amount_controllable`. A hold expires if never captured [`status: expired`] {`...expired`}.
2. **Capture / clearing** — merchant submits the final amount, usually within 24 h, up to 31 days for hotels,
   airlines, car rental. A Transaction of `type: capture` is created, the authorization closes. Captures of an
   approved authorization **always succeed**: spending controls, the webhook and card status do not run at capture.
   Over-capture (tips, rentals, ground transport) and force capture (offline terminals, or a merchant capturing
   despite a decline) cannot be blocked, only disputed (https://docs.stripe.com/issuing/purchases/transactions).
3. **Settlement** — funds move between issuer and acquirer; the ledger-relevant event is the capture record with
   its final `amount` in card currency and `merchant_amount`/`merchant_currency` (network daily FX rate).
4. **Refund / reversal** — Transaction `type: refund` (positive amount; a refund reversal is negative), possibly
   unlinked to any authorization. Refunds post even to inactive or canceled cards.
5. **Decline** — a closed authorization with `approved: false` and a reason.

Decline reasons to map to one internal enum (`LIMIT`, `FROZEN`, `MCC_BLOCKED`, `FUNDS`, `WEBHOOK`, `OTHER`):

| Source | Limit exceeded | Card frozen | Category blocked | Webhook decline / timeout |
|---|---|---|---|---|
| Stripe `request_history[].reason` | `spending_controls` | `card_inactive`, `card_canceled`, `cardholder_inactive` | `spending_controls` (inspect `merchant_data.category_code`) | `webhook_declined`, `webhook_timeout`, `webhook_error` |
| Marqeta response code | 1834 amount, 1817 count, 1841 per-auth | 1003, 1813, 1901 | 1832 | 1884 (JIT insufficient funds), gateway timeout → decline |
| ISO 8583 / scheme codes seen by the merchant [P, vendor table] | 61 "Exceeds withdrawal amount limit", 65 "Exceeds withdrawal frequency" | 62 "Restricted card", 57 "Transaction not permitted to cardholder" | 57 / 62 | 05 "Do not honor", 91 "Issuer inoperative" |

ISO 8583 codes from https://docs.ebanx.com/docs/pay-in/dev-tools/response-codes/iso8583-codes; meanings vary by
scheme and issuer, so never branch on them — branch on the provider's own reason field.

**MCC blocklist for §2 (gambling, adult, tobacco)** — Visa Merchant Data Standards Manual, April 2026
(https://usa.visa.com/dam/VCOM/download/merchants/visa-merchant-data-standards-manual.pdf) [V]:

| MCC | Visa description | Stripe category enum (names [V], MCC↔name mapping [P]) |
|---|---|---|
| 7995 | Betting, incl. lottery tickets, casino gaming chips, off-track betting, wagers | `betting_casino_gambling` |
| 7800 | Gambling – Government-Owned Lotteries | `government_owned_lotteries_us_region_only` |
| 7801 | Gambling – Government-Licensed On-Line Casinos | `government_licensed_online_casions_online_gambling_us_region_only` |
| 7802 | Gambling – Government-Licensed Horse/Dog Racing | `government_licensed_horse_dog_racing_us_region_only` |
| 9406 | Government-Owned Lotteries (Non-U.S. region) | `government_owned_lotteries_non_us_region` |
| 5967 | Adult Content and Services (direct marketing, inbound teleservices) | `direct_marketing_inbound_telemarketing` |
| 7273 | Dating and Escort Services | `dating_escort_services` |
| 7297 | Massage Parlors | `massage_parlors` |
| 5993 | Cigar Stores and Stands | `cigar_stores_and_stands` |

Stripe enum names checked against https://stripe.dev/stripe-java/com/stripe/param/issuing/CardholderCreateParams.SpendingControls.BlockedCategory.html
(the page lists names, not codes). MCC 5194 (sometimes cited for tobacco wholesale) is **not** in the Visa manual; do not rely on it. Also block, for
§10 reasons (cash and money movement are not "spend"): 6010/6011 (financial institutions, manual/automatic cash),
6051 (non-financial institutions: foreign currency, crypto, money orders), 4829 (wire transfer/money orders).
Alcohol (5813, 5921) is not banned by §2; the owner decides in config. Keep the list in `spend_tiers.yaml` as
`blocked_mcc:` and pass it at issue time; MCC blocks are a coarse net (merchants mis-classify), so the webhook
parser also checks `merchant_data.category_code` and the ledger checks merchant names against the DNC/blocklist.

**Reconciliation into the ledger (§10, §15 `Transaction`):**

- Key every ledger row on `(provider, card_ref, provider_authorization_id)`; captures and refunds reference it.
  Unlinked refunds and force captures get their own row with `authorization_id = null` and an `anomaly` flag that
  the auditor reads.
- Status mapping: authorization approved → `prepared` (amount held, not spent); capture → `settled` (final amount;
  may differ from the hold — record both); bank/issuer statement line matched → `reconciled`. `released` is not
  used for card spend (it belongs to maker-checker transfers).
- Spent-this-month for display = sum of captures + open holds, in card currency; this is also how Stripe counts
  ("includes both captures and authorizations"). A declined authorization is logged (`AuditEvent`) but never a
  ledger row.
- Pull `list_transactions(since)` on a timer (every 15 min, and nightly for the auditor) **and** consume webhooks;
  the pull is the source of truth, webhooks only lower latency. Idempotent upserts by provider id.
- FX: store `amount` (card currency), `merchant_amount`, `merchant_currency`; P&L per experiment uses the card
  currency figure converted to AED at the ledger's daily rate, with the provider's figure kept verbatim.

## 4. `CardIssuerPort` Protocol and the in-memory fake

```python
# nour/ports/card.py  (sketch; Pydantic models live in nour/models)
from __future__ import annotations
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Mapping, Protocol, Sequence
from pydantic import BaseModel


class DeclineReason(StrEnum):
    LIMIT = "limit"
    FROZEN = "frozen"
    MCC_BLOCKED = "mcc_blocked"
    FUNDS = "funds"
    WEBHOOK = "webhook"
    OTHER = "other"


class CardRef(BaseModel):  # never carries a PAN, CVC or expiry
    provider: str  # "stripe" | "airwallex" | "nymcard" | "fake"
    card_id: str  # provider id
    budget_holder: str  # "operator" | "assistant_logistics" | "operator.ai_models"
    last4: str
    currency: str  # ISO 4217, e.g. "AED"
    monthly_cap: Decimal
    frozen: bool
    blocked_mcc: frozenset[str]


class CardTransaction(BaseModel):
    provider_id: str  # capture/refund id
    authorization_id: str | None  # None for unlinked refunds / force captures
    card_id: str
    kind: Literal["authorization", "capture", "refund", "reversal", "decline"]
    amount: Decimal  # card currency, signed (refund positive)
    merchant_amount: Decimal
    merchant_currency: str
    merchant_name: str
    mcc: str
    occurred_at: datetime
    decline_reason: DeclineReason | None = None
    raw_ref: str  # hash or id of the raw payload kept in object storage


class AuthorizationEvent(BaseModel):  # parsed from the real-time webhook
    provider_event_id: str
    authorization_id: str
    card_id: str
    amount: Decimal
    currency: str
    mcc: str
    merchant_name: str
    merchant_country: str
    is_incremental: bool
    received_at: datetime


class CardIssuerPort(Protocol):
    def issue_card(
        self, budget_holder: str, monthly_cap: Decimal, currency: str, blocked_mcc: Sequence[str]
    ) -> CardRef: ...
    def set_cap(self, card: CardRef, monthly_cap: Decimal) -> CardRef: ...
    def freeze(self, card: CardRef, reason: str) -> CardRef: ...
    def unfreeze(self, card: CardRef, approval_id: str) -> CardRef: ...
    def list_transactions(self, card: CardRef, since: datetime) -> list[CardTransaction]: ...
    def parse_authorization_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> AuthorizationEvent: ...
    def authorization_response(
        self, approved: bool, reason: DeclineReason | None
    ) -> tuple[int, dict]: ...
```

Contract notes: every method is idempotent on retry; `freeze` must succeed even when the provider is degraded
(retry with backoff for ≤ 5 s, then raise `CardIssuerUnavailable` so the kill switch escalates to revoking the
provider API token); `unfreeze` requires the `approval_id` of a passphrase-verified owner command (§6, §12);
`parse_authorization_webhook` verifies the provider signature and raises on failure, so an unsigned POST can never
become an `AuthorizationEvent`. Nothing in this module may log, return or store a PAN; the adapter's HTTP client
strips any `number`/`cvc`/`pan` keys before responses reach application code (defense in depth for R4).

**`FakeCardIssuer` (tests, staging twin, §13) — required behaviour:**

- Holds `cards: dict[card_id, CardRef]` and `spent: dict[(budget_holder, "YYYY-MM"), Decimal]` keyed by
  **calendar month in UTC** (matching Stripe's and Airwallex's reset at 00:00 UTC on the 1st) — document that the
  owner's Dubai month boundary is 04:00 local.
- `authorize(card_id, amount, mcc, ...)` (test-only helper that drives the same path as the webhook) returns a
  `CardTransaction(kind="decline", decline_reason=LIMIT)` when `spent + open_holds + amount > monthly_cap`,
  `FROZEN` when `card.frozen`, `MCC_BLOCKED` when `mcc in blocked_mcc`; otherwise records a hold. `capture(auth_id,
  amount)` always succeeds (even above the hold, to model over-capture) and moves the amount to `spent`.
- Separate counters per budget holder, so `operator` and `operator.ai_models` are tracked independently while a
  parent cap (`operator` total) is also enforced when configured (see §7).
- `fail_next(n: int = 1, exc: type[Exception] = CardIssuerUnavailable)` makes the next *n* calls raise, so tests
  cover the outage path (§12 incident table: provider outage → reduce to tier K).
- `last4` is random; there is no field anywhere for a PAN, and the fake's `__repr__` is `CardRef`-only. A
  Hypothesis property test asserts `sum(captures in month) <= monthly_cap + over_capture_allowance` for random
  authorization sequences.

## 5. Phase-0 gate tests (§16: "card declines above cap", "kill switch freezes within 5 seconds")

1. Issue the Operator card with cap AED 3,000 and `blocked_mcc`; authorize 2,900 then 200 → second is `LIMIT`.
2. Authorize at MCC 7995 → `MCC_BLOCKED` before any webhook logic runs.
3. Kill switch: `freeze()` on both cards, then authorize on each → `FROZEN`; measured end-to-end < 5 s, including
   the live provider in staging.
4. `unfreeze()` without `approval_id` → refused and logged.
5. `fail_next()` during `list_transactions` → scheduler retries, auditor sees the gap, no ledger corruption.
6. Live provider: a real AED 1 authorization on the test card, then a cap of AED 0.5 and a second attempt → decline
   visible in `list_transactions` with the provider's reason mapped to `LIMIT`.

## 6. Mapping the spec's guarantees onto provider features

- **"The card enforces the cap, not judgment."** The cap lives in the issuer's control object: Stripe
  `spending_controls.spending_limits[{interval: monthly}]`, Airwallex `transaction_limits.limits[{interval:
  MONTHLY}]`, NymCard `/velocitylimits {period: MONTHLY}`, Marqeta `velocity_window: MONTH`. Nour's own spend
  counter (`spend_tiers.yaml`, the watchdog's 3x rule) is a *second* control that may be tighter, never the only one.
  Caveats to state in the owner brief: issuer aggregation can lag (Stripe: up to 30 s) and over-/force-captures
  can exceed the cap by small amounts; mitigate with a `per_authorization` limit equal to the tier-K band
  (AED 1,000) and by blocking or allow-listing over-capture MCCs (restaurants, bars, car rental, hotels, taxis)
  on the Operator card, whose job is online experiments, not travel.
- **Belt and braces at authorization.** Where a real-time hook exists, Nour answers it: decline when the kill
  switch is set (even before the provider's freeze has propagated), when the task/experiment budget is exhausted,
  when the MCC is blocked, or when the amount would cross a tier boundary without an approval reference. Configure
  the provider's timeout default to **decline** for the Operator card (fail closed); the Assistant logistics card
  may fail open if the owner prefers, since it only buys known items.
- **"Kill switch freezes both cards" (§12).** `freeze()` on every `CardRef` in parallel, then revoke the provider
  API token from the secrets manager (so a compromised agent cannot unfreeze), then pause queues. Release requires
  passphrase plus second channel (§6) and calls `unfreeze(approval_id)`. The watchdog (§12 automatic triggers) uses
  the same path. Status change webhooks (`issuing.card.inactive`, `CARD_STATUS_CHANGE`, Stripe `issuing_card.updated`)
  are logged as confirmation; the freeze is considered effective only when the provider's card object reads frozen.
- **Tier 3 (§6).** Cards are created with no PAN retrieval permission: Stripe — never pass `expand[]=number`;
  Airwallex — do not enable the PCI details API, use iframes if a human must see the number; NymCard — PCI widget
  with limited-use tokens. The owner, not Nour, enters card details into a merchant site when a card-on-file is
  needed; Nour pays only where the provider offers programmatic checkout or a network token.
- **No API cap → does not qualify.** A dashboard-only limit can be changed by whoever holds the dashboard login
  (which Nour must not hold, §2) and cannot be asserted by a test; a "soft" alert (Airwallex
  `low_remaining_transaction_limit`, Pemo cycle alerts) is monitoring, not enforcement. Spend platforms without a
  documented card-control API (Alaan, Qashio, Wio today) are therefore not adapters, even though their own cards
  do decline at the limit; they may still be the owner's personal choice for the Assistant logistics card if the
  owner sets the cap by hand and Nour only reads the feed — in that configuration the adapter exposes `set_cap`
  and `freeze` as `NotSupported`, and the kill switch must fall back to revoking the owner-granted read token plus
  an owner alert. Record this degradation in `Incident` and the morning brief; it is not acceptable for the
  Operator card.

## 7. The AI-model budget line inside the Operator cap (§10)

- Issue a **second card** under the Operator budget holder: `budget_holder = "operator.ai_models"`, its own
  `monthly_cap` (`spend_tiers.yaml: monthly_cap.ai_models_within_operator`), MCC allow-list limited to software/
  SaaS where the provider supports allow-lists (Stripe `allowed_categories`, Airwallex `allowed_merchant_categories`,
  NymCard allowed MCCs) so it cannot pay for ads or goods.
- Keep the total by construction: on Stripe the cardholder-level `monthly` limit of AED 3,000-equivalent applies
  across all of the cardholder's cards, so the main Operator card and the AI sub-card share one hard ceiling
  (https://docs.stripe.com/issuing/controls/spending-controls). Airwallex and NymCard document per-card limits only;
  there, set the two cards so that `cap_main + cap_ai_models == 3,000` and let Nour's ledger show the split. If a
  provider cannot issue a second card, the AI line is a ledger-enforced sub-budget inside a single 3,000 card and
  the brief says so; the 3,000 total is still enforced by the card, which is what §2 requires.
- Every model-provider charge is reconciled to an `Experiment` or `Task` via the invoice/receipt id in the
  transaction metadata; unmatched AI spend after 48 h is an auditor finding. The sub-card is included in the kill
  switch and the watchdog like any other card.

## 8. Open items

- Airwallex UAE go-live date and whether AED becomes a control currency; Mamo `partner_cards` limit fields;
  NymCard card-status endpoint and real-time authorization option; whether Alaan or Qashio expose any card API to
  customers (Alaan "Public API / webhooks" is a third-party claim); Revolut Business availability in the UAE;
  Marqeta UAE availability. Re-run this survey before the phase 2 gate.
