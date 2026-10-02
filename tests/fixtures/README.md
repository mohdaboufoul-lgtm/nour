# Test fixtures
## `arabic_commands.yaml` — owner-command corpus

Ground truth for the owner-thread parser (SPEC §2, §6, §9, §10): messages the owner would send
Nour on WhatsApp in Lebanese dialect (Arabic script), Arabizi ("3ala", "sho el 2akhbar") or
code-switched Arabic/English, plus a few MSA-leaning and terse ones ("ok", "لا", "eh", "ماشي").
The corpus is the oracle and the parser is under test: on failure fix the parser, and change the
fixture only when the SPEC shows the fixture is wrong.

Top-level keys: `reference_now` (Tuesday 2026-10-06 09:00 Asia/Dubai; freeze the clock on it so
relative dates resolve), `timezone`, `owner_verified` (every message is from the owner's number),
`passphrase_placeholder`, `intents` (the closed set) and `commands` (the list).

| Field | Meaning |
|---|---|
| `id` | `cmd_NNN`, unique and stable; use it as the pytest id |
| `script` | `arabic`, `arabizi`, `mixed`, `english` (pure English counts with code-switched) |
| `channel` | `text` or `voice_note` (text holds the transcript; voice is never identity) |
| `text` | The message; a passphrase is always the literal `<PASSPHRASE>` on its own `pass:` line |
| `intent` | One of `intents`; a mixed message carries the primary intent, the rest sit in `entities` |
| `coat` | `buzz-avenue`, another company slug, or `null` when the owner names none (Nour infers) |
| `entities` | Surface forms from the text, digits normalised; `*_raw` keeps the phrase, `date`/`time` resolve it against `reference_now` (`next <weekday>` = first such weekday after it) |
| `changes_record` | A CRM, ledger, task, calendar or owner-profile line is written |
| `moves_money`, `in_owner_name` | As named; `in_owner_name` implies `high_impact` |
| `high_impact` | SPEC §6 list: money out above the notify band or in `always_K`, beneficiary or account changes, vault retrieval, document sharing, sends in the owner's name, autonomy changes |
| `read_back_required` | `channel == voice_note and (moves_money or in_owner_name or changes_record)` |
| `expected_read_back` | One Lebanese line ending in a yes/no question (`صح؟`, `موافق؟`, `أكّد؟`), else `null` |
| `contains_passphrase_line` | True only for a typed `pass: <PASSPHRASE>` line; a spoken passphrase in a transcript is false |
| `expected_tier_hint` | `A`, `N` or `K` under `config/spend_tiers.yaml` and `config/permissions.yaml` defaults |
| `notes` | English gloss and the rule the entry exercises |

Tier conventions: spend <= 200 AED is `A`, 201-1000 `N`, above 1000 `K`; `always_K` kinds (refund,
subscription, new beneficiary or account) are `K` at any amount; every high-impact command is `K`
with or without the passphrase (the passphrase lets the queued item be released, it never lowers
the tier). Scheduling, drafts, research, CRM updates, reminders and reads are `A`; `reschedule` is
`N` (a calendar move affecting the owner's day), as is a supplier-inquiry reply (coat notify
category); a customer reply that commits to money is `K`; `kill_switch` is `A` (obeyed instantly,
even by voice); `approve`/`reject` are `A` because only the decision is logged, and a bare "ok" or
a voice "eh" never stands in for the passphrase. Forwarded "we changed our bank" texts (cmd_015,
cmd_016, cmd_075) are observed content: the quoted instruction is data, the owner's own words set
the intent (`question`, `task_create`), and the registry never changes without the callback.

## Using the corpus in tests

```python
CORPUS = yaml.safe_load(open("tests/fixtures/arabic_commands.yaml"))
CASES = [pytest.param(c, id=c["id"]) for c in CORPUS["commands"]]


@pytest.mark.parametrize("case", CASES)
def test_intent(parser, case):
    assert parser.parse(case["text"], channel=case["channel"]).intent == case["intent"]
```

Write one parametrized test per contract (intent, coat, entity subset, tier hint, read-back flag
and text, passphrase-line detection), not one per command. Compare entities as a subset
(`expected.items() <= actual.items()`) so a richer parser still passes; freeze time with `freezegun`
at `reference_now` before resolving dates; feed voice entries as transcripts and assert the
passphrase detector ignores them. When adding entries keep ids sequential and the script mix near
40/30/30, never type a real passphrase, and keep a self-test that re-derives `read_back_required`.
