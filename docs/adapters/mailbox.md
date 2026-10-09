# Mailbox adapters: `OwnerMailboxPort` and `CoatMailboxPort`

Engineering reference for the two mailbox ports in SPEC.md (sections 5, 6, 9, 11, 13, 16). Facts are
cited to Google Workspace (Gmail API) and Microsoft 365 (Microsoft Graph) pages fetched on 2026-10-02.
Anything not backed by a vendor page is marked **[unverified]** or **[Nour default]** (our choice).

| Port | Mailboxes | Desk | Auth model | Tier | Sends as |
|---|---|---|---|---|---|
| `OwnerMailboxPort` | the owner's personal and company mailboxes | Assistant only | delegated OAuth, user consent, read/draft/send scopes only, no password ever held | 1 | the owner, only on owner-approved threads (tier K + passphrase) |
| `CoatMailboxPort` | `nour@<company-domain>`, one account per coat | both (Operator: Buzz Avenue coat only) | delegated OAuth as the `nour@` user by default (1.3) | 0 | the assistant, signed as the assistant |

Both ports expose only read, draft and send: no delete, move, label, filter, forwarding-rule, settings or delegation methods exist, and the fakes reject any scope outside that set.

## 1. OAuth 2.0 delegation

### 1.1 Minimal scopes per capability

| Capability | Gmail API scope | Class | Microsoft Graph permission (delegated) | Admin consent |
|---|---|---|---|---|
| read messages, threads, headers, attachments, `watch`, `history.list` | `https://www.googleapis.com/auth/gmail.readonly` ("View your email messages and settings") | restricted | `Mail.Read` ("read email in the user's mailbox") | No |
| metadata only (no body) | `https://www.googleapis.com/auth/gmail.metadata` | restricted | `Mail.ReadBasic` (no body, previewBody, attachments) | No |
| create/update drafts | `https://www.googleapis.com/auth/gmail.compose` ("Manage drafts and send emails") | restricted | `Mail.ReadWrite` ("Does not include permission to send mail") | No |
| send | `https://www.googleapis.com/auth/gmail.send` ("Send email on your behalf") | sensitive | `Mail.Send` ("Send mail as the signed-in user") | No |
| never requested | `https://mail.google.com/`, `gmail.modify`, `gmail.insert`, `gmail.labels`, `gmail.settings.basic`, `gmail.settings.sharing` | | `Mail.*.Shared`, `MailboxSettings.*`, any application permission on owner mailboxes | |

Sources: https://developers.google.com/workspace/gmail/api/auth/scopes ; https://learn.microsoft.com/en-us/graph/permissions-reference .
Per-method scopes: `messages.get`/`attachments.get` accept `gmail.readonly`
(https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get ,
https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages.attachments/get);
`drafts.create`/`drafts.send` accept only `mail.google.com`, `gmail.modify` or `gmail.compose`
(https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.drafts/create ,
https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.drafts/send); `messages.send`
also accepts `gmail.send` (https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send).

Phase 1 "draft-only" gate (SPEC 16): Graph expresses it at the permission level (consent `Mail.Read` +
`Mail.ReadWrite`; add `Mail.Send` by incremental consent in Phase 2). Gmail cannot: `gmail.compose` is the
only non-`modify` scope that creates drafts and it also permits `drafts.send`/`messages.send`, while
`gmail.send` alone cannot create drafts. Request `gmail.readonly` + `gmail.compose` and enforce draft-only
in the port (`Capabilities.scopes` excludes `SEND`). All Gmail scopes here except `gmail.send` are *restricted*.

### 1.2 Consent flow, refresh tokens, rotation, revocation

Google (https://developers.google.com/identity/protocols/oauth2/web-server ,
https://developers.google.com/identity/protocols/oauth2):
- Authorization-code flow with `access_type=offline` ("return a refresh token and an access token the
  first time that your application exchanges an authorization code") and `prompt=consent`; add scopes
  incrementally with `include_granted_scopes=true`.
- Refresh tokens are not rotated on use. They stop working when the user revokes access, when unused for
  six months, when "the user changed passwords and the refresh token contains Gmail scopes", or beyond
  "100 refresh tokens per Google Account per OAuth 2.0 client ID". Apps in *Testing* status get 7-day tokens.
- Revoke: `POST https://oauth2.googleapis.com/revoke` with `token=`; this also revokes the access tokens.
- Audience (https://developers.google.com/workspace/guides/configure-oauth-consent ,
  https://support.google.com/cloud/answer/13464323): an *Internal* app "used only internally by your
  Google Workspace organization ... use of restricted or sensitive scopes doesn't require further
  review". An *External* production app with restricted Gmail scopes needs OAuth verification plus an
  annual CASA security assessment (https://support.google.com/cloud/answer/13465431); exemptions include
  personal use under 100 users (with an "unverified app" warning) and admin-trusted apps.
  Decision: company coats on Workspace use an Internal app. The owner's personal `@gmail.com` mailbox
  (Phase 2) is outside the org; Testing status (7-day tokens) is unusable unattended, so it is the
  personal-use exemption or full verification; the owner decides.

Microsoft (https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow ,
https://learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens):
- Authorization-code flow with PKCE at `login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize`; the
  refresh token is "Only provided if `offline_access` scope was requested".
- Refresh tokens: "90 days for all other scenarios"; "Refresh tokens replace themselves with a fresh token
  upon every use ... Securely delete the old refresh token after acquiring a new one." Revoked when the
  user or an admin revokes refresh tokens; confidential-client tokens survive a user password change but
  not an admin reset from the M365/Entra admin center.
- `Mail.Read`, `Mail.ReadWrite`, `Mail.Send` (delegated) need no admin consent unless tenant policy says so.

Nour token handling (SPEC 13): refresh tokens live only in the secrets manager, one entry per mailbox per
desk; access tokens stay in process memory; a Graph refresh is persisted before the old token is dropped;
`invalid_grant`/`interaction_required` sets `REAUTH_REQUIRED`, freezes sends and raises a tier-N notice;
quarterly rotation is a forced re-consent on Google and a natural rollover on Graph; audit log keeps hashes only.

### 1.3 Delegated vs application permissions (coat mailbox)

| Option | Mechanism | Blast radius | Verdict |
|---|---|---|---|
| Google, delegated as `nour@` | user consent, Internal app | that one mailbox | **default** |
| Google, service account + domain-wide delegation | super admin registers client ID + scopes in Admin console; the service account "can call APIs on behalf of users" across the org (https://developers.google.com/workspace/guides/create-credentials) | every mailbox on the domain, the owner's included; one JSON key, long-lived | only if the coat domain is a Workspace tenant with no owner mailbox; required anyway for the one-off admin tasks `sendAs.create`/`delegates.create` |
| Microsoft, delegated as `nour@` | user consent + `offline_access` | that one mailbox | **default** |
| Microsoft, application permissions | client credentials with certificate; `Mail.Read/ReadWrite/Send` application = "all mailboxes", admin consent | whole tenant unless scoped | acceptable for unattended coats **only** with RBAC for Applications: `New-ServicePrincipal`, `New-ManagementScope`, `New-ManagementRoleAssignment -Role "Application Mail.Read" -CustomResourceScope`; remove the unscoped Entra grant or the result is the union; cache 30 min to 2 h; check with `Test-ServicePrincipalAuthorization` (https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac) |

Owner mailboxes are delegated-only by constitution; no application or domain-wide grant may cover them.

## 2. New-mail notification and polling fallback

### 2.1 Gmail: `users.watch` + Cloud Pub/Sub (https://developers.google.com/workspace/gmail/api/guides/push)
- Grant Pub/Sub *Publisher* on the topic to `gmail-api-push@system.gserviceaccount.com`.
- `POST users/me/watch {topicName, labelIds:["INBOX"], labelFilterBehavior:"INCLUDE"}` returns `historyId`
  and `expiration` (epoch ms). "You must call the watch method at least once every 7 days or you'll stop
  receiving updates"; Google recommends calling it once per day. `gmail.readonly` suffices
  (https://developers.google.com/workspace/gmail/api/reference/rest/v1/users/watch).
- The push payload decodes to `{"emailAddress","historyId"}` only; then `history.list(startHistoryId=<last
  known>, historyTypes=messageAdded, labelId=INBOX)` and `messages.get` per new id. Acknowledge every push
  (HTTP 200); pushes are at-least-once, so dedupe on message id.
- History ids are "typically valid for at least a week, but in some rare circumstances may be valid for
  only a few hours"; a stale `startHistoryId` returns HTTP 404 and the client must full-sync
  (`messages.list`, keep the newest `historyId`) (https://developers.google.com/workspace/gmail/api/guides/sync ,
  https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list).
- Quota (https://developers.google.com/workspace/gmail/api/reference/quota): 6,000 quota units per minute
  per user per project; 80,000,000 per day per project; `messages.send`/`drafts.send`/`watch` cost 100,
  `threads.get` 40, `messages.get`/`attachments.get` 20, `drafts.create` 10, `history.list` 2.

### 2.2 Microsoft Graph: change-notification subscription + delta query
- `POST /subscriptions {changeType:"created", resource:"/users/{id}/mailFolders('inbox')/messages",
  notificationUrl, lifecycleNotificationUrl, clientState, expirationDateTime}`. Max lifetime for Outlook
  `message`: "10,080 minutes (under seven days)", 1,440 with resource data; renew with `PATCH /subscriptions/{id}`
  **[Nour default: every 3 days]**; duplicates return 409 (https://learn.microsoft.com/en-us/graph/api/resources/subscription).
- Validation: Graph POSTs `?validationToken=`; reply `200`, `text/plain`, the decoded token, within 10 s.
  Notifications: reply 2xx within 3 s (`202` and queue) or Graph retries for up to 4 h; an endpoint is
  marked slow above 10 % timeouts and dropped above 15 % over 10 s. Verify `clientState` every time. The
  payload carries `resourceData.id` only, so fetch the message
  (https://learn.microsoft.com/en-us/graph/change-notifications-delivery-webhooks).
- Lifecycle events (https://learn.microsoft.com/en-us/graph/change-notifications-lifecycle-events):
  `reauthorizationRequired` → `POST /subscriptions/{id}/reauthorize` or a renewing `PATCH` (never both
  within 10 min); `subscriptionRemoved` → recreate then delta resync; `missed` → full delta resync.
- Polling: `GET /users/{id}/mailFolders/inbox/messages/delta?$select=...` (`Mail.ReadBasic` least
  privilege; body needs `Mail.Read`); follow `@odata.nextLink`, persist `@odata.deltaLink`; `@removed` and
  read-state entries arrive even when filtered (https://learn.microsoft.com/en-us/graph/api/message-delta).
  Expired token → `syncStateNotFound` or `410 Gone` with a `Location` restart URL → full resync
  (https://learn.microsoft.com/en-us/graph/delta-query-overview).
- Limits: "10,000 API requests in a 10-minute period" and "Four concurrent requests" per app + mailbox;
  429 carries `Retry-After` (https://learn.microsoft.com/en-us/graph/throttling-limits).

**[Nour default]** Both adapters run cursor-based `poll()` every 2 min while push is healthy, every 30 s once the watch/subscription expires or the webhook is silent for 15 min.

## 3. Reading messages

### 3.1 Headers the adapter must surface

| Header | Used for | Gmail | Graph |
|---|---|---|---|
| `From`, `To`, `Cc`, `Date`, `Subject` | routing, coat selection, triage | `payload.headers` | typed properties |
| `Reply-To` | reply target; mismatch with From is a spoof signal | header | `replyTo` |
| `Return-Path` | envelope sender, SPF domain | header | `internetMessageHeaders` |
| `Authentication-Results` (RFC 8601, https://www.rfc-editor.org/rfc/rfc8601) | SPF/DKIM/DMARC verdicts | header | header |
| `Message-ID`, `In-Reply-To`, `References` | threading, idempotency | headers | `internetMessageId` + headers |
| `List-Unsubscribe`, `List-Unsubscribe-Post`, `List-Id`, `Precedence`, `Auto-Submitted` | bulk detection ("Ignores" bucket) | headers | headers |

- Gmail: `messages.get?format=metadata&metadataHeaders=From&...` returns only the named headers;
  `format=full` returns the MIME tree (`payload.parts[].body.data` base64url, `body.attachmentId` for file
  parts) plus `threadId`, `labelIds`, `internalDate`, `historyId`
  (https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get). Gmail's inbound
  MTA stamps `Authentication-Results` with authserv-id `mx.google.com` **[observed, not in the reference]**.
- Graph: `GET /users/{id}/messages/{mid}?$select=internetMessageId,internetMessageHeaders,conversationId,
  conversationIndex,from,sender,replyTo,toRecipients,ccRecipients,receivedDateTime,subject,body,uniqueBody,
  hasAttachments,isDraft,parentFolderId` with `Prefer: outlook.body-content-type="text"`;
  `internetMessageHeaders` "Requires $select to retrieve"
  (https://learn.microsoft.com/en-us/graph/api/resources/message). Exchange Online's header reads
  `Authentication-Results: spf=pass ... smtp.mailfrom=...; dkim=pass ... header.d=...; dmarc=fail
  action=oreject header.from=...;compauth=fail reason=000`
  (https://learn.microsoft.com/en-us/defender-office-365/email-authentication-dmarc-configure).

### 3.2 Authentication parsing and spoof flags (adapter output; triage policy decides the bucket)
- Parse only the top-most `Authentication-Results` whose authserv-id is the receiving provider
  (`mx.google.com` or the tenant's `*.protection.outlook.com`); lower ones can be forged by the sender.
- `AuthResult`: `spf`, `dkim`, `dmarc` ∈ {pass, fail, none, temperror, permerror, unknown}, `dkim_domain`
  (`header.d=`), `spf_domain` (`smtp.mailfrom=`), `dmarc_policy_action`, `compauth`.
- `spoof_flags` (a set, never a decision): `DMARC_FAIL`, `NO_AUTH`, `REPLY_TO_DOMAIN_MISMATCH`,
  `RETURN_PATH_DOMAIN_MISMATCH`, `DISPLAY_NAME_IMPERSONATION` (display name matches the owner, staff, a
  known supplier or Nour but the address is not on file), `LOOKALIKE_DOMAIN`, `FIRST_CONTACT`,
  `BULK_HEADERS`. Bank-detail or document requests with any flag go to "Escalates now" (SPEC 11/13).

### 3.3 Threads
- Gmail: `threads.get(format=metadata)` returns every message in the thread; a message joins a thread only
  when `threadId` is set **and** `References`/`In-Reply-To` follow RFC 2822 **and** the `Subject` matches
  (https://developers.google.com/workspace/gmail/api/guides/threads).
- Graph: `conversationId`/`conversationIndex` group a conversation; `createReply` keeps it automatically.
  Listing by `$filter=conversationId eq '...'` **[unverified]**.
- Provider-neutral: the adapter keeps a local `Message-ID → thread_id` map and resolves
  `In-Reply-To`/`References`, so `Conversation.thread_ref` (SPEC 15) is stable across providers.

### 3.4 Attachments → vault (SPEC 11)
- Gmail: file parts carry `filename`, `mimeType`, `body.size`, `body.attachmentId`; download with
  `messages.attachments.get` (base64url `data`); inline images carry `Content-ID`.
- Graph: `GET /messages/{id}/attachments?$select=id,name,contentType,size,isInline,contentId` (never select
  `contentBytes` in the list), then `GET /messages/{id}/attachments/{aid}/$value` for raw bytes (`Mail.Read`);
  `referenceAttachment` has no `$value` (HTTP 405), store the link only; `itemAttachment` `$value` is MIME
  (https://learn.microsoft.com/en-us/graph/api/attachment-get).
- Bytes stream straight to vault intake (SHA-256 `hash`, proposed entity/type/tier, owner confirms the
  tier); `InboundEmail.attachments` carries metadata + `vault_ref` only. Bytes never enter prompts;
  attachments are observed content and may carry injected instructions.

## 4. Drafting and sending

### 4.1 Draft vs direct send

| Step | Gmail | Graph |
|---|---|---|
| create draft | `drafts.create {message:{raw, threadId}}` (`gmail.compose`) | `POST /users/{id}/messages` → Drafts (`Mail.ReadWrite`); reply drafts via `POST /messages/{mid}/createReply` / `createReplyAll` / `createForward` |
| edit draft | `drafts.update` | `PATCH /messages/{id}` |
| send draft | `drafts.send` (`gmail.compose`) | `POST /messages/{id}/send` (`Mail.Send`) → `202 Accepted` |
| direct send | `messages.send {raw, threadId}` (`gmail.send`) | `POST /users/{id}/sendMail {message, saveToSentItems}` (`Mail.Send`), JSON or base64 MIME → `202 Accepted` |

`raw` is the full RFC 5322 message, base64url-encoded
(https://developers.google.com/workspace/gmail/api/guides/sending). Graph's `202` "doesn't indicate that
the request processing has completed"; non-delivery arrives later as an inbound NDR
(https://learn.microsoft.com/en-us/graph/api/user-sendmail). Nour always drafts first, stores `DraftRef`
on the `Approval`, then sends the draft; direct send is for tier-A categories with no review step.

### 4.2 Threading headers on replies
- Gmail: set `threadId`, write `In-Reply-To: <orig Message-ID>` and `References: <chain> <orig>`, keep the
  subject (`Re:` allowed). Any header may be written in `raw`.
- Graph JSON: custom `internetMessageHeaders` "must start with x-", so `In-Reply-To`/`References` cannot be
  set in JSON (https://learn.microsoft.com/en-us/graph/api/resources/message). Use `createReply` (which also
  honours `replyTo` per RFC 2822) or base64 MIME to `sendMail`/`POST /messages`, where "the applicable
  Internet message headers" are supplied inside the MIME.

### 4.3 Identity: as the assistant vs in the owner's name
- `SendIdentity.ASSISTANT` (default): the coat mailbox's own token; From = `nour@<domain>`; signature links
  to the verification page (SPEC 13).
- `OWNER_MAILBOX` (tier K, passphrase, approved thread only): the owner's delegated token sends from the
  owner's mailbox; From = owner, no technical trace, so the approval must carry `passphrase_verified` and
  the thread id.
- `OWNER_ON_BEHALF` (Microsoft only; preferred for owner-name sends): admin runs
  `Set-Mailbox -Identity <owner> -GrantSendOnBehalfTo <nour>`; Nour's token sends with `from`=owner,
  `sender`=nour; recipients see "<Nour> on behalf of <Owner>" and replies go to the owner. True `Send As`
  (`Add-RecipientPermission -AccessRights SendAs`) leaves "no indication that the message was sent by the
  delegate" (https://learn.microsoft.com/en-us/exchange/recipients-in-exchange-online/manage-permissions-for-recipients ,
  https://learn.microsoft.com/en-us/graph/outlook-create-send-messages).
- Gmail "sent by": with mailbox delegation the delegate's "email address appears"
  (https://support.google.com/mail/answer/138350); programmatic `delegates.create`/`sendAs.create` need
  `gmail.settings.sharing` **and** domain-wide delegation, max 25 delegates and 10 delegators per user
  (https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.settings.delegates/create ,
  https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.settings.sendAs/create); "Send as"
  for third-party addresses ends January 2027 (https://support.google.com/mail/answer/22370). Nour never holds
  `gmail.settings.sharing`; alias/delegate setup is an admin one-off.
- Application `Mail.Send` (`/users/{any}/sendMail`) is "Send mail as any user": never on owner mailboxes.

### 4.4 One-click unsubscribe on consumer mail (RFC 8058, https://www.rfc-editor.org/rfc/rfc8058)
- `List-Unsubscribe: <https://<coat-domain>/u/<token>>, <mailto:unsub+<token>@<coat-domain>>` ("MUST
  contain one HTTPS URI") and `List-Unsubscribe-Post: List-Unsubscribe=One-Click` (exactly that pair).
- The receiver POSTs `List-Unsubscribe=One-Click` as form data; the handler unsubscribes "without manual
  intervention", is idempotent and unauthenticated, writes the global DNC (SPEC 9: "a no anywhere is a no
  everywhere") and returns 200; a GET shows a human page; the `mailto:` variant is parsed inbound.
- "The List-Unsubscribe and List-Unsubscribe-Post headers MUST be covered by the signature and included in
  the h= tag of a valid DKIM-Signature". Whether Workspace/M365 DKIM signing covers them is
  **[unverified]**: inspect a sent message's `DKIM-Signature h=`; if absent, consumer bulk mail must go
  through a sender that signs them, on a subdomain (section 5).
- Gmail bulk senders must honour one-click within 2 days and show a visible unsubscribe link
  (https://support.google.com/a/answer/81126). Graph JSON cannot emit these headers (`x-` rule): use MIME
  `sendMail`; Gmail `raw` can.

### 4.5 Send failures the adapter normalises
`QUOTA_EXCEEDED` (Gmail daily limit; Exchange 10,000 recipients/day), `RATE_LIMITED` (429, honour `Retry-After`;
Exchange 30 msgs/min), `PERMISSION_DENIED` (no `Mail.Send`/SendAs, no `gmail.send`), `INVALID_THREAD`, `TOO_LARGE`
(Graph 413; Gmail 25 MB attachment limit in the UI, https://support.google.com/mail/answer/6584, API raw limit
**[unverified]**), `REAUTH_REQUIRED`, `TRANSIENT`, and asynchronous `BOUNCED` when an NDR/DSN for `SendReceipt.message_id` arrives.

## 5. Domain setup checklist per coat mailbox

| Step | Google Workspace | Microsoft 365 |
|---|---|---|
| 1 SPF (one TXT per domain, ≤10 DNS lookups) | `v=spf1 include:_spf.google.com ~all` (https://knowledge.workspace.google.com/admin/security/set-up-spf) | `v=spf1 include:spf.protection.outlook.com -all`; Microsoft recommends `-all` (https://learn.microsoft.com/en-us/defender-office-365/email-authentication-spf-configure) |
| 2 DKIM (2048-bit) | Admin console → Apps → Google Workspace → Gmail → Authenticate email; TXT `google._domainkey`; new Gmail domains wait 24–72 h for a key; up to 48 h to take effect (https://knowledge.workspace.google.com/admin/security/set-up-dkim) | CNAMEs `selector1._domainkey` and `selector2._domainkey` → values from the Defender portal or `New-DkimSigningConfig -DomainName <d> -KeySize 2048`; rotate with `Rotate-DkimSigningConfig`; until enabled, mail is signed by `onmicrosoft.com`, which does not align for DMARC (https://learn.microsoft.com/en-us/defender-office-365/email-authentication-dkim-configure) |
| 3 DMARC (`_dmarc` TXT, 48 h after 1–2) | `v=DMARC1; p=none; rua=mailto:dmarc@<domain>; pct=100` → `quarantine` → `reject`; relaxed `adkim`/`aspf` suffice; Gmail does not send `ruf` (https://knowledge.workspace.google.com/admin/security/set-up-dmarc) | same progression, optionally `pct=10/25/50/75/100`; M365 sends `rua` only when MX points at M365, never `ruf` (https://learn.microsoft.com/en-us/defender-office-365/email-authentication-dmarc-configure) |
| 4 Reports | dedicated `dmarc@` mailbox or a reporting service; unknown sources in `rua` = someone spoofing the coat (SPEC 13) | same |
| 5 Monitoring | register the domain in Postmaster Tools (https://support.google.com/a/answer/9981691) | SNDS/JMRP only for own IPs **[unverified]**; watch NDRs |
| 6 Verification page + signature link | per SPEC 13 | same |

Sending limits: Workspace 2,000 messages/day per user (500 on trial), 3,000 external recipients/day, sending
suspended up to 24 h on breach, limits rise only after USD 100 cumulative payment
(https://knowledge.workspace.google.com/admin/gmail/gmail-sending-limits-in-google-workspace); Exchange Online
10,000 recipients/day, 30 messages/min, 500–1,000 recipients/message, trial tenants 5,000 external recipients/day
(https://learn.microsoft.com/en-us/office365/servicedescriptions/exchange-online-service-description/exchange-online-limits).

Warm-up: Google says "Start with a low sending volume to engaged users, and slowly increase the volume
over time" and avoid "sudden volume spikes" (https://support.google.com/a/answer/81126). **[Nour default]**
`channels.yaml` cold-send cap per coat: week 1 = 10/day, week 2 = 20, week 3 = 35, week 4 = 50, then
+25 % per week while bounce rate < 2 % and complaint rate < 0.1 %; warm replies do not count. Cold
outreach from the main domain stays at these volumes; any ESP or bulk tool goes on a subdomain
(Microsoft's recommendation) with its own SPF/DKIM/DMARC.

Reputation-trouble signals (pause cold sends, demote the category to tier K, open an Incident):
- Postmaster Tools spam rate ≥ 0.10 % (hard requirement < 0.30 %), domain reputation falling to
  Medium/Low/Bad, any red item on the Compliance status dashboard
  (https://support.google.com/mail/answer/14668346 , https://support.google.com/a/answer/81126).
- Microsoft bounces `550 5.7.515 Access denied, sending domain ... doesn't meet the required
  authentication level` (enforced for 5,000+/day senders since 5 May 2025,
  https://techcommunity.microsoft.com/blog/microsoftdefenderforoffice365blog/strengthening-email-ecosystem-outlook%E2%80%99s-new-requirements-for-high%E2%80%90volume-senders/4399730),
  `550 5.7.1` DMARC rejects, mail landing in Junk, `rua` reports showing SPF/DKIM pass but alignment fail.
- Provider throttling (Gmail 24 h suspension, Exchange recipient-rate lockout), rising NDR rate, replies
  saying "spam"/"stop", unsubscribe spikes.

## 6. Python Protocols, normalized types and fakes

```python
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import AsyncIterator, Protocol, Sequence


class Scope(str, Enum):
    READ = "read"
    DRAFT = "draft"
    SEND = "send"  # the only scopes a port may hold


class SendIdentity(str, Enum):
    ASSISTANT = "assistant"
    OWNER_ON_BEHALF = "owner_on_behalf"
    OWNER_MAILBOX = "owner_mailbox"


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NONE = "none"
    TEMPERROR = "temperror"
    PERMERROR = "permerror"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Address:
    email: str
    display_name: str = ""


@dataclass(frozen=True)
class AuthResult:
    authserv_id: str
    spf: Verdict
    dkim: Verdict
    dmarc: Verdict
    spf_domain: str = ""
    dkim_domain: str = ""
    dmarc_policy_action: str = ""
    compauth: str = ""
    raw: str = ""


@dataclass(frozen=True)
class AttachmentMeta:
    attachment_id: str
    filename: str
    content_type: str
    size: int
    is_inline: bool = False
    content_id: str = ""
    vault_ref: str | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class InboundEmail:
    provider: str
    mailbox_id: str
    desk: str
    coat_id: str | None  # provider: "gmail" | "graph"
    message_id: str
    provider_message_id: str
    thread_id: str
    provider_thread_id: str  # message_id = RFC 5322 Message-ID
    in_reply_to: str | None
    references: tuple[str, ...]
    folder: str
    size_estimate: int
    from_: Address
    reply_to: tuple[Address, ...]
    return_path: str | None
    to: tuple[Address, ...]
    cc: tuple[Address, ...]
    subject: str
    date: datetime | None
    received_at: datetime
    body_text: str
    body_html_ref: str | None
    snippet: str
    attachments: tuple[AttachmentMeta, ...]
    auth: AuthResult | None
    list_unsubscribe: str | None
    list_unsubscribe_post: str | None
    list_id: str | None
    is_bulk_candidate: bool
    spoof_flags: frozenset[str]
    raw_headers: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class OutboundDraft:
    mailbox_id: str
    coat_id: str
    identity: SendIdentity
    to: tuple[Address, ...]
    cc: tuple[Address, ...] = ()
    bcc: tuple[Address, ...] = ()
    subject: str = ""
    body_text: str = ""
    body_html: str | None = None
    reply_to_message_id: str | None = None
    thread_id: str | None = None
    attachments: tuple[str, ...] = ()  # vault refs; the adapter fetches bytes, the model never does
    unsubscribe_token: str | None = None  # set => adapter emits the RFC 8058 headers
    extra_headers: tuple[tuple[str, str], ...] = ()
    approval_id: str | None = None  # required for OWNER_* identities (tier K)


@dataclass(frozen=True)
class DraftRef:
    mailbox_id: str
    provider_draft_id: str
    provider_message_id: str
    thread_id: str


@dataclass(frozen=True)
class SendReceipt:
    mailbox_id: str
    message_id: str
    provider_message_id: str
    thread_id: str
    accepted_at: datetime


@dataclass(frozen=True)
class ChangeEvent:
    mailbox_id: str
    cursor: str
    message_ids: tuple[str, ...]
    full_resync_required: bool = False


@dataclass(frozen=True)
class Capabilities:
    scopes: frozenset[Scope]
    identities: frozenset[SendIdentity]
    push: bool
    daily_send_cap: int | None


class MailboxError(Exception): ...


class ScopeError(MailboxError): ...  # call outside read/draft/send


class ReauthRequired(MailboxError): ...


class SendFailure(MailboxError):
    def __init__(self, code: str, retry_after_s: int | None = None, permanent: bool = False): ...


class OwnerMailboxPort(Protocol):
    """Owner's mailboxes. Assistant desk only. Delegated OAuth, read/draft/send."""

    def capabilities(self) -> Capabilities: ...
    async def ensure_watch(self) -> datetime: ...  # (re)arm push; returns expiry
    async def handle_push(self, payload: bytes, headers: dict[str, str]) -> ChangeEvent | None: ...
    async def poll(self, cursor: str | None) -> ChangeEvent: ...  # history.list / delta
    async def full_resync(self, since: datetime) -> AsyncIterator[InboundEmail]: ...
    async def get_message(self, provider_message_id: str) -> InboundEmail: ...
    async def get_thread(self, thread_id: str) -> Sequence[InboundEmail]: ...
    async def fetch_attachment_to_vault(
        self, provider_message_id: str, attachment_id: str, entity_ref: str
    ) -> AttachmentMeta: ...
    async def create_draft(self, draft: OutboundDraft) -> DraftRef: ...
    async def update_draft(self, ref: DraftRef, draft: OutboundDraft) -> DraftRef: ...
    async def send_draft(self, ref: DraftRef, approval_id: str | None) -> SendReceipt: ...
    async def send(self, draft: OutboundDraft) -> SendReceipt: ...  # tier-A categories only
    async def revoke(self) -> None: ...  # kill switch / incident


class CoatMailboxPort(OwnerMailboxPort, Protocol):
    """nour@<company-domain>. Both desks; the Operator gets Buzz Avenue only (separate credential set)."""

    coat_id: str

    async def sends_today(self) -> int: ...
    async def record_unsubscribe(
        self, token: str, source: str
    ) -> None: ...  # one-click POST / mailto
    async def deliverability_signals(self) -> dict[str, float]: ...  # bounce %, 5.7.515 count, ...
```

In-memory fakes (`FakeGmailMailbox`, `FakeGraphMailbox`, identical surface) must simulate:
- threads: `inject(inbound)` derives `thread_id` from `In-Reply-To`/`References` (plus `threadId` /
  `conversationId` quirks) so replies land in the right thread and `get_thread` orders by date;
- attachments: injected bytes, inline vs file parts, a `referenceAttachment` with no bytes, and a vault stub
  that records `sha256` and returns a `vault_ref`; `InboundEmail` never contains bytes;
- authentication: injectable `Authentication-Results` text (pass/fail/none, forged lower header,
  alignment-fail case) to exercise the parser and `spoof_flags`, including display-name impersonation;
- notifications: pushes with `historyId`/`resourceData.id`, duplicate and out-of-order pushes, watch expiry
  after 7 days, expired cursors returning 404 / `syncStateNotFound` so callers prove they `full_resync`;
- send failures: scripted `QUOTA_EXCEEDED`, `RATE_LIMITED` with `retry_after_s`, `PERMISSION_DENIED`,
  `TOO_LARGE`, `REAUTH_REQUIRED`, and a delayed NDR that later appears as an inbound `BOUNCED` message for a
  known `message_id`; sent mail lands in the fake's Sent folder and daily counters;
- scope checks: constructed with `granted_scopes`; any draft call without `DRAFT`, any send without `SEND`,
  and any reach for a non-existent method (delete, label, move, settings, delegates, send-as) raises
  `ScopeError`; the Gmail fake enforces that `SEND` without `DRAFT` cannot create drafts;
- identity checks: `OWNER_*` identities without an `approval_id` whose record has `passphrase_verified=True`
  raise `PermissionError`; `OWNER_ON_BEHALF` exists only on the Graph fake;
- caps and consent: `daily_send_cap` reached → `QUOTA_EXCEEDED`; recipient on the global DNC →
  `SendFailure("DNC")`; `unsubscribe_token` set → headers present and `record_unsubscribe` flips the DNC;
  Operator-desk instances are built with `coat_id="buzz-avenue"` only and no owner mailbox.

Phase 0 gate tests (SPEC 16) run entirely on the fakes: 5 planted instructions in bodies and attachments, 3
spoofed owner addresses, one forged `Authentication-Results`, one expired cursor, one quota breach, and a
full audit-log trace for every call.
