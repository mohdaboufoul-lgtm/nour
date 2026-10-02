# Nour operations

How to run, deploy and keep Nour alive. The design authority is `docs/SPEC.md`;
section numbers below refer to it. Nothing here overrides the constitution.

## 1. Running locally (SQLite, no Docker)

```bash
cp .env.example .env            # fill in values; .env is git-ignored
make sync                       # uv sync --all-groups --extra postgres
make check                      # ruff + mypy + pytest, the same as CI
uv run nour serve               # event bus + both desks against ./nour.sqlite3
make dryrun                     # 48-hour dry run: drafts and logs, no sends, no spend (§8, §16 gate)
```

`NOUR_DATABASE_URL` defaults to `sqlite+pysqlite:///./nour.sqlite3` in `.env.example`.
SQLite is for development and the unit suite only; anything that reaches a live
channel runs on PostgreSQL. `NOUR_DRY_RUN=true` stays on until the phase 0 gate passes.

## 2. Running with Compose (PostgreSQL)

```bash
make up                         # postgres + brain + auditor, built from ./Dockerfile
COMPOSE_PROFILES=s3 make up     # ... plus MinIO as S3-compatible vault storage (dev only)
docker compose logs -f brain auditor
make test-pg                    # tests marked `postgres` against localhost:5432
make down                       # keeps volumes; `docker compose down -v` wipes them
```

Volumes: `postgres_data` (records, ledger, audit log), `vault` (encrypted vault
objects, brain only), `audit_reports` (auditor findings), `minio_data` (profile `s3`).
Ports bind to `127.0.0.1` only; the public webhook URL terminates at a reverse
proxy or tunnel that forwards to `:8000`.

On the first database start the `audit_role_init` script in `docker-compose.yml`
creates the read-only `nour_audit` role with `SELECT` on schema `audit` only.
Migrations must create the append-only audit tables (§15 `AuditEvent`) in that
schema. The brain's own role should get `INSERT` but never `UPDATE`/`DELETE` on them.

## 3. Environment variables

`.env.example` documents every variable with a one-line comment. Groups:

| Group | Variables |
|---|---|
| Runtime | `NOUR_ENV`, `NOUR_DRY_RUN`, `NOUR_TIMEZONE=Asia/Dubai`, `NOUR_REGION=me-central-1`, `NOUR_CONFIG_DIR`, `NOUR_PROMPTS_DIR` |
| Database | `NOUR_DATABASE_URL` (brain, read/write), `NOUR_AUDIT_DATABASE_URL` (auditor, read-only role), `POSTGRES_*`, `NOUR_AUDIT_DB_PASSWORD` |
| Models (§4) | `NOUR_PRIMARY_MODEL_VENDOR/_MODEL`, `NOUR_FALLBACK_MODEL_VENDOR/_MODEL`, `NOUR_AUDITOR_MODEL_VENDOR/_MODEL`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `NOUR_AUDITOR_API_KEY` |
| Secrets and vault (§11, §13) | `NOUR_SECRETS_BACKEND` (env, aws, azure, vault), `NOUR_VAULT_STORAGE` (local, s3), `NOUR_VAULT_KEY_REF`, `NOUR_S3_*`, `MINIO_ROOT_*` |
| Owner thread and channels (§6, §9) | `NOUR_OWNER_WHATSAPP_NUMBER`, `NOUR_WHATSAPP_APP_SECRET`, `NOUR_WHATSAPP_VERIFY_TOKEN`, `NOUR_WHATSAPP_ACCESS_TOKEN_REF`, `NOUR_SECOND_CHANNEL` (email, desktop) |
| Money (§10) | `NOUR_CARD_ISSUER` (fake or the provider) |

Rules:

- The owner's passphrase is never an environment variable, flag or config value.
  It exists only as an argon2 hash in the `Owner` record and is verified
  server-side before the model sees the event (§4, §6, §15).
- `*_REF` variables are references into the secrets backend, never values. With
  `NOUR_SECRETS_BACKEND=env` (development only) the referenced values are read
  from the environment; in staging and production they come from the cloud
  secrets manager and are scoped and short-lived (§13 token leakage).
- Audit log entries store hashes of inputs and outputs, never secrets or Tier 2
  content (§6, §13). Nothing from `.env` may be logged.

## 4. The two-container split: brain and auditor

| | `brain` | `auditor` |
|---|---|---|
| Command | `nour serve` | `nour auditor run --daily` |
| Role | Event bus, both desks' agent loops, watchdog, kill switch, vault service | Reads the day's audit log independently and flags anything suspicious |
| Model | Primary vendor, with a different vendor as fallback | A different vendor from the primary, its own key (`NOUR_AUDITOR_API_KEY`) |
| Database | Full read/write role | `nour_audit`: `SELECT` on schema `audit` only, `default_transaction_read_only` |
| Vault | Mounts `/data/vault` | No vault mount, no vault key reference |
| WhatsApp | Holds the channel credentials | None |
| Inbound ports | `127.0.0.1:8000` | None |

Why: §4 requires "one append-only log that a separate auditor reads" and §16
"a separate container for the auditor on a different model". An auditor on the
same model would share the primary's blind spots and drift (§13 provider outage
or model drift); one holding the brain's credentials could be steered by the same
prompt injection it is meant to catch (§13 prompt injection: "auditor flags any
action traceable to observed text"). Keeping it to the audit log alone means a
compromised auditor can leak nothing from the vault, the CRM or the owner profile.
Both containers run read-only root filesystems with all capabilities dropped and
mount `./config` and `./prompts` read-only, so no running process can edit its
own constitution.

## 5. Backups and the restore test (§13, §14)

- Daily: `make backup` (`nour backup`) writes an encrypted snapshot of memory, vault,
  ledger, CRM and audit log to a second UAE-region location (a different bucket,
  account or availability zone from the live stores, same region). Schedule it at
  02:00 Dubai time, after the nightly reflection (23:30) has written its lessons.
- Monthly: `make restore-test` (`nour restore --verify`) restores the latest
  backup into a scratch database and vault, checks row counts, audit-log hash chain
  and a sample vault object, then discards the scratch copy. Run it on the first
  working day of each month and record the result for the quarterly review.
- Backup encryption keys live in the secrets manager under their own reference,
  separate from `NOUR_VAULT_KEY_REF`, so a leaked backup is unreadable.
- The constitution, playbooks, skills and config stay as plain files in version
  control so she can be rebuilt on another host or model (§14 model-agnostic core).

## 6. Log and data retention

| Data | Retention | Source |
|---|---|---|
| Voice-note audio | Deleted after 7 days; transcript kept under Tier 1 | §13, §15 `Message` |
| Episodic memory | 12 months, then summarised | §8 |
| Semantic memory | Until corrected | §8 |
| Decision journal, experiment log, audit log | Permanent, append-only | §8, §15 |
| Owner profile | Owner-deletable line by line | §8 |
| Tier 2 content | Never in memory, logs, prompts or the index (metadata only) | §6, §11 |
| Passwords, card numbers, biometrics, UAE Pass, bank logins | Never held | §6 |
| Personal data about contacts | Minimum per contact; deleted on request from any individual | §14 |

Container stdout logs rotate locally (50 MB x 5 files per service) and must not
carry message bodies, Tier 2 content or secrets.

## 7. Secrets rotation

Rotate every credential quarterly and on any incident (§13 token leakage):
vendor API keys (brain and auditor separately), the WhatsApp access token and
app secret, both database passwords, the vault and backup keys, MinIO or object
storage keys, and any OAuth refresh tokens. Rotation is done in the secrets
manager; containers pick up new references on restart. Every rotation is logged
as an audit event and all tokens remain revocable from the server (§13 phone
theft). Failed-passphrase freezes and the kill switch are not affected by rotation.

## 8. UAE-region deployment checklist

Data-residency rule (§14): memory, vault, logs and backups stay in the UAE region,
and Tier 2 processing uses a private model in the same region. No store, replica,
backup, log sink or model endpoint for those stores may be outside it.

- [ ] Region: AWS `me-central-1` (UAE) or Azure `UAE North`; set `NOUR_REGION`.
- [ ] Containers: ECS/Fargate or AKS in that region, one task for `brain`, one for
      `auditor`, same image, separate task roles and separate secret scopes.
- [ ] PostgreSQL: managed instance in-region, TLS required, encryption at rest,
      automated snapshots kept in-region, `nour_audit` role created exactly as in
      `docker-compose.yml`.
- [ ] Vector index with per-desk namespaces in the same region (§8).
- [ ] Vault: in-region object storage with KMS encryption, versioning on, public
      access blocked, bucket policy limited to the brain's task role.
- [ ] Secrets manager in-region (`NOUR_SECRETS_BACKEND=aws` or `azure`); no `.env`
      on production hosts; all `*_REF` values point at it.
- [ ] Backups: daily encrypted snapshot to a second in-region location; monthly
      restore test on the calendar.
- [ ] Logging: audit log off-device and append-only; log sink in-region; no
      cross-region log forwarding or third-party analytics.
- [ ] Private in-region model endpoint configured before any Tier 2 document is read.
- [ ] Ingress: only the webhook path exposed, behind a TLS-terminating proxy or
      tunnel; WhatsApp signature verification on; the database and auditor reachable
      from nothing but the brain's network.
- [ ] Clock: `Asia/Dubai` on every container; cultural calendar loaded (§14).
- [ ] Staging twin provisioned in the same region for testing new skills (§13).
- [ ] Owner thread allowlist, passphrase hash and second channel set up (§6);
      kill switch tested (freezes within 5 seconds); card declines above cap (§16 gate).
- [ ] Break-glass pack updated with the region, account and deputy details (§14).
