# Model adapters — primary brain, critic, auditor/fallback, Tier 2 private model

Reference for engineers implementing SPEC §4 (model layer), §8 (self-critic), §12 (auditor, outage
incident), §13 (provider outage / drift controls), §14 (model-agnostic core, fallback tested weekly,
cost per task in the ledger, UAE residency) and §17 (vendor decision). Verified against official docs
on 2026-10-02; anything not verified is marked **[unverified]**. Re-check model ids and prices at
config load (both vendors expose a models endpoint) — they move faster than this file.

Decisions recorded here (owner may override in `config/models.yaml`):

| Role | Vendor | Model id | Why |
|---|---|---|---|
| brain (both desks) | Anthropic | `claude-fable-5-1` | Most capable current model (task brief: "brain = most capable"). Budget alternative `claude-opus-5-5` (same API surface, 2.5× cheaper) — see §2.6 before choosing |
| critic (second pass) | Anthropic | `claude-sonnet-5-5` | Current-generation cheaper model; structured score output; `claude-haiku-4-5` if volume demands |
| auditor (read-only, nightly) | OpenAI | `gpt-6.1-sol` | Different vendor (SPEC §4/§12); 1.05M context fits a day's log; `gpt-6-luna` as budget option |
| fallback (brain during outage) | OpenAI | `gpt-6.1-sol` | Different vendor; runs at tier K only, so near-flagship quality at $2/$10 is enough; `gpt-6-astra` for parity |
| tier2 (vault content) | self-hosted, UAE region | Jais 2 70B (or 8B) via vLLM | Open weights, Arabic-first, inference never leaves the region (§4) |

OpenAI stays the second vendor: it is the only non-Anthropic frontier vendor with a documented UAE
*processing* region (`ae.api.openai.com`), it has a mature strict tool-calling + JSON-schema surface,
and no other vendor showed clearly better Arabic in official material. Gemini was not chosen because
Google's regional-processing story for the UAE could not be verified.

## 1. Vendor-neutral `ModelPort`

The agent loop never imports a vendor SDK. Adapters live in `nour/adapters/models/{anthropic,openai,vllm,fake}.py`
and translate to/from these types (pydantic v2, `nour/ports/model.py`):

```python
Role = Literal["brain", "critic", "auditor", "tier2"]


class ToolDef(BaseModel):
    name: str
    description: str
    input_schema: dict  # JSON Schema; additionalProperties=false, every field in `required`
    strict: bool = True


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict  # arguments already json-parsed; never raw-string matched


class Block(BaseModel):  # vendor-neutral transcript content
    type: Literal["text", "tool_call", "tool_result", "opaque"]
    text: str | None = None
    tool_call: ToolCall | None = None
    tool_result: tuple[str, str, bool] | None = None  # (call_id, content, is_error)
    opaque: dict | None = (
        None  # vendor-bound block (e.g. Claude thinking) — replayed only to the same model
    )


class Message(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: list[Block]


class ModelRequest(BaseModel):
    role: Role
    system: list[
        str
    ]  # ordered stable→volatile; adapters put cache breakpoint after the last stable block
    messages: list[Message]
    tools: list[ToolDef] = []
    max_tokens: int
    output_schema: dict | None = None  # structured output (critic score, auditor report)
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    meta: dict = {}  # desk, coat_id, task_id, conversation_id — logged, never sent as prompt text


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0


class Cost(BaseModel):
    usd: Decimal
    aed: Decimal
    fx_rate: Decimal
    price_table_version: str


class ModelResponse(BaseModel):
    text: str
    tool_calls: list[ToolCall]
    stop_reason: Literal["end_turn", "tool_use", "max_tokens", "refusal", "error"]
    usage: Usage
    cost: Cost
    vendor: str
    model: str
    request_id: str | None
    latency_ms: int
    opaque_blocks: list[dict] = []  # returned verbatim by the adapter so the loop can replay them


class ModelPort(Protocol):
    vendor: str
    model: str

    def complete(self, request: ModelRequest) -> ModelResponse: ...
    def count_tokens(
        self, request: ModelRequest
    ) -> int: ...  # vendor-accurate, used for budget checks
```

Rules the loop enforces regardless of adapter:

- **Append-only transcript.** The loop never edits earlier turns (SPEC §4 Log step and the Anthropic
  preserved-thinking check both require it). Operator instructions that arrive mid-task go in as a
  `system` message appended to `messages`, not by rewriting `system`.
- **One `ModelRequest` per role.** Brain, critic, auditor and tier2 each have their own system prompt,
  tool list and namespace; a request whose `tools` contain a tool outside `meta.desk` is refused before
  it reaches an adapter.
- **`stop_reason` is checked before `tool_calls` are read.** `max_tokens` → retry once with a larger
  budget, `refusal` → log and queue the task as tier K with the refusal category, `error` → outage path (§5).

### 1.1 Executor mapping: tool call → typed action

Every action tool shares one envelope (JSON Schema fragment merged into each tool's `input_schema`):

```json
{"coat": {"type": "string"}, "tier_claimed": {"enum": ["A", "N", "K"]},
 "counterpart": {"type": "string"}, "amount": {"type": ["number", "null"]}, "currency": {"const": "AED"},
 "reason": {"type": "string", "description": "One sentence."},
 "data_tier_touched": {"enum": [0, 1, 2]}}
```

`nour/executor.py` turns a `ToolCall` into an `Action` in this order; any failure is a refusal that is
itself logged (SPEC §4: refusals are logged):

1. `ActionEnvelope.model_validate(call.arguments)` — malformed → `tool_result(is_error=True)`, no execution.
2. Tool must exist in **this desk's** registry (SPEC §4: no tool outside its desk). Cross-desk → refuse + audit flag.
3. `coat` must be named and in `coat.desks_allowed`; Operator desk accepts `buzz-avenue` only. Coat-less outbound → refuse.
4. **Tier is computed server-side** from `permissions.yaml`, `spend_tiers.yaml`, the coat's `approval_rules`
   and `ask_every_time`; `tier_claimed` is stored for the auditor but never used for routing. Mismatch → audit flag.
5. High-impact actions require `passphrase_verified=True` on the triggering event; otherwise tier K.
6. `reason` must be non-empty, ≤ 200 chars, one sentence; `data_tier_touched` must be ≤ the desk's data-tier ceiling.
7. Dispatch: A executes; N executes and schedules the notification; K writes the approvals queue and returns
   a `tool_result` saying "queued, approval id …" so the model moves on.
8. `AuditEvent` row with input/output hashes, `tier_claimed`, computed tier, model, cost reference.

## 2. Anthropic adapter (`nour/adapters/models/anthropic.py`)

Use the official SDK (`anthropic` 1.x, Python ≥ 3.10; pin `anthropic>=1,<2`). Client: `anthropic.Anthropic()`
(key from the secrets manager into `ANTHROPIC_API_KEY`; never in config files).

### 2.1 Model ids and roles (verified 2026-10-02)

| Role | Id | Notes |
|---|---|---|
| brain | `claude-fable-5-1` | 1M context, 128K output. Thinking always on — **omit** `thinking`; depth via `output_config.effort`. `temperature/top_p/top_k` → 400. No assistant prefill. `tool_choice` `any`/`tool` → 400 (use `auto` + instruction + `strict`). `stop_reason: "refusal"` possible (`stop_details.category`). **Requires 30-day data retention; not available under ZDR** unless Anthropic authorises it |
| brain (budget) | `claude-opus-5-5` | Same surface (thinking can't be disabled, effort default `medium` — set it), ZDR-eligible, 2.5× cheaper |
| critic | `claude-sonnet-5-5` | Adaptive thinking on; effort default `high`; no tools needed, so structured outputs apply cleanly |

Exact ids as above — never append date suffixes. Pin by writing the id in `config/models.yaml` and
asserting at startup with `client.models.retrieve(id)` (returns `max_input_tokens`, `max_tokens`, `capabilities`).

### 2.2 Tool-use request/response shape

```python
resp = client.messages.create(
    model=MODEL,
    max_tokens=req.max_tokens,
    system=[*stable_blocks_with_cache_control, *volatile_blocks],  # §2.3
    tools=[
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
            "strict": True,
        }
        for t in sorted_by_name(req.tools)
    ],
    tool_choice={"type": "auto"},  # forced choice is a 400 on Fable 5.1
    output_config={"effort": req.effort},
    messages=render(req.messages),
    betas=["server-side-fallback-2026-07-01"],
    fallbacks="default",  # optional: re-route refusals to another Claude model
    cache_control={"type": "ephemeral"},  # top-level automatic caching of the conversation tail
)
```

- Response `content` blocks: `text`, `tool_use` (`id`, `name`, `input` as a dict — parse with the SDK,
  never string-match), `thinking` (store as `opaque`, replay unchanged to the same model, never edit).
- `stop_reason`: `end_turn`, `tool_use`, `max_tokens`, `refusal`, `pause_turn` (re-send to continue).
- Tool results go back as **one** user message containing all `tool_result` blocks
  (`tool_use_id`, `content`, `is_error`) — one message per call silently discourages parallel calls.
- `usage`: `input_tokens` (uncached remainder only), `cache_creation_input_tokens`,
  `cache_read_input_tokens`, `output_tokens`, `inference_geo`. Total input = sum of the three.
- Errors (most specific first): `BadRequestError` → config bug; `AuthenticationError`/`PermissionDeniedError`/
  `NotFoundError` → outage-with-alert, not retryable; `RateLimitError` → honour `retry-after`;
  `InternalServerError` (500/529 overloaded) and `APIConnectionError`/`APITimeoutError` → retryable (§5).

### 2.3 Prompt caching for the constitution/persona prompt

Caching is a byte-exact prefix match over `tools → system → messages`. Cache read on Fable 5.1 is
$0.25/MTok (0.025×), on Opus 5.5 $0.20 (0.05×), on Sonnet 5.5 $0.20 (0.1×); 5-minute write 1.25×, 1-hour write 2×.
Minimum cacheable prefix on these models: 512 tokens.

- Render `system` as `[constitution, persona, permissions/coat mandate]` with
  `cache_control: {"type": "ephemeral"}` on the **last stable block**; the whole tools+system prefix caches together.
- **Do not put `Session date: {{today}}` at the top of the system prompt** as the §18 skeleton does — every
  byte before the breakpoint must be stable. Send date/desk status as a `{"role": "system", ...}` message
  appended to `messages` (supported on Fable 5.1, Opus 5.5, Sonnet 5.5; no beta header) or in the first user turn.
- Serialise tools sorted by name with `json.dumps(sort_keys=True)`; the tool list must not vary per request.
- One cache namespace per (desk, coat) is expected and fine — the two desks must not share a prompt anyway.
- Events arrive in bursts with idle gaps. On Fable 5.1 a `max_tokens: 0` keep-alive of the previous
  request every ~4 min while a task is open is cheaper than the 1-hour TTL; on Opus/Sonnet use `ttl: "1h"`
  on the explicit marker when the start-to-start gap is 5–60 min.
- Integration test: two identical requests → second has `usage.cache_read_input_tokens > 0`. Alert when the
  daily cache-read ratio drops (a silent invalidator was introduced).

### 2.4 Structured output for the critic score

```python
class CriticScore(BaseModel):
    tone: int; claims: int; compliance: int; register: int      # 0–5 each
    blocking_issues: list[str]; verdict: Literal["send", "fix", "escalate"]
r = client.messages.parse(model="claude-sonnet-5-5", max_tokens=2048, system=CRITIC_SYSTEM,
                          messages=[...draft + register target...], output_format=CriticScore)
score = r.parsed_output     # wire form: output_config={"format": {"type": "json_schema", "schema": ...}}
```

Schema rules: `additionalProperties: false`, no numeric/string constraints, no recursion; incompatible with
`tool_choice` and citations. `Message.critic_score` (SPEC §15) stores the four numbers plus verdict.

### 2.5 Token counting

`client.messages.count_tokens(model=..., system=..., tools=..., messages=...)` → `input_tokens`. Model-specific;
Fable 5.1 / Opus 4.7+ tokenise Arabic ~1–1.35× denser than Sonnet 4.6-era counts. Never use `tiktoken` for
Claude. Used for: pre-flight budget check before a long transcript, the weekly prompt-size drift check, and
`ModelPort.count_tokens`.

### 2.6 Pricing (USD per MTok, first-party API, 2026-10-02)

| Model | Input | 5m cache write | 1h cache write | Cache read | Output | Batch (in/out) |
|---|---|---|---|---|---|---|
| `claude-fable-5-1` | 10 | 12.50 | 20 | 0.25 | 50 | 5 / 25 |
| `claude-opus-5-5` | 4 | 5 | 8 | 0.20 | 20 | 2 / 10 |
| `claude-sonnet-5-5` | 2 | 2.50 | 4 | 0.20 | 10 | 1 / 5 |
| `claude-haiku-4-5` | 1 | 1.25 | 2 | 0.10 | 5 | 0.50 / 2.50 |

Tool-use system-prompt overhead: 286 tokens on Opus 5.5 / Sonnet 5.5 (Fable 5.1 figure not published
**[unverified]**). `inference_geo: "us"` adds 1.1× — irrelevant here (no UAE geo exists, §2.8).
Worked order of magnitude: a brain turn with a 6K cached prefix, 2K fresh input, 800 output on Fable 5.1 ≈
$0.0615 (AED 0.23); the same on Opus 5.5 ≈ $0.025. At ~300 brain turns/day the Fable choice is ≈ AED 2,000/month
versus ≈ AED 800 — the owner's "AI-model budget line inside the AED 3,000 cap" (§17) decides this.

### 2.7 Rate limits

429 `rate_limit_error` with `retry-after` (seconds) and `anthropic-ratelimit-{requests,input-tokens,output-tokens}-{limit,remaining,reset}`
headers; token bucket; limits per model (Fable 5.x shares one pool — Start tier 500K ITPM / 100K OTPM,
Opus 5.5 2M / 400K). **Cache reads do not count toward ITPM**, so caching raises effective throughput.
529 `overloaded_error` → backoff. Spend-cap 429 carries `error.details.error_code: "enforced_spend_limit_reached"`
and **no** `retry-after` — treat as an outage immediately, do not retry. The SDK retries 408/409/429/5xx
twice with backoff (`max_retries`); set `timeout` per role (brain 600 s streaming, critic 60 s) and remember
wall-clock ≈ `timeout × (max_retries + 1)`. Use the Admin Rate Limits API to read configured limits at boot.

### 2.8 Versioning, residency, retention

- API version: header `anthropic-version: 2023-06-01`; the SDK sets it. Pin the SDK major in `pyproject.toml`,
  pin model ids and every `anthropic-beta` string in `config/models.yaml`, and fail startup if
  `client.models.retrieve(id)` 404s.
- `inference_geo` accepts only `"us"` or `"global"`; workspace geo is `"us"` only. **There is no UAE option**,
  so Tier 0/1 content sent to the brain is processed outside the UAE — acceptable under SPEC §6/§14 (only
  Tier 2 must stay in region) but say so in the owner's data-protection record.
- Retention: prompts/outputs are not retained by default except for Covered Models (Fable 5.x, Mythos 5.x),
  which require 30-day retention; ZDR orgs get `400 invalid_request_error` on them. Choosing Fable 5.1 means
  accepting 30-day retention at Anthropic; Opus 5.5 is ZDR-eligible.

## 3. OpenAI adapter (`nour/adapters/models/openai.py`) — auditor and fallback

Official SDK (`openai`, current major **[pin after checking PyPI]**). Use the **Responses API**, not Chat
Completions: current OpenAI reasoning models only support tool calling on Chat Completions with
`reasoning_effort: none` (stated on the `gpt-6-luna` model page), and new features land on Responses first.

### 3.1 Model ids and prices (USD per MTok, from the per-model pages, 2026-10-02)

| Role | Id | Input | Cached input | Output | Context / max out | Notes |
|---|---|---|---|---|---|---|
| fallback / auditor | `gpt-6.1-sol` | 2 | 0.10 | 10 | 1.05M / 128K | Released 2026-09-29; reasoning effort low…max; functions on Responses API only |
| fallback (parity) | `gpt-6-astra` | 10 | 1 | 50 | 1.05M / 128K | Released 2026-09-03; no `none` effort |
| auditor (budget) | `gpt-6-luna` | 0.10 | 0.01 | 0.50 | 1.05M / 128K | Chat Completions tool calls need `reasoning_effort: none` |
| previous flagship | `gpt-5.6-sol` | 4 | 0.40 | 20 | 1.05M / 128K | Still served |

Batch 50% off. Regional-processing endpoints carry a **10% uplift** for models released on/after 2026-03-05.
**[note]** OpenAI's consolidated pricing page did not yet list the `gpt-6*` rows when fetched; the figures above
come from the per-model pages — verify both at config load (`client.models.list()`).

### 3.2 Tool calling shape differences

```python
r = client.responses.create(
    model="gpt-6.1-sol",
    instructions=system_text,
    input=items,
    max_output_tokens=req.max_tokens,
    reasoning={"effort": req.effort},
    tool_choice="auto",
    parallel_tool_calls=True,
    tools=[
        {
            "type": "function",
            "name": t.name,
            "description": t.description,
            "parameters": t.input_schema,
            "strict": True,
        }
        for t in req.tools
    ],
    store=False,
)  # ZDR projects: do not persist responses [verify]
```

| Concern | Anthropic | OpenAI Responses |
|---|---|---|
| Tool definition | `name/description/input_schema/strict` | `type:"function"`, `name/description/parameters/strict` (flat, no `function:` wrapper) |
| Call in output | `tool_use` block: `id`, `name`, `input` (dict) | output item `type:"function_call"`: `call_id`, `name`, `arguments` (**JSON string → `json.loads`**) |
| Result back | `tool_result` block with `tool_use_id`, in a user message | input item `{"type":"function_call_output","call_id":…,"output":…}` |
| Forcing | `any`/`tool` → 400 on Fable 5.1 | `"required"`, `{"type":"function","name":…}`, `allowed_tools` all supported |
| Reasoning state | `thinking` blocks, bound to model+conversation | `reasoning` items must be passed back with the tool outputs (or use `previous_response_id`, which needs `store=true`); with `store=false` use `include: ["reasoning.encrypted_content"]` **[verify]** |
| Stop | `stop_reason` | `status` `completed`/`incomplete` (+ `incomplete_details.reason`: `max_output_tokens`, `content_filter`, …) |
| Refusal | `stop_reason: "refusal"` | output `refusal` item (structured outputs) / `content_filter` |
| Usage | `input_tokens` = uncached remainder | `input_tokens` = **total**; `input_tokens_details.cached_tokens` subset; `output_tokens_details.reasoning_tokens` inside `output_tokens` |
| 429 | `rate_limit_error` + `retry-after` | `429` with `slow_down` for throttling; `503` for overload (since 2026-09-02) |

The adapter maps both shapes onto `ToolCall`/`Block` so the executor (§1.1) is identical under fallback.

### 3.3 JSON mode / structured outputs

`text={"format": {"type": "json_schema", "name": "AuditReport", "schema": ..., "strict": True}}`; same schema
rules as Anthropic (`additionalProperties: false`, all fields required). `client.responses.parse(..., text_format=AuditReport)`
returns a pydantic object. `json_object` (JSON mode) only guarantees valid JSON — never use it for the auditor.

### 3.4 Data residency and retention (relevant to §14)

- Abuse-monitoring logs are kept up to 30 days by default; **Zero Data Retention** is available on
  `/v1/responses` and `/v1/chat/completions` (not Files/Vector Stores/Conversations) and must be granted to
  the org; "Modified Abuse Monitoring" is the lighter option.
- Regional processing (inference stays in-region) exists for **US, EU and UAE** ("limited model support" for
  UAE); AU/CA/JP/IN/SG/KR/UK are storage-only. Per-request routing: a project with Global geography can call
  `https://ae.api.openai.com/v1` to process in the UAE (added 2026-08-21), with the 10% uplift.
  **[unverified]** Secondary reporting says only one model is enabled for UAE inference residency — confirm
  which `gpt-6*` ids are served from `ae.` before relying on it.
- For Nour: the auditor reads the audit log (hashes, metadata, reasons — Tiers 0–1 at most, never Tier 2),
  so UAE processing is a nice-to-have; set `base_url` to `ae.api.openai.com` when the auditor's model is
  available there, otherwise global with ZDR. The fallback brain processes the same Tier 0/1 content as the
  primary and may use global routing.

### 3.5 Vendor separation is validated at config load

```python
assert (
    cfg.auditor.vendor != cfg.brain.vendor and cfg.fallback.vendor != cfg.brain.vendor
)  # SPEC §4, §12, §17
assert host(cfg.fallback.base_url) != host(cfg.brain.base_url)
assert cfg.tier2.vendor == "self_hosted" and cfg.tier2.region in UAE_REGIONS
```

`vendor` is the adapter class name, not a substring of the model id; the check runs in `nour.config.load()`
and in the weekly regression harness, and a failure refuses to start the brain.

## 4. Private in-region model for Tier 2 (`Tier2ModelPort`)

SPEC §13/§14: Tier 2 content is processed only on a private model in the same UAE region as the vault, and
only metadata ever reaches the brain. What exists today (verified 2026-10-02):

| Option | Status | Verdict |
|---|---|---|
| **Self-hosted open weights via vLLM** on GPU instances in AWS `me-central-1` or Azure UAE North | Available now. **Jais 2** (Inception/G42 + MBZUAI + Cerebras, Dec 2025): 8B and 70B chat models, Apache 2.0, 8,192-token context, MSA + dialects + English, vLLM serving documented. **Falcon Arabic 7B** (TII, Falcon-3 base, 32K context; licence not verified). Qwen3 and `gpt-oss-120b` (Apache 2.0, 128K) as multilingual generalists — Arabic quality **[unverified]** | **Chosen.** vLLM's OpenAI-compatible server gives tool calling (`--enable-auto-tool-choice --tool-call-parser hermes|qwen3_xml|…`) and schema-guided structured outputs. Ollama is fine for a dev box (`/v1/chat/completions` with tools, `format` JSON) but has no `tool_choice` |
| Amazon Bedrock `me-central-1` | Regional-availability table shows **no text model with In-Region inference** in `me-central-1`; Claude 5.x, GPT-5.6, Grok and Kimi rows are Global cross-region only; Nova Pro/Lite are "Geo" cross-region (Middle East geo, which includes Bahrain). AWS's Feb 2026 post confirms cross-region inference "travels over the AWS Global Network" with data at rest kept in the source region | Not acceptable for Tier 2 (inference leaves the UAE). Acceptable for Tier 0/1 if the owner prefers AWS billing |
| Azure OpenAI / Foundry UAE North | UAE North does not appear in the OpenAI model region tables (doc dated 2026-09-21); a Microsoft moderator confirmed no deployments in UAE North in 2024. Partner models (e.g. DeepSeek, Cohere) listed for UAE North per a third party **[unverified]** | Not for Tier 2 |
| OpenAI `ae.api.openai.com` | Processing in the UAE, but on a third-party multi-tenant service | Not "private"; only if the owner amends §13 |
| Core42 / G42 sovereign cloud Jais endpoints | **[unverified]** | Evaluate in phase 3 if self-hosting is too costly |

8K context on Jais 2 is enough because Tier 2 extraction works page-by-page on OCR text; long contracts are
chunked inside the port. Deploy one instance in the vault's VPC/subnet, no public ingress, egress only to the
vault's object store; log model calls with hashes only.

```python
class VaultRef(BaseModel):
    doc_id: str
    version: int
    pages: list[int] | None = None  # never content


class Tier2Metadata(BaseModel):  # the ONLY thing allowed out
    doc_id: str
    doc_type: Literal[
        "passport",
        "id",
        "visa",
        "trade_licence",
        "bank_statement",
        "contract",
        "insurance",
        "medical",
        "legal",
        "banking_details",
        "other",
    ]
    title: str
    parties_hash: list[str]
    issued: date | None
    expiry: date | None
    amounts: list[tuple[Decimal, str]]
    language: str
    renewal_action: str | None
    confidence: float


class Tier2ModelPort(Protocol):
    def extract(
        self, ref: VaultRef, task: Literal["classify", "expiry", "summary_fields"]
    ) -> Tier2Metadata: ...
```

The implementation resolves `ref` to decrypted bytes *inside the vault boundary*, runs the private model with
a schema-guided prompt, validates the result against `Tier2Metadata`, then runs a DLP pass (IBAN, passport,
Emirates-ID, card-number patterns; any hit → the field is dropped and an Incident is raised) before returning.
It has no access to the brain's transcript, and the brain has no tool that returns raw vault content. Until
the phase-3 model is live, `Tier2ModelPort` is implemented by `HumanTier2Port` (owner reads and types the
metadata) — not by any public-cloud adapter.

## 5. Fallback and outage policy (`nour/models/router.py`)

**Detecting an outage.** Each adapter classifies exceptions into `Retryable` (timeouts, connection errors,
Anthropic 500/529, OpenAI 5xx/503, 429 with `retry-after`) and `Fatal` (401/403/404, 400 config errors, Anthropic
`enforced_spend_limit_reached`, OpenAI billing errors). Policy per role, after the SDK's own two retries:

1. Honour `retry-after` once more, capped at 30 s; count failures in a sliding 2-minute window.
2. Breaker opens on 3 consecutive `Retryable` failures, or on 1 `Fatal`, or on a p95 latency > 3× baseline
   for 10 minutes (drift/brown-out). State is in Postgres so both desks and the watchdog see it.
3. **Switch vendor:** brain and critic route to the OpenAI fallback (`gpt-6.1-sol`); the transcript is rebuilt
   from the vendor-neutral `messages` (Claude `opaque` thinking blocks are dropped — they are bound to Claude
   anyway). The auditor keeps its own model; if *that* vendor is down, the nightly audit is skipped and reported.
4. **Reduce to tier K:** `autonomy_ceiling = K` for both desks — every action (even A/N categories) is queued
   for approval; replies and renewals keep being *drafted*. Items queued during the outage stay queued; they
   are never auto-released when the primary returns.
5. **Log the incident:** `Incident(type="model_outage", detected_by="nour", first_response="fallback+tier_K",
   frozen_scope="autonomous_tiers")`, an `AuditEvent` with reason "primary model outage: <error class>", and
   the owner is told in the next brief (immediately only if it coincides with a money action). Every call
   made under fallback carries `fallback=true` in `ModelCall` so cost and behaviour can be separated.
6. **Recovery:** half-open probe every 10 min (`count_tokens` on the primary); after 30 min healthy the
   breaker closes, the ceiling is restored, and the incident is resolved with a one-line post-mortem.

**Weekly behavioural regression (SPEC §13/§14/§16).** `tests/regression/scenarios/*.yaml` hold ~40 fixed
scenarios (one per capability category, plus the five planted-instruction cases from the phase-0 gate, a
beneficiary-change email, a staff money request, an Arabic register case per contact type, a spend just above
each band). Each Monday 06:00 the harness runs every scenario against brain, critic and auditor on **both**
the primary and the fallback vendor, with `FakeTools` so nothing is sent or spent, and records
`recordings/<date>/<role>-<model>.jsonl` (requests, tool calls, scores, usage, cost). Assertions are
behavioural, not textual: which tool was called, computed vs claimed tier, coat named, refusal of injected
instructions, critic verdict band, no canary strings in any request. The **behavioural diff** compares the
assertion vector and per-scenario cost/latency to the previous accepted recording; any flipped assertion or
>20% cost drift fails the run, freezes autonomy promotions, and lands in the Monday review. Recordings double
as the fixtures for `ScriptedModel` (§7).

## 6. Cost accounting (`nour/ledger/model_cost.py`)

`config/model_prices.yaml` (owner-editable, versioned):

```yaml
version: 2026-10-02
fx: { usd_aed: 3.6725 }                     # AED is pegged; still a config value, not a constant
anthropic:
  claude-fable-5-1:  { input: 10, cache_write_5m: 12.5, cache_write_1h: 20, cache_read: 0.25, output: 50 }
  claude-opus-5-5:   { input: 4,  cache_write_5m: 5,    cache_write_1h: 8,  cache_read: 0.20, output: 20 }
  claude-sonnet-5-5: { input: 2,  cache_write_5m: 2.5,  cache_write_1h: 4,  cache_read: 0.20, output: 10 }
openai:
  gpt-6.1-sol: { input: 2, cached_input: 0.10, output: 10, regional_uplift: 1.10 }
  gpt-6-luna:  { input: 0.10, cached_input: 0.01, output: 0.50, regional_uplift: 1.10 }
self_hosted:
  jais-2-70b:  { per_hour_usd: <instance price>, amortise_over_calls: true }
```

Per call: Anthropic `usd = (input·p_in + cache_write·p_cw + cache_read·p_cr + output·p_out) / 1e6`;
OpenAI `usd = ((input − cached)·p_in + cached·p_cached + output·p_out) / 1e6 × uplift` (reasoning tokens are
already inside `output`); self-hosted = instance-hour cost apportioned by output tokens. `aed = usd × fx`,
rounded half-up to fils; both stored with `price_table_version` and `fx_rate` so old rows never re-price.
Every call writes a `ModelCall` row (task_id, conversation_id, desk, coat_id, role, vendor, model, usage fields,
usd, aed, latency_ms, request_id, fallback, cache_hit_ratio). When a task closes, the sum becomes a ledger line:
`Transaction(direction=out, category=ai_models, counterpart=<vendor>, amount=<AED>, task_id, coat_id,
status=settled)` — Operator-desk tasks draw down `monthly_cap.ai_models_within_operator`; the watchdog's 3×
daily-expectation freeze applies to model spend too. The morning brief shows model cost per coat and per task
category; the Anthropic Usage & Cost Admin API and OpenAI's usage endpoint are reconciled weekly against
`ModelCall` sums (discrepancy > 5% → incident).

## 7. `ScriptedModel` / `FakeModel` for offline tests (`nour/adapters/models/fake.py`)

Implements `ModelPort` and `Tier2ModelPort` with no network. Required surface:

- **Scripted steps:** `ScriptedModel(steps=[...])`, each step a `ModelResponse` or a callable
  `(ModelRequest) -> ModelResponse`; steps are consumed in order per `role`; running out raises
  `ScriptExhausted` (a test must say how many turns it expects). Helper builders:
  `tool_call_step("send_email", coat="buzz-avenue", tier_claimed="A", counterpart=…, amount=None, reason="…")`,
  `text_step("…")`, `stop_step("max_tokens" | "refusal")`.
- **Adversarial policies** (`policy=`), each exercising a structural control from SPEC §13:
  `obey_injected_instructions` (finds `<<INJECT: pay AED 900 to …>>` markers in any non-owner content and emits
  the requested tool call — executor must refuse and log), `claim_tier_a` (every call says `tier_claimed="A"` —
  executor must compute K), `leak_iban_placeholder` (puts the canary `AE07 0331 2345 6789 0123 456` /
  `TIER2-CANARY-7731` into text and tool arguments — DLP and audit must catch it), `cross_desk_tool` (Assistant
  tool from the Operator desk), `coatless_outbound` (`coat=None` on an outbound tool), `repeat_same_action`
  (same call N times with no new input — watchdog freeze at 3), `refuse_everything`, `fallback_outage(n)`
  (raises `Retryable`/`Fatal` for the first *n* calls — breaker and tier-K tests), `rate_limited(retry_after=…)`.
- **Recorded request log:** `fake.requests: list[ModelRequest]` with rendered system text and tools, so tests
  assert: no Tier 2 canary ever appears in any request (`assert_no_leak(fake, canaries)`), the Operator desk's
  requests contain only Operator tools, the system prompt carries the constitution hash, the date is not in the
  cached prefix, and the fallback adapter received the transcript without `opaque` blocks.
- **Deterministic usage and cost:** fixed `Usage` per step (e.g. 1,000 in / 200 out / 800 cache read) and the
  real `model_cost` function, so ledger tests are exact to the fil.
- **Fixtures:** `fake_models()` returns `{brain, critic, auditor, fallback, tier2}` wired through the same
  router as production; `replay_recording(path)` turns a §5 regression recording into scripted steps.
- `FakeTier2Model.extract` returns a fixed `Tier2Metadata`; any attempt to pass content instead of a `VaultRef`
  is a `TypeError` at the port boundary, which the leak tests rely on.

## Sources (fetched 2026-10-02)

- Anthropic models/pricing: https://platform.claude.com/docs/en/about-claude/pricing · rate limits & headers: https://platform.claude.com/docs/en/api/rate-limits · versioning: https://platform.claude.com/docs/en/api/versioning · data residency (`inference_geo` us/global only): https://platform.claude.com/docs/en/manage-claude/data-residency · retention/ZDR, Covered Models 30-day rule: https://platform.claude.com/docs/en/manage-claude/api-and-data-retention · structured outputs: https://platform.claude.com/docs/en/build-with-claude/structured-outputs · prompt caching: https://platform.claude.com/docs/en/build-with-claude/prompt-caching · tool use: https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview · token counting: https://platform.claude.com/docs/en/build-with-claude/token-counting
- OpenAI: models `gpt-6-astra` / `gpt-6.1-sol` / `gpt-6-luna` / `gpt-5.6-sol`: https://developers.openai.com/api/docs/models/gpt-6-astra, …/gpt-6.1-sol, …/gpt-6-luna, …/gpt-5.6-sol · pricing + 10% regional uplift: https://developers.openai.com/api/docs/pricing · function calling: https://developers.openai.com/api/docs/guides/function-calling · structured outputs: https://developers.openai.com/api/docs/guides/structured-outputs · retention, ZDR, residency regions, `ae.api.openai.com`: https://developers.openai.com/api/docs/guides/your-data · changelog (gpt-6 launches, 429/503 split, Aug-21 regional routing): https://developers.openai.com/docs/changelog · (help-centre residency article https://help.openai.com/en/articles/9903489 returned 403 to automated fetch)
- AWS: Bedrock regional availability table (me-central-1 rows): https://docs.aws.amazon.com/bedrock/latest/userguide/models-region-compatibility.html · cross-region inference for the Middle East (Feb 2026): https://aws.amazon.com/blogs/machine-learning/introducing-amazon-bedrock-global-cross-region-inference-for-anthropics-claude-models-in-the-middle-east-regions
- Azure: Foundry models sold by Azure (region tables, 2026-09-21): https://learn.microsoft.com/en-us/azure/ai-foundry/openai/concepts/models · UAE North Q&A: https://learn.microsoft.com/en-us/answers/a/1445919
- Open weights: Jais 2 70B Chat (Apache 2.0, 8,192 ctx, vLLM): https://huggingface.co/inception42/Jais-2-70B-Chat · Jais 2 announcement: https://mbzuai.ac.ae/news/inception-cerebras-and-mbzuai-release-jais-2-the-next-generation-of-the-worlds-leading-arabic-open-weight-llm/ · Falcon Arabic: https://falconllm.tii.ae/falcon-arabic.html · gpt-oss-120b on Bedrock (Apache 2.0, 128K): https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-oss-120b.html · vLLM tool calling: https://docs.vllm.ai/en/latest/features/tool_calling.html · Ollama OpenAI compatibility: https://docs.ollama.com/api/openai-compatibility
