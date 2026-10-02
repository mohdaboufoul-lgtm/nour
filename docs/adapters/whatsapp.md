# WhatsApp adapter reference (`WhatsAppPort` + inbound webhook)

Engineering reference for the WhatsApp Business Platform (Cloud API) as used by Nour. Serves
`docs/SPEC.md` sections 4 (owner thread), 5 (one WhatsApp Business line per coat), 9 (daily send caps,
template-first contact, opt-out), 12 (kill switch), 13 (block-rate monitoring, bans), 16 (phase 0 event
bus). Facts were checked against Meta's developer docs on 2026-10-02; what could not be verified is
marked **UNVERIFIED**. Meta is moving docs to `developers.facebook.com/documentation/business-messaging/
whatsapp/...`; the older `docs/whatsapp/cloud-api/...` URLs still resolve and both are cited.

## 0. Versions and auth

- Base URL `https://graph.facebook.com/<VERSION>/`. Latest Graph API is v26.0 (2026-07-29); v25.0 is
  supported until 2028-07-29 and is what most WhatsApp examples use. Pin one version in config.
  https://developers.facebook.com/docs/graph-api/changelog
- Header `Authorization: Bearer <SYSTEM_USER_TOKEN>`; permissions `whatsapp_business_messaging` and
  `whatsapp_business_management`. https://developers.facebook.com/docs/whatsapp/cloud-api/get-started
- System-user tokens are long-lived (can be set to never expire); keep them only in the secrets manager,
  rotate quarterly, and implement "revoke every token" by invalidating the system user in Business
  Settings (no self-revoke endpoint found — **UNVERIFIED**).

## 1. Inbound webhook

### 1.1 Verification handshake (GET)
Meta calls the callback with `hub.mode=subscribe`, `hub.verify_token=<yours>`, `hub.challenge=<int>`.
If the token matches, respond `200` with the raw `hub.challenge` as the body; else `403`.
https://developers.facebook.com/docs/graph-api/webhooks/getting-started
Setup: App Dashboard → WhatsApp → Configuration (callback URL, verify token, subscribe to `messages`),
then `POST /<WABA_ID>/subscribed_apps` per WABA (optional `override_callback_uri` + `verify_token`).
https://developers.facebook.com/docs/whatsapp/cloud-api/guides/set-up-webhooks ·
https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/override/
Also subscribe: `account_update`, `phone_number_quality_update`, `business_capability_update`,
`message_template_status_update`, `user_preferences`.

### 1.2 Signature (POST)
`X-Hub-Signature-256: sha256=<hex>` = HMAC-SHA256 of the request body, key = the app's **App Secret**.
Constant-time compare against everything after `sha256=`. Meta signs "an escaped unicode version of the
payload, with lowercase hex digits" (`äöå` → `äöå`): verify the **raw bytes received**;
re-serialising parsed JSON breaks it (Arabic text guarantees this).
https://developers.facebook.com/docs/graph-api/webhooks/getting-started ·
https://developers.facebook.com/docs/messenger-platform/webhooks (escaping note)
Respond `200 OK` to every POST and process asynchronously (no explicit timeout in the docs — **UNVERIFIED**).
On non-200 Meta retries with decreasing frequency (Cloud API page: up to 7 days; Graph API page: 36 h) and
"these retries can result in duplicate webhook notifications". Batching (≤ 1000) and ordering are not guaranteed.

### 1.3 Envelope and the fields the adapter needs
```json
{"object":"whatsapp_business_account","entry":[{"id":"<WABA_ID>","changes":[{"field":"messages","value":{
  "messaging_product":"whatsapp",
  "metadata":{"display_phone_number":"15550783881","phone_number_id":"106540352242922"},
  "contacts":[{"profile":{"name":"Sheena Nelson"},"wa_id":"16505551234"}],
  "messages":[ ... ]   /* or "statuses":[ ... ]  — never both in one value */ }}]}]}
```
https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/components
- `entry[].id` = WABA id. `value.metadata.phone_number_id` = **receiving line → coat**. Route on it,
  never on `display_phone_number` (human form, no `+`).
- `messages[].id` = `wamid.<base64>`, unique per message: the **dedupe key**.
- `messages[].from` / `contacts[].wa_id` = sender (E.164 digits, no `+`). `contacts[].profile.name` is
  user-chosen and untrusted: never use it for identity, routing or owner checks.
- `messages[].timestamp` = unix seconds as a string; `messages[].context` (on replies) carries `from`,
  `id` of the quoted message and `forwarded`; `messages[].errors[]` e.g. `type:"unsupported"` (stickers, polls).

### 1.4 One full example per kind (only `value` shown; envelope as in 1.3)
Text — https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages/text
```json
{"messaging_product":"whatsapp","metadata":{"display_phone_number":"15550783881","phone_number_id":"106540352242922"},"contacts":[{"profile":{"name":"Sheena Nelson"},"wa_id":"16505551234"}],
 "messages":[{"from":"16505551234","id":"wamid.HBgLMTY1MDM4Nzk0MzkVAgASGBQzQTA0NjU1OUFFRTAzODEwMTQ0RgA=",
   "timestamp":"1749416383","type":"text","text":{"body":"Does it come in another color?"}}]}
```
Audio / voice note (`voice:true` = recorded in the client, `false` = uploaded file) —
https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages/audio/
```json
{"messaging_product":"whatsapp","metadata":{"display_phone_number":"15550783881","phone_number_id":"106540352242922"},"contacts":[{"profile":{"name":"Sheena Nelson"},"wa_id":"16505551234"}],
 "messages":[{"from":"16505551234","id":"wamid.HBgLMTY1MDM4Nzk0MzkVAgASGBQzQTRBNjU5OUFFRTAzODEwMTQ0RgA=",
   "timestamp":"1744344496","type":"audio","audio":{"mime_type":"audio/ogg; codecs=opus",
   "sha256":"wvqXMe6n7n1W0zphvLPoLj+s/NtKqmr3zZ7YzTP7xFI=","id":"1908647269898587",
   "url":"https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=133...","voice":true}}]}
```
Image — https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages/image
```json
{"messaging_product":"whatsapp","metadata":{"display_phone_number":"15550783881","phone_number_id":"106540352242922"},"contacts":[{"profile":{"name":"Sheena Nelson"},"wa_id":"16505551234"}],
 "messages":[{"from":"16505551234","id":"wamid.HBgLMTY1MDM4Nzk0MzkVAgASGBQzQTRBNjU5OUFFRTAzODEwMTQ0RgA=",
   "timestamp":"1744344496","type":"image","image":{"caption":"Taj Mahal","mime_type":"image/jpeg",
   "sha256":"SfInY0gGKTsJlUWbwxC1k+FAD0FZHvzwfpvO0zX0GUI=","id":"1003383421387256",
   "url":"https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=133..."}}]}
```
Document — https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages/document
```json
{"messaging_product":"whatsapp","metadata":{"display_phone_number":"15550783881","phone_number_id":"106540352242922"},"contacts":[{"profile":{"name":"Sheena Nelson"},"wa_id":"16505551234"}],
 "messages":[{"from":"16505551234","id":"wamid.HBgLMTY1MDM4Nzk0MzkVAgASGBQzQTRBNjU5OUFFRTAzODEwMTQ0RgA=",
   "timestamp":"1744344496","type":"document","document":{"caption":"my receipt","filename":"receipt.pdf",
   "mime_type":"application/pdf","sha256":"V5OPpLD/gEG6Xjg0MbmQDLFgcKsL+j5LfY4ny/pZ4MY=","id":"622684793477189",
   "url":"https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=133..."}}]}
```
Reaction (`emoji` is omitted when the user removes a reaction) —
https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages/reaction
```json
{"messaging_product":"whatsapp","metadata":{"display_phone_number":"15550783881","phone_number_id":"106540352242922"},"contacts":[{"profile":{"name":"Sheena Nelson"},"wa_id":"16505551234"}],
 "messages":[{"from":"16505551234","id":"wamid.HBgLMTY1MDM4Nzk0MzkVAgASGBQzQUFERjg0NDEzNDdFODU3MUMxMAA=",
   "timestamp":"1749419544","type":"reaction",
   "reaction":{"message_id":"wamid.HBgLMTQxMjU1NTA4MjkVAgASGBQzQUNCNjk5RDUwNUZGMUZEM0VBRAA=","emoji":"👍"}}]}
```
Status (`sent` → `delivered` → `read`, or `failed`; `played` is also listed, for audio) —
https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages/status
```json
{"messaging_product":"whatsapp","metadata":{"display_phone_number":"15550783881","phone_number_id":"106540352242922"},
 "statuses":[{"id":"wamid.HBgLMTY1MDM4Nzk0MzkVAgASGBQzQUFERjg0NDEzNDdFODU3MUMxMAA=","status":"sent",
   "timestamp":"1750030073","recipient_id":"16505551234",
   "conversation":{"id":"72b14d6bd5407799e66f64d1b338e567","expiration_timestamp":"1750116480","origin":{"type":"marketing"}},
   "pricing":{"billable":true,"pricing_model":"PMP","type":"regular","category":"marketing"}}]}
```
```json
{"messaging_product":"whatsapp","metadata":{"display_phone_number":"15550783881","phone_number_id":"106540352242922"},
 "statuses":[{"id":"wamid.HBgLMTY1MDM4Nzk0MzkVAgARGBI0QUQ2MjA4NEYyRkExNjMyREUA","status":"failed",
   "timestamp":"1751142888","recipient_id":"16505551234",
   "errors":[{"code":131049,"title":"This message was not delivered to maintain healthy ecosystem engagement.",
     "message":"This message was not delivered to maintain healthy ecosystem engagement.",
     "error_data":{"details":"In order to maintain a healthy ecosystem engagement, the message failed to be delivered."},
     "href":"/documentation/business-messaging/whatsapp/support/error-codes"}]}]}
```
`read` carries only `id/status/timestamp/recipient_id` (v24.0+ omits conversation/pricing). `pricing.type`
∈ `regular | free_customer_service | free_entry_point`; `pricing_model` is `PMP` (per-message, since
2025-07-01). Whether `read` is withheld when read receipts are off is undocumented — **UNVERIFIED**.

Other `field` values on the same envelope:
- `user_preferences` (user tapped stop/resume on marketing messages):
  `{"wa_id":"16505551234","detail":"User requested to stop marketing messages","category":"marketing_messages","value":"stop","timestamp":1731705721}`
  https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/user_preferences
- `account_update`: `event` ∈ `ACCOUNT_VIOLATION | DISABLED_UPDATE (ban_info.waba_ban_state) | ACCOUNT_RESTRICTION (restriction_info[].restriction_type, expiration)`.
  https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/reference/account_update
- `phone_number_quality_update`: `{"display_phone_number":…,"event":"THROUGHPUT_UPGRADE"|"ONBOARDING"|…,"current_limit":"TIER_250"}` (`current_limit` deprecated Feb 2026).
  https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/phone_number_quality_update
- `business_capability_update`: `{"max_daily_conversations_per_business":2000,"max_phone_numbers_per_waba":25}`.
  https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/business_capability_update

## 2. Outbound

All sends: `POST /<VERSION>/<PHONE_NUMBER_ID>/messages`, JSON body. The `PHONE_NUMBER_ID` in the path
**is the coat**. https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages
Success response (HTTP 200) for every type:
```json
{"messaging_product":"whatsapp","contacts":[{"input":"16505551234","wa_id":"16505551234"}],
 "messages":[{"id":"wamid.HBgLMTY1MDM4Nzk0MzkVAgARGBJBRkJENzExMTRFRjk2NTI1OTEA","message_status":"accepted"}]}
```
`message_status` ∈ `accepted | held_for_quality_assessment | paused`. `accepted` = queued, not
delivered; delivery truth is the `statuses` webhook. Store `messages[0].id` to correlate.

Text (max 4096 chars; `context.message_id` makes a quoted reply) —
https://developers.facebook.com/docs/whatsapp/cloud-api/messages/text-messages
```json
{"messaging_product":"whatsapp","recipient_type":"individual","to":"9715XXXXXXXX","type":"text",
 "context":{"message_id":"wamid.<inbound id>"},"text":{"preview_url":false,"body":"..."}}
```
Template — the **only** type allowed outside the 24-hour customer service window, so every contact
Nour initiates is a template —
https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/overview ·
https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/utility-templates/utility-templates
```json
{"messaging_product":"whatsapp","recipient_type":"individual","to":"9715XXXXXXXX","type":"template",
 "template":{"name":"reservation_confirmation","language":{"code":"ar"},
   "components":[{"type":"body","parameters":[{"type":"text","parameter_name":"day","text":"Saturday"}]}]}}
```
Templates are created per WABA (`POST /<WABA_ID>/message_templates`), reviewed (`IN_REVIEW` ≤ 24 h →
`APPROVED | REJECTED`), and can be `PAUSED`/`DISABLED` on negative feedback. Category `utility | marketing |
authentication` is fixed at creation and drives billing; promotion filed as utility is a policy violation.

Audio / voice-note reply — https://developers.facebook.com/docs/whatsapp/cloud-api/messages/audio-messages
```json
{"messaging_product":"whatsapp","recipient_type":"individual","to":"9715XXXXXXXX","type":"audio",
 "audio":{"id":"<MEDIA_ID>","voice":true}}
```
Upload first: `POST /<PHONE_NUMBER_ID>/media` multipart (`file`, `type`, `messaging_product=whatsapp`) →
`{"id":"<MEDIA_ID>"}`. Audio ≤ 16 MB; `audio/ogg` **must be OPUS** with `"voice": true` to render as a
voice message; aac/mp3/m4a/amr render as plain files. Synthesise Nour's voice to OGG/Opus mono.

Mark as read / typing (inbound ids only, within 30 days; also marks earlier messages read) —
https://developers.facebook.com/docs/whatsapp/cloud-api/guides/mark-message-as-read ·
https://developers.facebook.com/docs/whatsapp/cloud-api/typing-indicators
```json
{"messaging_product":"whatsapp","status":"read","message_id":"wamid.<inbound id>","typing_indicator":{"type":"text"}}
```
→ `{"success":true}`. Typing clears after 25 s or when the reply lands; send it only when a reply is coming.

### 2.1 Error shape and the codes that matter for caps and bans
https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes
```json
{"error":{"message":"(#131047) Re-engagement message","type":"OAuthException","code":131047,
 "error_data":{"messaging_product":"whatsapp","details":"..."},"fbtrace_id":"..."}}
```
Branch on `code` + `error_data.details`, not HTTP status or `error_subcode` (deprecated). The same
codes also arrive asynchronously in `statuses[].errors[]` (accepted, then failed).

| code | meaning | adapter action |
|---|---|---|
| 0, 10, 131005 | token expired / permission missing | `AUTH` — freeze all lines, alert owner |
| 1, 2, 131000, 131016 | server / temporary | `TRANSIENT` — backoff retry; counts toward watchdog ">10 failed sends/hour" |
| 4, 80007, 130429 | app / WABA / throughput rate limit | `RATE_LIMIT` — back off the whole line |
| 131056 | pair limit: >1 msg per 6 s to one user (bursts of 45 "borrow" quota) | per-recipient queue |
| 131047 | >24 h since the user last replied; free-form refused | `WINDOW_CLOSED` — template or nothing; never retry free-form |
| 131048 | quality-based restriction on the number's send volume | `QUALITY_BLOCK` — stop business-initiated sends on the line, alert |
| 131049 | per-user marketing limit ("healthy ecosystem"); user may be blocked ≤ 24 h | no retry for 24 h; counts in block-rate proxy |
| 131026 | recipient not on WhatsApp / rejected terms / old client | mark `unreachable`, no retry |
| 131031 | WABA restricted or locked (policy) | `ACCOUNT_LOCKED` — freeze line, incident "number banned" (spec 12) |
| 131037, 131045, 133010 | no approved display name / number not registered | onboarding incomplete |
| 132000, 132001, 132012 | template params / unapproved / bad format | `TEMPLATE_INVALID` — tier K, no retry |
| 132015, 132016 | template paused / disabled for low quality | retire template, alert |
| 131008, 131009, 135000 | bad request | bug — log, no retry |

Platform limits the policy layer must model:
- **Messaging limit** = unique users messaged *outside* a customer service window per rolling 24 h, per
  **business portfolio** (shared by all its numbers): 250 new → 2,000 (business or partner verification,
  or 2,000 high-quality template deliveries in 30 days) → 10K → 100K → unlimited by automatic scaling
  (≥ 50 % utilisation over 7 days + high quality; raised within 6 h). Since 2025-10-07 a quality drop no
  longer lowers the limit and the "Flagged" state is gone.
  https://developers.facebook.com/docs/whatsapp/messaging-limits ·
  https://developers.facebook.com/documentation/business-messaging/whatsapp/upcoming-messaging-limits-changes/
- **Throughput**: 80 msg/s per number by default; 200 API calls/h/app/WABA (5,000 for active WABAs).
  https://developers.facebook.com/docs/whatsapp/cloud-api/overview
- **Quality rating** per number: `GREEN | YELLOW | RED | NA | UNKNOWN` via
  `GET /<PHONE_NUMBER_ID>?fields=quality_rating,status,name_status,messaging_limit_tier`. Meta describes it
  as user feedback (blocks, reports) over the last 7 days; the Help Center article body
  (https://www.facebook.com/business/help/896873687365001) could not be fetched — **UNVERIFIED**. No API returns block counts.
- **Enforcement ladder** for policy violations or "excessive negative feedback": warning → 1/3-day
  template block → 5/7/30-day block on all sends → indefinite lock → permanent disable; signalled by
  `account_update`. https://developers.facebook.com/docs/whatsapp/overview/policy-enforcement
- **Per-user marketing cap**: Meta throttles marketing templates to unreceptive users; wait 24 h after
  131049. https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/marketing-templates/per-user-limits/
- **Pricing**: per delivered template since 2025-07-01; free-form inside the window is free; the
  window is 24 h from the user's last message. https://developers.facebook.com/docs/whatsapp/pricing

## 3. Media download (voice notes) and the 7-day deletion rule

https://developers.facebook.com/docs/whatsapp/cloud-api/reference/media
1. Webhook gives `audio.id` (and a `url`; always re-resolve via the id).
2. `GET /<VERSION>/<MEDIA_ID>?phone_number_id=<PHONE_NUMBER_ID>` → `{"url","mime_type","sha256","file_size",
   "id","messaging_product"}`. With `phone_number_id` set, Meta refuses another line's media: it enforces the coat wall.
3. `GET <url>` with `Authorization: Bearer <token>` → binary body. "If you omit your token, the request
   will fail." Verify the webhook `sha256` (base64) against the bytes.
4. Media URLs expire after **5 minutes**; webhook media ids after **7 days**; uploaded media persists
   30 days (`DELETE /<MEDIA_ID>` earlier).
Spec 9/13 (audio deleted after 7 days, transcripts kept): download in the webhook worker at once, store the
OGG at `wa-audio/<coat>/<wamid>.ogg` in the UAE bucket with a 7-day lifecycle rule **and** a nightly sweep
that deletes and logs; keep only transcript, duration, sha256, wamid. Never log the media URL.

## 4. BSP vs direct Cloud API, numbers, display names, "one line per coat"

- **Direct**: Meta business portfolio + app + system-user token; you own the WABA and Meta bills its payment
  method. **Solution Partner (BSP)**: onboards you via Embedded Signup, may invoice on its own credit line,
  usually wraps the API; a **Tech Provider** onboards you but billing stays with Meta. Spec 16 says "through
  a provider": pick one that proxies raw Cloud API payloads, or go direct. https://developers.facebook.com/docs/whatsapp/solution-providers
- **Phone numbers**: owned by you, able to receive the verification code; a new portfolio may register
  2 numbers (20 after business verification or the 2,000 limit). A number on the consumer or Business
  app "cannot be registered unless deleted first", except via **Coexistence** (Business app ≥ 2.24.17,
  partner Embedded Signup): the app stays usable but throughput is fixed at 20 mps, broadcast lists and
  view-once are disabled, history syncs via `smb_message_echoes`. Register with
  `POST /<PHONE_NUMBER_ID>/register {"messaging_product":"whatsapp","pin":"<6 digits>","data_localization_region":"AE"}`
  (AE is supported; 10 calls per number per 72 h).
  https://developers.facebook.com/docs/whatsapp/cloud-api/phone-numbers ·
  https://developers.facebook.com/docs/whatsapp/cloud-api/reference/registration ·
  https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-business-app-users
- **Display name**: required at registration, reviewed (`name_status` ∈ `APPROVED | AVAILABLE_WITHOUT_REVIEW
  | PENDING_REVIEW | DECLINED | EXPIRED | NONE`); sends fail with 131037 until approved; it must represent
  the business, so lines are named after the company, not "Nour". Sending needs `status == CONNECTED`
  (other values seen: `DISCONNECTED | BANNED | UNVERIFIED | FAILED`; full enum **UNVERIFIED**).
- **One line per coat** = one `phone_number_id` per coat plus a dedicated **owner line**. Topologies:
  one WABA, N numbers (simplest; the limit is portfolio-wide anyway; quality is per number; a WABA-level
  `DISABLED_UPDATE` takes every coat down); one WABA per company in one portfolio (isolates WABA bans and
  template namespaces, lets each verified name match its display names — whether Meta requires one legal
  entity per WABA is **UNVERIFIED**; assume yes); separate portfolios (isolates limits too; not for
  phase 0). The week-one task "open the Buzz Avenue WhatsApp Business line" on the phone conflicts with
  Cloud API registration unless Coexistence is used: decide before buying the SIM.

## 5. Recommended Python interface

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

LineId = str            # phone_number_id; the registry maps LineId -> coat
MessageKind = Literal["text", "audio", "image", "document", "reaction", "unsupported"]

@dataclass(frozen=True)
class MediaRef:
    media_id: str; mime_type: str; sha256_b64: str
    filename: str | None = None; caption: str | None = None; voice: bool = False

@dataclass(frozen=True)
class InboundMessage:
    waba_id: str; line: LineId; display_phone_number: str
    message_id: str                              # wamid — idempotency key
    sender_wa_id: str; sender_name: str | None   # name is untrusted display data
    timestamp: datetime; kind: MessageKind
    text: str | None = None; media: MediaRef | None = None
    reaction_to: str | None = None; reaction_emoji: str | None = None
    reply_to: str | None = None; forwarded: bool = False
    errors: tuple["ApiError", ...] = ()

@dataclass(frozen=True)
class StatusEvent:
    waba_id: str; line: LineId; message_id: str; recipient_wa_id: str
    status: Literal["sent", "delivered", "read", "played", "failed"]; timestamp: datetime
    conversation_id: str | None = None; origin: str | None = None    # marketing/utility/service/...
    pricing_category: str | None = None; billable: bool | None = None
    errors: tuple["ApiError", ...] = ()

@dataclass(frozen=True)
class MarketingPreference:   # user_preferences webhook
    line: LineId; wa_id: str; value: Literal["stop", "resume"]; timestamp: datetime

@dataclass(frozen=True)
class AccountEvent:          # account_update / phone_number_quality_update / business_capability_update
    waba_id: str; field: str; event: str | None; payload: dict

WebhookEvent = InboundMessage | StatusEvent | MarketingPreference | AccountEvent

@dataclass(frozen=True)
class ApiError(Exception):
    code: int; details: str; fbtrace_id: str | None = None
    category: Literal["AUTH", "TRANSIENT", "RATE_LIMIT", "WINDOW_CLOSED", "QUALITY_BLOCK",
                      "ACCOUNT_LOCKED", "UNDELIVERABLE", "TEMPLATE_INVALID", "BAD_REQUEST"] = "BAD_REQUEST"
    retry_after_s: int | None = None

@dataclass(frozen=True)
class SendResult:
    message_id: str; wa_id: str
    message_status: Literal["accepted", "held_for_quality_assessment", "paused"]

@dataclass(frozen=True)
class DownloadedMedia:
    media_id: str; mime_type: str; data: bytes; sha256_verified: bool

def parse_webhook(body: bytes, signature: str, *, app_secret: str) -> list[WebhookEvent]:
    """Verify X-Hub-Signature-256 over the RAW body (raise SignatureError), parse every
    entry[].changes[], return events in payload order. Pure; no I/O; safe to call twice."""

class WhatsAppPort(Protocol):
    async def send_text(self, line: LineId, to: str, body: str, *,
                        reply_to: str | None = None, preview_url: bool = False) -> SendResult: ...
    async def send_template(self, line: LineId, to: str, name: str, language: str,
                            components: list[dict], *, category: Literal["utility", "marketing"]) -> SendResult: ...
    async def send_audio(self, line: LineId, to: str, ogg_opus: bytes, *,
                         reply_to: str | None = None) -> SendResult: ...   # upload, then send voice=true
    async def mark_read(self, line: LineId, message_id: str, *, typing: bool = False) -> None: ...
    async def download_media(self, line: LineId, media_id: str) -> DownloadedMedia: ...
    async def line_health(self, line: LineId) -> dict: ...   # quality_rating, status, name_status, tier
```
`parse_webhook` returns `WebhookEvent`, not only `InboundMessage`, because statuses, stops and account
events feed the block-rate and kill-switch logic; the app secret is injected so it stays pure. The port
takes a `line`, never a coat, so the coat wall is enforced by the registry above it; the HTTP adapter
maps table 2.1 onto `ApiError.category` and never retries `WINDOW_CLOSED`, `QUALITY_BLOCK`, `ACCOUNT_LOCKED`, `TEMPLATE_INVALID`.

**In-memory fake** (`FakeWhatsAppPort`) must simulate, so the policy layer is testable without Meta:
- every send recorded per `(line, to)`, returns `accepted` with a synthetic `wamid.`, then emits
  `sent → delivered → read` (`played` for audio) on a controllable clock;
- a 24 h window per `(line, wa_id)` from the last inbound: free-form outside it raises 131047
  (`WINDOW_CLOSED`); templates pass; a second send within 6 s to one user raises 131056;
- a portfolio limit (default 250 unique users / 24 h outside windows) raising 131048; scripted
  "blocked" users that get `sent` but never `delivered`; "limited" users that get `failed` + 131049;
- injectable `MarketingPreference(stop)`, `AccountEvent` (RED quality, `DISABLED_UPDATE`), duplicate and
  out-of-order webhooks, `fail_next(n, code)` for the watchdog test (">10 failed sends/hour → freeze",
  spec 12); `block_rate(line)` computed exactly as the production proxy in section 6.

## 6. Spec compliance notes

- **Opt-out (spec 9, 13)**: three platform signals — `user_preferences.value == "stop"`, a user block
  (visible only as undelivered / 131049) and a reply saying stop. Parse inbound text for stop phrases
  (Arabic, Arabizi, English: "stop", "وقف", "بطل", "unsubscribe", "لا ترسل") and write the `wa_id` to the
  shared do-not-contact list at once (tier A, no model in the loop); a stop blocks every coat's templates
  to that user until an explicit `resume`/opt-in. Policy: "You must respect all requests (either on or
  off WhatsApp) by a person to block, discontinue, or otherwise opt out of communications."
  https://whatsappbusiness.com/policy/ Every consumer template carries a footer or quick-reply
  "reply STOP / وقف"; free-form replies carry it at least once per conversation.
- **Template-first contact**: outside the 24 h window only templates send (131047 otherwise). Keep
  `csw_open_until[(line, wa_id)] = last_inbound_ts + 24h` from the webhook; the planner chooses
  `send_template` when closed, `send_text` when open. Policy also requires prior **opt-in** naming the
  business (https://developers.facebook.com/docs/whatsapp/overview/getting-opt-in): the CRM stores opt-in
  source and time per contact and `send_template` is refused without it. Cadence steps (day 0/3/7/14) are
  `utility` only when tied to something the contact asked for; anything promotional is `marketing`.
- **Daily caps (spec 9)**: Nour's cap (50/line/day, raised weekly while block rate < 1 %) is stricter
  than Meta's 250-per-portfolio floor, so it lives in the policy layer: a counter keyed `(line, Dubai
  date)` over business-initiated sends (templates and any first message outside a window). Replies inside
  an open window do not count but obey the 6-second pair limit and the 09:00–20:00 / prayer-time calendar.
  Raising the cap is a config change proposed in the Monday review with the week's block-rate evidence.
- **Block rate (spec 13; phase-2 gate "< 1 %")**: Meta exposes no block count, so define a per-line 7-day
  proxy: `(stops + failed 131049/131048 + business-initiated sends with no "delivered" within 24 h) /
  unique business-initiated recipients`; also poll `quality_rating` hourly and consume
  `phone_number_quality_update`. Freeze business-initiated sends on a line when proxy ≥ 1 %, quality `RED`,
  or any `ACCOUNT_RESTRICTION`/`DISABLED_UPDATE`; report in the brief; keep answering inbound (it repairs quality).
- **No cold mass messaging**: never send to a `wa_id` without recorded opt-in; never iterate a list;
  the port deliberately has no bulk method; email is the cold channel (spec 9).
- **Owner thread (spec 4, 12, 13)**: a dedicated line; `owner_verified = (line == OWNER_LINE and
  sender_wa_id in OWNER_ALLOWLIST)` is set server-side before the model sees the event; anything else
  on that line is logged and dropped; `profile.name` is never consulted; passphrase and second-channel
  checks sit above the port. Kill switch: pause every outbound queue, keep the webhook alive so the owner's
  release command still arrives, rotate the system-user token in Business Settings (manual runbook), log
  the freeze; do not deregister numbers (10 calls/72 h; re-registration risks the display-name review).
- **Dedupe (spec 4)**: Meta retries for days and batches; key inbox on `message_id`, statuses on `(message_id, status)`; seen-set ≥ 7 days; process after `200`.
- **Data residency (spec 13, 14)**: register each number with `data_localization_region: "AE"`; keep
  media and transcripts in the UAE bucket. What the flag covers beyond message storage is **UNVERIFIED**.
