# Infrastructure adapters: region, secrets, vault, database, vectors, bank feed, ports

Reference for engineers implementing SPEC.md sections 4, 6, 10, 11, 13 and 14. Facts were checked
against vendor documentation on 2026-10-02. Each claim is tagged:

**[V]** verified against the linked official page on that date; **[A]** from an aggregator or news source, confirm in the
provider console; **[U]** unverified (could not be fetched, or a design assumption), do not ship on it without checking.

Hard rules served: every store in a UAE region; scoped short-lived tokens, no secret in any prompt, log or memory;
Tier 2 encrypted per field, indexed by metadata only, never embedded; originals never modified; daily encrypted backups
to a second in-country location, restore-tested monthly, fully drilled annually.

## 1. Region facts

### 1.1 AWS Middle East (UAE), `me-central-1`

Opened 29 Aug 2022 with three Availability Zones [V] (https://aws.amazon.com/blogs/aws/now-open-aws-region-in-the-united-arab-emirates-uae/).
It is an opt-in Region: enable it on the account before anything else [V] (AWS Backup region table below).

| Service | Status in me-central-1 | Source |
|---|---|---|
| Amazon RDS (incl. RDS for PostgreSQL) | available [V] | https://docs.aws.amazon.com/general/latest/gr/rds-service.html |
| Aurora PostgreSQL | listed by aggregator [A] | https://awsfundamentals.com/regions/me-central-1 |
| AWS KMS (incl. FIPS endpoint) | available [V] | https://docs.aws.amazon.com/general/latest/gr/kms.html |
| AWS Secrets Manager | available [V] | https://docs.aws.amazon.com/general/latest/gr/asm.html |
| Amazon S3 | listed by aggregator [A]; S3 is in every commercial region | https://awsfundamentals.com/regions/me-central-1 |
| AWS Backup | available; cross-Region copy, cross-account copy, Audit Manager, restore testing and logically air-gapped vaults all supported; S3 and RDS Multi-AZ backups supported; EKS backups not supported [V] | https://docs.aws.amazon.com/aws-backup/latest/devguide/backup-feature-availability.html |
| Amazon OpenSearch Service (managed domains) | available [V] | https://docs.aws.amazon.com/general/latest/gr/opensearch-service.html |
| Amazon OpenSearch Serverless | **not** in the serverless endpoint table [V] | same page |
| Amazon Bedrock | control-plane endpoint exists [V]; which models are enabled there is not checked [U] | https://docs.aws.amazon.com/general/latest/gr/bedrock.html |
| ECS, EKS, Lambda, AWS Private CA | listed by aggregator [A]; Fargate not shown on that list [U] | https://awsfundamentals.com/regions/me-central-1 |

RDS for PostgreSQL ships pgvector 0.8.2 on PostgreSQL 16, 17 and 18 (latest minors) [V]
(https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-extensions.html).

### 1.2 Azure UAE North and UAE Central

| Region | Location | AZs | Paired region | Access | Source |
|---|---|---|---|---|---|
| UAE North (`uaenorth`) | Dubai | 3 | UAE Central | open | [V] https://learn.microsoft.com/en-us/azure/reliability/regions-list |
| UAE Central (`uaecentral`) | Abu Dhabi | none | UAE North | restricted: request via support ticket for in-country DR | [V] same page; process: https://learn.microsoft.com/en-us/troubleshoot/azure/general/region-access-request-process |

| Service | UAE North | UAE Central | Source |
|---|---|---|---|
| Key Vault, Blob Storage, Azure Backup, AKS | "foundational" services, available in all recommended and alternate regions [V] for the policy; per-region listing not checked [U] | same | https://learn.microsoft.com/en-us/azure/reliability/availability-service-by-category |
| Azure Database for PostgreSQL flexible server | yes; zone-redundant HA, geo-redundant backup [V] | yes (restricted); same-zone HA only; geo-redundant backup [V] | https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/overview |
| pgvector (`vector` extension) | supported via allowlist then `CREATE EXTENSION vector` [V] | same | https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/how-to-use-pgvector |
| Azure AI Search | available with AI enrichment, semantic ranker, availability zones; page footnote on 2026-10-02: region in high demand, new services blocked [V] | not listed | https://learn.microsoft.com/en-us/azure/search/search-region-support |
| Azure OpenAI | support described as very limited in UAE North (community Q&A, 2025) [A] | not listed | https://learn.microsoft.com/en-us/answers/a/1445919 |
| Storage geo-redundancy | GRS/GZRS copies asynchronously to the paired region; the secondary is fixed by the primary and cannot be chosen [V] | - | https://learn.microsoft.com/en-us/azure/storage/common/storage-redundancy |

### 1.3 A second in-country location for backups

| Primary | Second location | How | Notes |
|---|---|---|---|
| Azure UAE North | Azure UAE Central | PostgreSQL geo-redundant backup (set only at server creation, RPO up to 1 h, no PITR at the paired site) [V]; Storage GRS/GZRS [V]; Azure Backup vault with locked immutability [V] | Request UAE Central access first; it has no AZs, so treat it as cold DR only. |
| AWS me-central-1 | A second AWS account in the same Region | AWS Backup cross-account copy (same Organization, destination vault with its own CMK and `backup:CopyIntoBackupVault` policy, re-encrypted on copy) [V] + Vault Lock compliance mode [V] | AWS has no second UAE Region (me-south-1 is Bahrain), so the second location is a separate account and KMS key across the three AZs, not a second site. |
| Either | Off-provider in-country copy | Nightly `pg_dump` + encrypted vault export pushed to the other cloud's UAE region (Azure UAE North from AWS, or an AWS me-central-1 bucket from Azure); Oracle Cloud also runs Dubai and Abu Dhabi regions [A] | This is the only option that survives a whole-provider account loss. Phase 0 recommendation: do it weekly, daily from phase 2. |

Sources: https://docs.aws.amazon.com/aws-backup/latest/devguide/create-cross-account-backup.html, https://docs.aws.amazon.com/aws-backup/latest/devguide/vault-lock.html,
https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-backup-restore, https://learn.microsoft.com/en-us/azure/backup/backup-azure-immutable-vault-concept,
https://www.oracle.com/news/announcement/second-generation-cloud-region-in-the-uae-100120/. Google Cloud has no UAE region [A].

### 1.4 UAE data-residency notes (brief, factual)

- Federal Decree-Law No. 45 of 2021 (PDPL) in force since 2 Jan 2022; applies to mainland and non-financial free zones; DIFC and ADGM have their own regimes (DIFC DP Law No. 5 of 2020, ADGM DP Regulations 2021) that apply only to entities licensed there [A] (https://pinsentmasons.com/out-law/guides/business-in-the-uae-navigating-data-protection-regime). The statute page at https://uaelegislation.gov.ae/en/legislations/1972 could not be fetched [U].
- PDPL does not impose blanket localisation; cross-border transfer is allowed to jurisdictions the UAE Data Office deems adequate or under contractual safeguards [A]. Secondary sources disagree on whether the Executive Regulations have been issued; a Feb 2026 law-firm note says they remain unpublished and cites a 1 Jan 2027 compliance date [U] (https://www.kayrouzandassociates.com/insights/cross-border-data-transfers-under-uae-law-in-2026). Confirm with counsel.
- Sector rules (CBUAE localisation for licensed financial institutions, health-data rules) apply only if one of the owner's companies is in that sector [A].
- For Nour every store is in-country by design, so the transfer question only arises for calls to non-UAE model vendors
  with Tier 0/1 content; Tier 2 never leaves. Record that decision in the quarterly review.

## 2. Secrets manager port

### 2.1 Port

```python
from typing import Protocol, Literal
from dataclasses import dataclass
from datetime import datetime

Desk = Literal["operator", "assistant", "auditor"]


@dataclass(frozen=True)
class SecretRef:  # e.g. "operator/whatsapp/system_user_token"; refs, never values, appear in logs
    path: str


@dataclass(frozen=True)
class Token:  # repr() shows version and expiry only
    value: str  # never logged, never placed in a prompt
    expires_at: datetime  # short-lived: minutes to hours, never "forever"
    version: str  # provider version id; sha256(path + version) goes to the audit log


class ScopedSecrets(Protocol):
    desk: Desk

    def get(
        self, ref: SecretRef
    ) -> Token: ...  # raises Forbidden if ref is outside the desk's prefix
    def refs(self) -> list[SecretRef]: ...  # metadata only


class SecretsPort(Protocol):
    def scoped(self, desk: Desk) -> ScopedSecrets: ...
    def rotate(self, ref: SecretRef, reason: str) -> str: ...  # returns new version
    def revoke_all(self, desk: Desk, reason: str) -> list[str]: ...  # kill switch; returns receipts
```

Rules: the brain containers receive only a `ScopedSecrets`; the scope is enforced by the cloud IAM policy on the
container identity, not by Python. `get()` caches in memory until `expires_at` and never writes to disk.
The auditor's scope contains the read-only DB role and the audit-log read key and nothing else.

### 2.2 AWS mapping

- Naming: `nour/{env}/{desk}/{provider}/{name}` in Secrets Manager; one customer-managed KMS key per desk.
- One IAM task role per ECS task definition: `nour-brain-operator`, `nour-brain-assistant`, `nour-auditor`.
  Each allows `secretsmanager:GetSecretValue` only on `arn:aws:secretsmanager:me-central-1:*:secret:nour/prod/{desk}/*`
  and `kms:Decrypt` only on that desk's key. Credentials are vended to the container through the container credential
  provider and carry the task ARN in CloudTrail [V] (https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-iam-roles.html).
  Task role credentials rotate every 1-6 hours; presigned URLs made with them die with the session [V]
  (https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html).
- Rotation: RDS master credentials use managed rotation (no Lambda, as often as every 4 hours, usually under a minute)
  [V] (https://docs.aws.amazon.com/secretsmanager/latest/userguide/rotate-secrets_managed.html). Application DB users and
  third-party tokens use a Lambda rotation function with the alternating-users strategy. Quarterly schedule:
  `--rotation-rules '{"ScheduleExpression":"cron(0 3 1 1,4,7,10 ? *)","Duration":"2h"}'`.
- On incident: `aws secretsmanager rotate-secret --secret-id <id> --rotate-immediately`, then deny all sessions issued
  before now by attaching the `AWSRevokeOlderSessions` inline policy (`DateLessThan aws:TokenIssueTime`) to each task role
  [V] (https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_use_revoke-sessions.html).

### 2.3 Azure mapping

- One Key Vault per desk per environment (Microsoft's own recommendation: vault per application per environment) with the
  RBAC permission model [V] (https://learn.microsoft.com/en-us/azure/key-vault/general/rbac-guide).
- Each container runs under its own user-assigned managed identity. Role assignments at vault scope:
  brain containers get `Key Vault Secrets User` (read secret values) and `Key Vault Crypto User` (wrap/unwrap with the
  vault key); the auditor identity gets `Key Vault Reader` (metadata only) on the audit vault [V] same page.
- Rotation: every secret carries `--expires`; Key Vault raises `SecretNearExpiry` 30 days before expiry to Event Grid, and a
  Function regenerates the credential and writes a new secret version (dual-credential pattern) [V]
  (https://learn.microsoft.com/en-us/azure/key-vault/secrets/tutorial-rotation-dual). Set validity to 90 days for the
  quarterly rule.
- On incident: `az keyvault secret set-attributes --vault-name <v> --name <n> --enabled false` on the compromised version
  [V] (https://learn.microsoft.com/en-us/cli/azure/keyvault/secret), remove the identity's role assignment, regenerate at the
  provider, then write the new version.

### 2.4 Kill switch: concrete revoke calls per credential type

| Credential | Revoke call | Status |
|---|---|---|
| Google Workspace OAuth (mail, calendar) | `POST https://oauth2.googleapis.com/revoke` with `token=<access or refresh token>`; 200 on success; revoking an access token also revokes its refresh token | [V] https://developers.google.com/identity/protocols/oauth2/native-app |
| Microsoft 365 delegated token (owner mailbox) | `POST https://graph.microsoft.com/v1.0/users/{id}/revokeSignInSessions` (permission `User.RevokeSessions.All`); invalidates all refresh tokens; a few minutes' delay | [V] https://learn.microsoft.com/en-us/graph/api/user-revokesigninsessions |
| Microsoft 365 app-only (client secret) | `POST /applications/{id}/removePassword` body `{"keyId": "<guid>"}` | [V] https://learn.microsoft.com/en-us/graph/api/application-removepassword |
| Meta user access token (Facebook Login) | `DELETE /{user-id}/permissions` invalidates the user's tokens | [V] https://developers.facebook.com/docs/facebook-login/guides/permissions/request-revoke |
| Meta WhatsApp Cloud API system-user token | Revoke/regenerate in Business Settings > System users; no API call found in the fetched docs | [U] https://developers.facebook.com/docs/facebook-login/guides/access-tokens |
| Lean bank connection | `DELETE /customers/v1/{customer_id}/entities/{entity_id}` body `{"reason":"USER_REQUESTED"}`, bearer auth; marks the entity deleted and revokes its permissions | path [V] on the KSA reference https://docs.leantech.me/v2.0-KSA/reference/deleteentity; UAE base URL [U] |
| Tarabut consent | `DELETE /consentInformation/v1/consents/{consentId}`, bearer auth; revokes at Tarabut and at the bank | [V] https://docs.tarabut.com/reference/revokeconsent (Saudi sandbox host shown; UAE host [U]) |
| Desk cards (Alaan, Pemo, Qashio) | Freeze or cancel from the admin dashboard/app; a public freeze API was not found | [A] https://help.alaanpay.com/en/articles/7012158-how-do-i-freeze-my-alaan-card, https://www.pemo.io/post/virtual-corporate-cards-uae, https://www.qashio.com/blog/virtual-vs-physical-cards |
| AWS role sessions | `AWSRevokeOlderSessions` deny policy on each task role; then rotate every secret immediately | [V] see 2.2 |
| Azure identities | Disable the secret versions; delete the identity's role assignments (`az role assignment delete`) | [V] RBAC guide; identity disable [U] |
| Model-vendor API keys | Rotate from the vendor console; no uniform revoke API | [U] |

The kill switch runs every row in parallel, records each receipt (provider, ref, version, timestamp, HTTP status) in the
audit log, and treats a card provider without an API as a hard page to the owner with the dashboard link. A `CardPort`
with `freeze(card_id)` must exist from phase 0 so the provider choice can be swapped.

## 3. Vault storage

### 3.1 Envelope encryption

Per document version (and per Tier 2 field):

1. Generate a data key: AWS `kms:GenerateDataKey` (`AES_256`) with encryption context
   `{"doc_id": ..., "version": ..., "tier": "2", "entity": ...}`; KMS returns plaintext and encrypted copies and never
   stores either [V] (https://docs.aws.amazon.com/kms/latest/developerguide/data-keys.html). The context is non-secret,
   bound to the ciphertext as AAD, required identically on `Decrypt`, logged to CloudTrail, and usable as a
   `kms:EncryptionContext:tier` key-policy condition so only the vault service role may decrypt tier-2 keys [V]
   (https://docs.aws.amazon.com/kms/latest/developerguide/encrypt_context.html).
   On Azure, generate the AES key locally and wrap it with a Key Vault RSA key (`wrapKey`/`unwrapKey`, `RSA-OAEP-256`)
   [V] (https://learn.microsoft.com/en-us/azure/key-vault/keys/about-keys-details); Key Vault has no encryption-context
   equivalent, so the binding below is the only AAD.
2. Encrypt bytes with AES-256-GCM, 12-byte random nonce, 16-byte tag, AAD = `f"{doc_id}:{version}:{tier}"`
   (for fields: `f"{table}:{column}:{row_id}:{tier}"`).
3. Store `{alg, edk, key_id, nonce, tag, aad_fields, sha256_plain, sha256_cipher}` as a JSON header beside the ciphertext.
   Zero the plaintext key immediately after use.
4. Decrypt reverses the path; any AAD mismatch (moved, renamed, re-tiered) fails closed.

The AWS Encryption SDK's default suite (AES-GCM 256, HKDF, ECDSA signing, key commitment, encryption context as AAD) is an
acceptable drop-in if you would rather not hand-roll step 2 [V]
(https://docs.aws.amazon.com/encryption-sdk/latest/developer-guide/supported-algorithms.html); keep the on-disk format
documented so the vault can be read on another host.

### 3.2 Server-side settings

- S3: default encryption SSE-KMS with the vault CMK; bucket policy denies `PutObject` without
  `x-amz-server-side-encryption-aws-kms-key-id`; S3 Bucket Key on (note it switches the service-level encryption context to
  the bucket ARN) [V] (https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingKMSEncryption.html). Versioning on;
  Object Lock enabled at bucket creation (it requires versioning); phase 0 in governance mode with a legal hold on Tier 2
  objects, compliance mode with a per-type default retention once retention periods are agreed; compliance mode cannot be
  shortened even by root [V] (https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html). Block Public Access,
  TLS-only, and a `s3:signatureAge` deny for stale presigned requests.
- Azure Blob: ZRS (plus GZRS when UAE Central is approved); customer-managed key from Key Vault; blob versioning;
  version-level WORM with a time-based retention policy, locked after testing, plus legal holds; soft delete; Shared Key
  disabled; immutable blobs cannot be modified or deleted and overwrites become new versions [V]
  (https://learn.microsoft.com/en-us/azure/storage/blobs/immutable-storage-overview).

### 3.3 Originals never modified

Object key `vault/{entity}/{doc_id}/v{n}.bin` plus `v{n}.json` header. The adapter never overwrites a key: a change is a
new `v{n+1}` object, and the storage versioning is a second line of defence, not the mechanism. The metadata index row
points to `(key, provider_version_id, sha256_plain)`. Deletion is a tombstone row; the object stays until its lock expires.

### 3.4 Share links

- Share only a derivative: watermark (3.5), write it to `shares/{share_id}/{doc_id}.pdf` with its own short retention,
  then sign. Never sign the original key.
- AWS presigned GET: up to 7 days with SigV4 and IAM-user credentials; with role or STS credentials the URL dies when the
  session does [V] (https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html). Sign from a dedicated
  `nour-share-signer` role session with an explicit duration; defaults: Tier 0/1 links 72 h, Tier 2 links 24 h.
- Azure: user delegation SAS (Entra-backed, no account key); revoking the user delegation key invalidates every SAS built
  on it, which is what the kill switch calls [V]
  (https://learn.microsoft.com/en-us/rest/api/storageservices/create-user-delegation-sas).
- Every link: `share_log` row (recipient, purpose, approval ref, expiry, share_id, sha256 of the derivative).

### 3.5 Watermarking

- PDF: build a one-page overlay (recipient, date, share_id, repeated diagonally at low opacity) with reportlab, then stamp it
  with pypdf `merge_page` / `merge_transformed_page` using `over=True` [V]
  (https://pypdf.readthedocs.io/en/stable/user/add-watermark.html). PyMuPDF `insert_text`/`insert_image` with opacity is
  the faster alternative for large files [U, not checked this round].
- Images: Pillow `ImageDraw` text on an RGBA layer, `Image.alpha_composite`, export flattened.
- Also write `share_id` into PDF/XMP metadata and keep the derivative's hash. Visible stamps deter and attribute; they are
  not DRM. The spec's "minimum necessary" rule (page not file, redacted copy not original) does more than any watermark.

### 3.6 Metadata-only index rule for Tier 2

Index row for a Tier 2 document holds exactly: `doc_id, entity, type, tier, title, expiry, version, sha256, size, mime,
created_at, allowed_recipients, share_log_ref`. It never holds OCR text, extracted fields, thumbnails or embeddings.
Enforce three ways: `IndexWriter.index()` raises if `tier == 2 and content is not None`; database
`CHECK (tier <> 2 OR content_tsv IS NULL)`; a test that files a fake passport and asserts the index row has no content.

## 4. Database

### 4.1 Managed PostgreSQL in region

| | AWS RDS for PostgreSQL (me-central-1) | Azure Database for PostgreSQL flexible server (UAE North) |
|---|---|---|
| PITR | any point in the retention period; logs shipped every 5 min; restore creates a new instance [V] https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_PIT.html | 7-35 day retention, RPO up to 5 min, restore creates a new server; no PITR at the geo-paired site [V] https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-backup-restore |
| Second location | AWS Backup copy to a second account's vault, Vault Lock compliance (grace at least 3 days) [V] | geo-redundant backup to UAE Central (creation-time option, RPO up to 1 h) [V]; long-term retention via Azure Backup vault (pg_dump-based, up to 10 years, immutable vault; restore-as-files only) [V] |
| Encryption | KMS CMK on instance and backups; master-password managed rotation [V] | AES-256 always on; CMK supported; LTR supports CMK servers [V] |
| pgvector | 0.8.2 [V] | `vector` via allowlist [V] |

### 4.2 Roles per desk and a read-only audit role

```sql
CREATE ROLE nour_migrator  LOGIN;  -- DDL only, used by CI; owns all tables
CREATE ROLE nour_operator  LOGIN;  -- Operator desk brain
CREATE ROLE nour_assistant LOGIN;  -- Assistant desk brain
CREATE ROLE nour_vault_svc LOGIN;  -- only role that may read/write tier-2 ciphertext columns
CREATE ROLE nour_auditor   LOGIN;  -- SELECT on audit.*, ledger.*, vault_meta.*; nothing else
CREATE ROLE nour_backup    LOGIN;  -- pg_dump: SELECT on all schemas, no DML
GRANT USAGE ON SCHEMA operator  TO nour_operator;   GRANT USAGE ON SCHEMA assistant TO nour_assistant;
GRANT USAGE ON SCHEMA shared, ledger, vault_meta TO nour_operator, nour_assistant;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
```
Credentials for each role are separate secrets under the desk's prefix (2.2/2.3); the brains never hold the master user.

### 4.3 Row-level security on shared tables

```sql
ALTER TABLE shared.contact ENABLE ROW LEVEL SECURITY;
ALTER TABLE shared.contact FORCE  ROW LEVEL SECURITY;      -- applies to the owner too
CREATE POLICY operator_rows  ON shared.contact FOR ALL TO nour_operator
  USING (desk = 'operator' AND coat_id = 'buzz-avenue');    -- Operator may use Buzz Avenue only (SPEC 15)
CREATE POLICY assistant_rows ON shared.contact FOR ALL TO nour_assistant USING (desk = 'assistant');
CREATE POLICY auditor_rows   ON shared.contact FOR SELECT TO nour_auditor USING (true);
```
Superusers bypass RLS and table owners bypass it unless `FORCE` is set, so application roles must never own tables
(https://www.postgresql.org/docs/current/ddl-rowsecurity.html). The global DNC flag lives in a separate table readable by
both desks with a `SELECT`-only policy.

### 4.4 Append-only audit log

```sql
CREATE TABLE audit.event (id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(), desk text, coat_id text,
  kind text NOT NULL, input_hash bytea, output_hash bytea, body jsonb NOT NULL, prev_hash bytea, row_hash bytea);
REVOKE UPDATE, DELETE, TRUNCATE ON audit.event FROM PUBLIC, nour_operator, nour_assistant, nour_auditor, nour_vault_svc;
CREATE FUNCTION audit.block_mutation() RETURNS trigger LANGUAGE plpgsql AS
  $$ BEGIN RAISE EXCEPTION 'audit.event is append-only'; END $$;
CREATE TRIGGER audit_no_mutation BEFORE UPDATE OR DELETE OR TRUNCATE ON audit.event
  FOR EACH STATEMENT EXECUTE FUNCTION audit.block_mutation();
```
A `BEFORE INSERT` row trigger sets `prev_hash` to the previous `row_hash` and `row_hash = sha256(prev_hash, ts, kind,
body)`. The triggers stop the brains and the auditor, not a migrator with DDL rights, so the real control is the nightly
export of the day's rows to object-locked storage (3.2) and the auditor's independent chain verification.

### 4.5 Point-in-time recovery and encrypted copies to the second location

- AWS: RDS automated backups at 35-day retention plus an AWS Backup plan with continuous backup (PITR) and a daily copy
  rule to the second account's locked vault; both vaults encrypted with CMKs; the source CMK is shared to the destination
  account [V] (4.1 sources). Enable Restore testing in me-central-1 for the automated part of 4.6 [V].
- Azure: create the server with geo-redundant backup (cannot be added later) and 35-day retention; add an Azure Backup
  weekly LTR policy to a Backup vault with immutability enabled and then locked [V]. The vault's own storage redundancy
  setting was not checked [U].
- Vault objects and the audit export copy by the same plan (AWS Backup for S3 is supported in me-central-1 [V]; Azure
  GRS/GZRS for Blob [V]).

### 4.6 Scripted monthly restore test

What: the most recent recovery point in the **second** location (never the primary), restored to an isolated instance
`nour-restore-YYYYMM` in a sandbox network with no egress, under the `nour_backup` credentials only.

Verify, in order, and fail the run on the first miss:
1. Restore completes and `SELECT 1` answers within the RTO target (record wall time).
2. Row counts per table match the manifest the backup job wrote (`backup_manifest.json`, kept with the recovery point).
3. `max(ts)` in `audit.event` is within RPO of the recovery-point time; the hash chain verifies end to end.
4. One randomly chosen Tier 2 vault object decrypts under the vault service role (proves key material and AAD survive).
5. One pgvector nearest-neighbour query returns the expected top hit for a fixed probe.
6. Ledger totals per coat equal the figures in that day's morning brief.

Record: insert `audit.event(kind='restore_test', body={recovery_point, location, started, finished, rto_s, checks:[...],
result, operator})`, write the same JSON to `audit-reports/restore/YYYY-MM.json` in the object-locked bucket, and surface
one line in the next morning brief. Then destroy the instance. The annual drill is the same script run from a clean host
with nothing but version control and the second-location backups, timed end to end.

```bash
# make restore-test (sketch; the provider call lives in the BackupPort adapter)
RP=$(nour backup latest --location secondary --json); T=nour-restore-$(date +%Y%m)
nour backup restore --recovery-point "$RP" --target "$T" --isolated && nour restore-check --target "$T" --manifest "$RP" \
  --probe fixtures/vector_probe.json --tier2-sample random --report audit-reports/restore/$(date +%Y-%m).json; nour backup destroy --target "$T"
```

## 5. Vector index with per-desk namespaces

Phase 0 recommendation: pgvector inside the same PostgreSQL, one schema per desk, no managed vector service.

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA vec_operator; CREATE SCHEMA vec_assistant;
CREATE TABLE vec_assistant.chunk (id bigserial PRIMARY KEY, doc_id uuid NOT NULL, coat_id text, tier smallint NOT NULL
  CHECK (tier IN (0, 1)),                       -- Tier 2 is never embedded; this is the structural rule
  embedding vector(1024) NOT NULL, meta jsonb NOT NULL);
CREATE INDEX ON vec_assistant.chunk USING hnsw (embedding vector_cosine_ops);
CREATE TABLE vec_operator.chunk (LIKE vec_assistant.chunk INCLUDING ALL, CHECK (tier = 0));  -- Operator holds Tier 0 only
GRANT USAGE ON SCHEMA vec_operator TO nour_operator; GRANT USAGE ON SCHEMA vec_assistant TO nour_assistant;
```

- Namespaces are schemas plus role grants; neither desk can name the other's schema. The embedding writer refuses any
  chunk whose source document is Tier 2 before it ever calls an embedding model, and a test files a Tier 2 document and
  asserts zero rows in both chunk tables.
- Embedding model must run in region: Bedrock exists in me-central-1 but model availability is unchecked [U]; Azure
  OpenAI in UAE North is limited [A]; the safe phase-0 answer is the private in-region model container the spec already
  requires for Tier 2, serving an open embedding model for Tier 0/1 too.
- Managed alternatives if pgvector outgrows the box: Amazon OpenSearch Service domains (available) rather than Serverless
  (not available) [V]; Azure AI Search in UAE North (available, but capacity-constrained at the time of checking) [V].

## 6. Open banking and the bank feed in the UAE

State of play (checked 2026-10-02):

- CBUAE issued the Open Finance Regulation on 27 June 2024; participation is mandatory for all CBUAE-supervised licensed
  financial institutions; it comprises a Trust Framework, an API Hub and Common Infrastructural Services; it comes into
  effect in phases notified by CBUAE [V] (https://www.centralbank.ae/media/rxfeelkt/cbuaei-2.pdf). Law-firm briefings date
  the Gazette publication to 15 April 2024 [A]
  (https://www.dlapiper.com/en/insights/publications/2024/04/new-fintech-regulations-in-the-united-arab-emirates-open-finance-regulation).
  A tracker reports a replacement Circular No. 03/2025 dated 10 July 2025 [U]
  (https://ozoneapi.com/the-open-finance-tracker/library/cbuae/); the CBUAE rulebook page could not be fetched.
- Nebras Open Finance LLC, a CBUAE subsidiary, was approved in Dec 2024 to operate the central infrastructure [A]
  (https://en.aletihad.ae/news/uae/4536364/cbuae-board-approves-establishment-of--nebras-open-finance); the consumer-facing
  brand is AlTareq (2025) [A] (https://whitesight.net/open-finance-in-the-uae-policies-and-players-powering-the-shift/).
- Third-party providers: Lean Technologies received in-principle approval from CBUAE (reported 29 July 2025) [A]
  (https://www.openbankingexpo.com/news/lean-technologies-gains-regulatory-approval-under-uaes-open-finance-framework/);
  Tarabut likewise (reported 5 Aug 2025) [A]
  (https://thepaypers.com/fintech/news/tarabut-secures-in-principle-approval-from-the-cbuae).
- Banks going live: Commercial Bank of Dubai reported as the first bank live under AlTareq in Dec 2025, with Pay10 and
  Lean [A]; ADIB reported live on 20 Jan 2026 [A]
  (https://www.openbankingexpo.com/news/abu-dhabi-islamic-bank-implements-open-finance-with-support-from-altareq/).
  Coverage is still rolling out bank by bank: check each company's bank before promising a live feed.

Read-only consent scope: Lean's UAE permissions are exactly `identity` (retail only), `accounts`, `balance`,
`transactions`; everything else is forced false [V] (https://docs.leantech.me/v2.0-UAE/docs/creating-a-bank-connection).
Nour requests `accounts, balance, transactions` only; never `identity`, never a payment scope. Consent lifetimes and
re-authentication periods under the AlTareq standards were not verified [U]. Revocation calls are in 2.4.

Statement-export fallback (works today for every bank):

| Format | What it is | Notes |
|---|---|---|
| CSV / XLSX | export from the bank's corporate portal | universal; column names differ per bank; normalise in the parser |
| MT940 / MT942 | SWIFT end-of-day / intraday statement | e.g. Commercial Bank of Dubai publishes MT940/942 via iBusiness, iConnect and SWIFT [A] (https://cbd.ae/docs/librariesprovider2/payments_downloads/mt940-942-format-specifications; PDF not fetched) |
| camt.053 | ISO 20022 XML statement replacing MT940 | richer structured remittance data; ask each bank whether it is offered [A] (https://www.sepaforcorporates.com/swift-for-corporates/the-difference-between-a-camt052-camt053-and-camt054/) |

Reconciliation pipeline: raw statement file -> vault (Tier 2, `type=financial`) -> parser -> `ledger.bank_txn(coat_id,
account_ref, booking_date, value_date, amount, currency, counterparty, reference, bank_txn_id, source, raw_hash)` with a
unique index on `(account_ref, coalesce(bank_txn_id, raw_hash))` -> matcher against `ledger.entry` by amount, reference and a
date window -> unmatched items to the morning brief. The model receives matcher summaries, never the statement file.

```python
class BankFeedPort(Protocol):
    def accounts(self, coat_id: str) -> list[AccountRef]: ...  # last-4 only in briefs
    def transactions(self, account: AccountRef, since: date) -> list[BankTxn]: ...
    def revoke(self, coat_id: str, reason: str) -> list[str]: ...  # called by the kill switch


class StatementImportPort(Protocol):
    def parse(
        self, blob: bytes, fmt: Literal["csv", "mt940", "camt053"], coat_id: str
    ) -> list[BankTxn]: ...
```

## 7. `ObjectStoragePort` and `BackupPort`

```python
@dataclass(frozen=True)
class ObjectRef:
    key: str
    version_id: str
    sha256: str
    size: int


class ObjectStoragePort(Protocol):
    def put_new_version(
        self, key: str, data: bytes, *, metadata: dict[str, str], retain_until: datetime | None
    ) -> ObjectRef:
        ...
        # write-once: raises KeyExists if `key` already has any version

    def get(
        self, ref: ObjectRef
    ) -> bytes: ...  # exact version; raises IntegrityError on sha256 mismatch
    def head(self, key: str) -> ObjectRef | None: ...
    def list_versions(self, key_prefix: str) -> list[ObjectRef]: ...
    def presign_get(
        self, ref: ObjectRef, *, ttl: timedelta, share_id: str
    ) -> str: ...  # ttl capped: 7 d; 24 h for tier 2
    def legal_hold(self, ref: ObjectRef, on: bool) -> None: ...
    def revoke_links(
        self, share_id: str | None = None
    ) -> int: ...  # Azure: revoke delegation key; AWS: rotate signer role


class BackupPort(Protocol):
    def snapshot(
        self, scope: Literal["db", "vault", "audit", "all"]
    ) -> RecoveryPoint: ...  # daily job
    def copy_to_secondary(
        self, rp: RecoveryPoint
    ) -> RecoveryPoint: ...  # second location, re-encrypted
    def latest(self, location: Literal["primary", "secondary"]) -> RecoveryPoint: ...
    def restore(
        self, rp: RecoveryPoint, *, target: str, isolated: bool = True
    ) -> RestoreHandle: ...
    def verify(
        self, handle: RestoreHandle, manifest: dict
    ) -> RestoreReport: ...  # the six checks in 4.6
    def destroy(self, handle: RestoreHandle) -> None: ...
    def export_portable(
        self, rp: RecoveryPoint, dest: ObjectStoragePort
    ) -> ObjectRef: ...  # pg_dump + encrypted vault tar
```

Fake behaviour for tests (`FakeObjectStorage`, `FakeBackup`, in-memory, injectable clock):

- `put_new_version` stores `(key, "v{n}")` with sha256 and raises `KeyExists` on a second put to the same key (the
  "originals never modified" unit test). `get` raises `IntegrityError` if a test corrupts the stored bytes.
- `presign_get` returns `fake://{key}?v=..&exp=..&share=..`; advancing the clock expires it; `ttl` over the tier cap raises.
  `legal_hold(on=True)` refuses deletion; `revoke_links()` kills every issued URL and returns the count (kill-switch test).
- `FakeBackup.snapshot` records `RecoveryPoint(id, taken_at, scope, manifest={table: rowcount}, location="primary")`;
  `copy_to_secondary` returns a new id with `location="secondary"`; `latest("secondary")` raises `NoBackup` until a copy
  exists, so the restore harness provably never reads the primary.
- `restore` yields an in-memory replica; `verify` runs the six checks of 4.6 and returns `RestoreReport(result, checks,
  rto_s)`; tests tamper with the replica to force each check to fail. No fake ever logs bytes or secret values.

## 8. What remains uncertain (carry into the open-decisions list)

1. Aurora PostgreSQL, Fargate and S3 in me-central-1 confirmed only via an aggregator; Key Vault in UAE Central inferred
   from the "foundational" policy. Check the consoles. [A]/[U]
2. Bedrock model availability in me-central-1; Azure OpenAI capacity in UAE North. [U]
3. PDPL Executive Regulations status and any 2027 compliance date. [U]
4. CBUAE Circular 03/2025, the phase timetable, AlTareq consent lifetimes; Lean/Tarabut UAE production hosts. [U]
5. Meta system-user token revocation by API; card-provider freeze APIs; Azure Backup vault redundancy for LTR. [U]
