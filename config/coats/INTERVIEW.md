# The 30-minute knowledge interview (one per company) — مقابلة المعرفة

Fills `config/coats/<slug>.knowledge.md` (sections 1 to 10), the `mandate`, `approval_rules` and
`allowed_activities` fields in `config/coats/<slug>.yaml`, the OWNER EDIT blocks in
`config/coats/<slug>.tone.md`, and the verification page `templates/verification_page.<slug>.md`
(SPEC §5, §17). Run it per company, Buzz Avenue first.

Rules of the room: the engineer asks and types; the owner answers; Nour (Assistant desk) may transcribe. Only what the
owner says enters the pack. Nothing from Tier 1 or 2 is written down here: no personal numbers, no bank details,
no licence numbers, no contract text (those go to the vault through `nour vault put-banking` and intake, OWNER_GUIDE §2).
If the owner does not know a value, write "<owner fills in>" and move on; a blank is safer than a guess, because the
critic treats this pack as the only source of allowed claims. Record the interview date and set the next review date.

| Minutes | Block | Fills |
|---|---|---|
| 0–3 | 1 Company at a glance | knowledge.md §1 |
| 3–9 | 2 Products, services and prices | knowledge.md §3, §4 |
| 9–13 | 3 Policies | knowledge.md §5 |
| 13–16 | 4 Standard terms | knowledge.md §6 |
| 16–19 | 5 Who is who | knowledge.md §8 |
| 19–24 | 6 Mandate: floor, discount, payment terms, templates, owner-only, recipients, activities | coat yaml `mandate`, `allowed_activities`, `approval_rules` |
| 24–27 | 7 Cadence and tone | channels/cadence, tone.md OWNER EDIT |
| 27–30 | 8 Never state, FAQs, verification page | knowledge.md §7, §9, §10; verification page |

## 1. الشركة في لمحة / Company at a glance (knowledge.md §1)
- What does the company sell, in one sentence a stranger would understand?
- Who buys: businesses, consumers, or both? (Consumers mean every message carries the opt-out line.)
- Which markets: UAE only, GCC, international? Which languages do customers write in?
- Working hours in Dubai time, and the days you are closed. (Outreach stays inside 09:00–20:00 regardless.)
- Website; the company email and WhatsApp line Nour will own; confirm the verification page URL (`https://<domain>/nour`).

## 2. المنتجات والخدمات والأسعار / Products, services and prices (knowledge.md §3, §4)
For each sellable item (stop at the ten that matter; the rest can follow by email):
- Code, English name, Arabic name, unit of sale.
- List price in AED, and whether it excludes VAT. (The 85% floor is computed from this; she never recomputes it.)
- Volume breaks: quantity → discount, never above the maximum discount from block 6.
- Lead time, and the stock rule: "always available", "made to order", or "check before quoting".
- Claimable facts she may state: specs, origin, certifications. Anything not listed, she will not say.
- Until when are these prices valid?

## 3. السياسات / Policies (knowledge.md §5)
- Delivery: areas, cost, days. What happens outside those areas?
- Returns and warranty: window, condition, who pays shipping. (Any refund is always ask-first.)
- Cancellation: up to when, and what after production starts?
- Samples: free, paid, or credited against the first order?
- Complaints: who should hear about a reputational-risk complaint, and how fast? (Default: you, immediately.)
- Partial payments and instalments: ever, or always the owner?

## 4. الشروط القياسية / Standard terms (knowledge.md §6)
- How many days is a quote valid?
- Governing law and jurisdiction on quotes and NDAs.
- Late-payment fee, if any. Confirm the chasing cadence day 0, 3, 7, 14 then monthly, stopped on any reply.
- Confirm: bank details appear only on rendered invoices; a change is announced by signed letter and confirmed by a call.
- Confirm: the standard NDA is the only one she sends, and any clause change is yours.

## 5. من هو من / Who is who (knowledge.md §8; roles only, Tier 0)
- Staff by name and role (operations, sales, PRO, accountant): which language each uses, what each may request from Nour.
- Which staff may never be asked for money, documents or commands by anyone (default: all of them).
- Key customer contacts and key supplier contacts, by name and company, and what they may ask for.
- Who at a supplier may announce a bank-detail change, and the number already on file for the callback.
- Anyone the Operator desk must never contact (family, personal contacts, your other companies' people).

## 6. التفويض التجاري / The commercial mandate (coat yaml)
- حدّ السعر / Price floor: lowest price as a percentage of list (shipped default 85). Below that she queues for you.
- صلاحية الخصم / Discount authority: the maximum she may offer alone (default 15%). Beyond it: "the owner decides".
- شروط الدفع / Payment terms she may accept: advance, net 15, net 30? Anything longer is yours.
- النماذج / Templates she may send: quote, invoice, proposal, standard NDA? Any others? (Drafting is free; sending starts ask-first.)
- ما يوقّعه المالك فقط / Owner-only: contract value threshold (default AED 20,000), exclusivity, credit beyond net 30; add any other.
- المستلمون المعتمدون / Approved recipients: who may receive which company document without a fresh approval each time
  (accountant, auditor, bank, PRO). Named people, per document type; the default is nobody.
- الأنشطة المسموحة / Allowed activities under this coat: sell, support, procure, chase invoices, recruit? Anything not listed is refused.
- Which reply categories may start at notify rather than ask-first (shipped: scheduling, supplier inquiry)?
- Spend for this company: who holds the card, and is the Operator's AED 3,000/month right for the first quarter?

## 7. إيقاع المتابعة والنبرة / First follow-up cadence and tone (channels, tone.md)
- First follow-up: how many days after a quote, and how many touches before she stops (default 0, 3, 7, 14, then monthly)?
- Daily WhatsApp send cap to start (default 50) and cold-email cap after the 4-week warm-up (default 40).
- With customers: the greeting (بقدر or Gulf أقدر), formal or lighter, and three phrases you want her to use or avoid.
- With you: حضرتك or إنت; your nicknames for staff and suppliers; recurring phrases she should recognise in voice notes.
- Sign-off confirmed as "Nour, <Company> AI assistant" / نور، المساعدة الذكية في <الشركة>; never your name, never "team".
- Product and place names for the speech vocabulary (go into the custom vocabulary with the engineer).

## 8. ما لا يُقال أبداً / The "never state" list, FAQs and the verification page (knowledge.md §7, §9, §10)
- Beyond the fixed list (no price below floor, no stock figures not in the pack, no bank details in chat, no mention of
  your other companies, no invented urgency): what else must she never say or promise for this company?
- Must this company look unrelated to your others? (Yes means a separately named persona, SPEC §5; say so now.)
- Three questions customers ask most, and the exact answer you want, in Arabic and English (knowledge.md §7).
- صفحة التحقق / Verification page: confirm the wording "Nour is the AI assistant of <Company>", the official email and
  line, and the sentence customers hear once: she never asks for payment to a new or changed account.
- Confirm the opt-out line and that a "no" on any channel puts the contact on the do-not-contact list for every company.

## Closing checklist — من يُدخل ماذا وأين / who enters what, where
- [ ] Engineer: knowledge.md §1, §3–§8 from blocks 1–5 and 8; interview date, pack version, next review date; change-log row.
- [ ] Engineer: `mandate`, `allowed_activities`, `approval_rules.notify_categories` in the coat yaml from block 6; `nour config check` passes.
- [ ] Engineer: `channels` caps in the coat yaml and the cadence in `config/channels.yaml` from block 7.
- [ ] Engineer: OWNER EDIT blocks in tone.md, speech custom vocabulary, verification page placeholders from blocks 7–8.
- [ ] Owner: approved-recipient names confirmed on his thread; receiving bank details entered with `nour vault put-banking <coat> <field>`.
- [ ] Owner: reads the filled pack once, replies "approved" on his thread; the engineer commits it with that message quoted.
- [ ] Both: a second persona requested? Open it as a charter question before go-live; otherwise note "same name and face".
