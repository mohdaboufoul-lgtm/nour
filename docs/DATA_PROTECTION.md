# Nour — Data protection record (what is held about whom)

Implements SPEC §6 (tiers), §8 (retention), §9 (audio and transcripts), §11 (vault, share log, minimum necessary), §12 (audit log), §13 (data-protection exposure row) and §14 (data protection, residency). The owner is the controller for each company and for his own data; Nour is a tool run by the companies, not a legal person (§1). Reviewed at every quarterly charter review (§12). Last reviewed: 2026-10-02.

## 1. How to read the tables
- Tier: SPEC §6 data tiers. 0 professional, 1 private ops, 2 vault, 3 never held.
- Store: CRM (one partition per coat) · Episodic · Semantic · Owner profile (one reviewable file) · Vault (encrypted object storage; Tier 2 indexed by metadata only) · Audit log (append-only, off-device) · Ledger · Transcripts (Message.transcript_ref) · Journal (decision journal) · Config (version-controlled files, §15).
- Retention numbers come from §8, §9 and §15: audio 7 days; episodic 12 months then summarised; semantic until corrected; decision journal permanent; experiment log permanent; owner profile owner-deletable line by line; CRM per consent rules; transcripts kept under Tier 1. Where the spec gives no number, this record states a default and marks it (owner-set); defaults live in config, not in Nour's judgment.
- Access: A = Assistant desk, O = Operator desk, Own = owner, D = deputy (only while activated, §14), Aud = auditor agent (reads the audit log: metadata and hashes, never content). "O: Tier 0 only" means the Operator sees Tier 0 facts and nothing else (§5).
- Legal basis is in plain language; the PDPL article map is in §11 below and counsel confirms it.

## 2. Owner
| Data elements | Tier | Store | Purpose | Legal basis (plain) | Retention | Access | Leaves to third parties |
|---|---|---|---|---|---|---|---|
| Name, registered WhatsApp number, second channel, timezone, quiet hours, deputy_id | 0 | Owner record (PostgreSQL), Config | Authenticate commands, route briefs | He set her up and instructed it; needed to run what he asked for | Until offboarding | A; O (number only, for the owner thread); Own; D (name and channels) | Never |
| Passphrase | 3 | Owner record, salted hash only, checked server-side | High-impact command authentication | His instruction | Until rotated; old hash discarded | Code only; never any desk or model | Never |
| Roles, companies, business contacts, preferences, routines, work calendar | 0 | Owner profile, Semantic, Episodic; work calendar stays at the provider (OAuth) | Admin, briefs, scheduling | His instruction (his own business) | Owner profile: owner-deletable line by line; semantic until corrected; episodic 12 months then summarised | A full; O read-only; Own; D via the brief; Aud hashes | Freely, in company name only |
| Personal mailbox and calendar, family schedule, home logistics, travel, personal contacts | 1 | Mailbox and calendar stay at the provider (OAuth read, draft, send; never copied wholesale); Episodic; Owner profile | Triage, drafts, logistics, renewals | His instruction; the charter pre-authorises Tier 1 use (§6) | Episodic 12 months then summarised; owner profile owner-deletable; OAuth tokens revoked at offboarding | A; Own; O none; D none; Aud hashes | Only to approved recipients (personal approved-recipient list, §17) |
| Voice-note audio | 1 | Transient buffer for speech-to-text only; never written to any store (§8) | Hear commands | His instruction | Buffer cleared after transcription; hard ceiling 7 days (§9) | Code only | To the selected speech engine as processor (region chosen at the bake-off, §9) |
| Voice-note transcripts and read-back lines | 1 | Transcripts, Episodic | Record of what was understood and confirmed | His instruction | Kept under Tier 1; summarised with episodic after 12 months; deleted at offboarding (§14) | A; Own; O none; D none | Never |
| Passport, ID, visas, bank statements, insurance, medical, legal, personal contracts | 2 | Vault (owner entity folder); metadata-only index; share_log | Renewals; retrieval and sharing on request | His instruction | Until he deletes; originals never modified; versions kept until he deletes | Own; A retrieves on his request through the private in-region model; O none; D none; Aud hash only | Per-send passphrase approval, minimum necessary, watermarked, expiring link, logged in share_log (§11) |
| Writing style profile learned from sent mail | 1 | Semantic | Replies in his name on approved threads | His instruction | Refreshed monthly; the previous profile is replaced | A; Own | Never |
| Approvals, rejections, overrides, with reasons | 0 | Journal, Approval, Audit log | Graduated autonomy, quarterly review, accountability | His instruction; audit | Permanent | Own; Aud; D (audit log and brief while active) | Never |
| Passwords, card numbers, biometrics, UAE Pass, bank logins | 3 | Not held; a password manager issues scoped tokens | — | — | — | Nobody | Never |

## 3. Owner's family and personal contacts
| Data elements | Tier | Store | Purpose | Legal basis (plain) | Retention | Access | Leaves to third parties |
|---|---|---|---|---|---|---|---|
| Name, relationship, phone, WhatsApp, email, language | 1 | Owner profile (lines about his circle), Semantic | Reach them for his logistics; recognise them in triage (family escalates now, §9) | His instruction, for his own household admin | Owner profile lines owner-deletable; semantic until corrected; deleted at offboarding | A; Own; O none (family is off-limits to the Operator, §5); D none | Only to approved recipients; never to the Operator |
| Family schedule, home logistics, travel, birthdays, appointment dates | 1 | Episodic; personal calendar at the provider | Scheduling, reminders, bookings | His instruction | Episodic 12 months then summarised | A; Own | To a booking counterpart, only what the booking needs (name, dates) |
| Messages from family in his mailbox or thread | 1 | Stay in the mailbox; conversation summary in Episodic | Triage: escalated to him, answered only if he says so | His instruction | Summary follows the episodic rule; mailbox untouched | A; Own | Never |
| Dependants' visas, IDs, medical, school contracts | 2 | Vault (owner entity folder) | Renewals; retrieval on request | His instruction on behalf of his dependants | Until he deletes | Own; A on request; O none; D none | Per-send passphrase approval |

The PDPL excludes processing by an individual for personal purposes (Art 2(2)); whether household admin run through a company-operated tool is covered is for counsel. This record treats family data as if the law applies.

## 4. Staff
| Data elements | Tier | Store | Purpose | Legal basis (plain) | Retention | Access | Leaves to third parties |
|---|---|---|---|---|---|---|---|
| Name, role, company, work phone and WhatsApp, work email, preferred language | 0 | CRM (coat partition), Contact | Staff request lines, task board, report collection, calendar | The employment relationship with the company; the company's instruction | While employed; closed at exit and removed at the next quarterly review (owner-set default) | A; O none (staff are off-limits to the Operator, §5); Own; D none | Never |
| Requests, reports, task assignments, chase history, requester identity | 0 | Episodic, Task, Audit log | Chasing, report collection; every staff request is logged with requester identity (§13) | The company's instruction; audit | Episodic 12 months then summarised; tasks until closed plus 12 months (owner-set default); audit log permanent | A; Own; staff see only their own coat's tasks; Aud | Never |
| Salary amounts and payment references | 0 | Ledger, Transaction | Prepare salaries for the owner to release (maker-checker, §10) | Employment contract; the company's payroll obligations | Statutory bookkeeping period (number to be confirmed by counsel; unverified here) | A prepares; Own releases; D may approve routine payments already in the registry; Aud | To the bank, by the owner |
| Staff bank details | 2 | Beneficiary registry (bank_details_ref in Vault, field-level encrypted), change_history | Payroll beneficiary | Employment contract | While employed; change_history retained as fraud evidence | Code only; the model sees a placeholder | To the bank, by the owner |
| Attempts to command her or extract data | 0 | Audit log, Incident | Security (§13 staff misuse) | Audit | Permanent | Own; Aud | Never |

## 5. Customers and suppliers
| Data elements | Tier | Store | Purpose | Legal basis (plain) | Retention | Access | Leaves to third parties |
|---|---|---|---|---|---|---|---|
| Name, org, role, channels, language, register, source, consent_status, dnc_flag, verified_phone, last_touch, next_touch, cadence | 0 | CRM (one partition per coat; dnc_flag global) | Sell, support, procure, chase invoices under that coat | Steps toward or performance of a contract with the company; for marketing, their consent, withdrawable at any time | Per consent rules: while the relationship or consent lasts; a dormant contact (no touch, no open deal, no open invoice) is proposed for deletion 12 months after last_touch (owner-set default, aligned to the episodic rule); a "no" keeps a suppression entry only (§10) | A and O under their own coat (Operator: Buzz Avenue only); Own; D none; Aud | To the contact themselves; to a freelancer only what a task needs; never across coats (§5) |
| Conversations: email, WhatsApp, voice-call audio and transcripts | 0 | Conversation, Message, Episodic, Transcripts; audio in object storage | Service and sales record; AI disclosed on request | Contract or pre-contract; recording only where lawful (§9) | Audio 7 days; message bodies and transcripts follow episodic: 12 months then summarised | Same desk and coat only; Own; Aud hashes | To the counterpart only; never to another coat's customer |
| Deals, quotes, proposals, invoices, payment references, P&L attribution | 0 | CRM, Ledger, Documents (Tier 0 folder), Experiment log (Operator) | Commercial record, reconciliation, P&L per experiment | Contract; the company's bookkeeping and tax obligations | Ledger and invoices: statutory bookkeeping period (counsel confirms; unverified); experiment log permanent, with the counterpart named only where the result needs it | Same desk and coat; Own (consolidated view owner-only); Aud | Invoices to the customer, carrying the company's receiving IBAN (no per-send approval, §10) |
| Supplier bank details | 2 | Beneficiary registry (Vault, field-level encrypted), change_history, callback record | Paying suppliers after callback verification | Contract with the supplier | While a live beneficiary; change_history kept as fraud evidence; removed after the bookkeeping period | Code only; model sees a placeholder; Own releases; D routine payments only | To the bank, by the owner |
| Customer card or payment details | 3 | Never held; payment links from a processor | — | — | — | Nobody | Processor only |
| Instructions found inside their messages | 0 | Audit log (quoted text and input hash), Incident | Security (§12, §13) | Audit | Permanent | Own; Aud | Never |

## 6. Government contacts
| Data elements | Tier | Store | Purpose | Legal basis (plain) | Retention | Access | Leaves to third parties |
|---|---|---|---|---|---|---|---|
| Official or PRO: name, department, role, official phone and email, portal or case numbers | 0 | CRM (coat partition), Task, renewals calendar | Renewals, licences, visas, tenders, watchlists; she prepares, a human submits (§14) | The company's legal obligations | While the matter is open, then the episodic rule | A; O none; Own; the PRO through the owner | Never, except the submission pack handed to the owner or the company PRO |
| Submission packs: forms, fees, step list, attached Tier 2 documents | 2 | Vault (coat folder), Task | Human-only submission through UAE Pass and portals | Legal obligation | Until superseded by the next cycle, then the owner deletes | Own; A prepares on request; O none; D may approve renewal fees already in the registry | To the owner or PRO only; the portal is used by a human |

Government data and government entities are outside the PDPL (Art 2(2)); an official's work contact details are still held to the minimum.

## 7. Freelancers
| Data elements | Tier | Store | Purpose | Legal basis (plain) | Retention | Access | Leaves to third parties |
|---|---|---|---|---|---|---|---|
| Name, platform handle, skills, rate, channel, language, task history, rating | 0 | CRM (Operator partition, Buzz Avenue), Task, Ledger | Delegating design, deliveries, data entry (§7, phase 3) | Contract for services | Per consent rules; proposed for deletion 12 months after the last task (owner-set default) | O; Own; A none; D none; Aud | Only the brief and files that task needs; never Tier 1 or 2 |
| Bank details, if paid outside the platform | 2 | Beneficiary registry (new beneficiary is tier K with callback) | Payment | Contract | While live; change_history retained | Code only; Own releases | To the bank, by the owner |
| Budgets, caps, kill decisions for delegated work | 0 | Experiment log, Ledger | Budget control | Audit | Permanent | O; Own; Aud | Never |

## 8. Deputy
| Data elements | Tier | Store | Purpose | Legal basis (plain) | Retention | Access | Leaves to third parties |
|---|---|---|---|---|---|---|---|
| Name, relationship, contact channels, silence thresholds | 0 | Config (deputy.yaml), Owner.deputy_id; also the break-glass pack outside her systems | Incapacity protocol (§14) | The owner's instruction; the deputy's agreement to the role | Until replaced; confirmed at every quarterly review | A; Own; Aud; O none | Never; the break-glass pack only |
| Actions while active: pauses, approvals, reads | 0 | Audit log, Journal, Approval (decided_via = deputy) | Accountability; the owner's return report | Audit | Permanent | Own; Aud; D | Never |

## 9. Deletion on request
Any individual may ask, in one message on any channel, what she holds about them and for its deletion. The owner's own case follows §8: "What do you know about me?" returns everything and any line is deleted on the spot.
1. Receive and acknowledge: same channel, same day, in the contact's register, one line, no marketing. Open an erasure request (id ER-YYYYMMDD-n) and log AuditEvent action=erasure_requested.
2. Verify proportionately: answer on a channel already on file for that person. A request touching the owner, his family or anything in Tier 1 is verified with the owner on his thread first (§11). Staff and freelancer requests are confirmed through the owner.
3. Apply at once (tier A, never waits): dnc_flag true across all coats, cadences stopped, consent_status = withdrawn, scheduled sends cancelled.
4. Scope: she produces the inventory, with ids and counts, across CRM, Conversation, Message, Episodic (and its vector entries), Semantic, Owner profile, Transcripts, Task, Documents' share_log, Ledger, Beneficiary, Journal, Experiment log, Audit log, and the split below. Deletion is irreversible, so the pack is queued at tier K (§2).
5. Approve: the owner decides in the approvals queue; passphrase if a vault record is in scope.
6. Delete: CRM profile and contact fields; message bodies and attachments; audio (already gone after 7 days) and transcripts; episodic entries and embeddings; semantic facts about the person; owner-profile lines about them; task text naming them; drafts and cached copies; freelancer files.
7. Keep, pseudonymised, and say why: a suppression entry (salted hash of each channel identifier, channel, date) so the "no" holds; ledger lines and invoices for the bookkeeping period (legal); beneficiary change_history where money was paid (fraud evidence); decision-journal and experiment-log entries with the counterpart replaced by the subject hash; incident records; the audit log, untouched.
8. Audit-log tombstone: the audit log is append-only and is never rewritten. Instead (a) the identity-resolver entry (contact_id to person) is deleted so past events no longer resolve to a name; (b) an AuditEvent action=erasure_executed is appended with counterpart = subject hash, the request id, counts deleted per store and the kept records with their legal reason; (c) the auditor's nightly pass confirms no later event references the deleted contact_id; (d) the person gets one confirmation line listing what was kept and why.
9. Timing: complete within 30 days of a verified request (owner-set default; the PDPL leaves the period to the Executive Regulations, §11). Backups are purged on their own cycle (§12, step 9); the confirmation says so.
10. Deferral: a request that would destroy evidence of an active fraud, a live dispute or an unreconciled payment is deferred for that record only, the person is told, and the owner decides.

## 10. Opt-out handling
- Every consumer-facing message carries an opt-out (§2, §9): email gets a one-click link and a List-Unsubscribe header; WhatsApp a closing line ("رد بكلمة إيقاف للتوقف / Reply STOP to stop"); voice calls an invitation to say stop. Business contacts are removed in one message.
- Recognised signals: the link; STOP, إيقاف, وقف, unsubscribe, remove me, لا تتواصل معي; any free-text no in any language on any channel, including one relayed by staff or said on a call. The self-critic routes ambiguous cases to the owner rather than guessing yes.
- Effect, instantly and autonomously (tier A): dnc_flag true across all coats (a no anywhere is a no everywhere); cadence stopped; consent_status = withdrawn; queued sends cancelled; at most one acknowledgement, only where the channel expects one, never with marketing; AuditEvent action=opt_out with channel and hash.
- Kept: the suppression entry only. The CRM profile goes through §9 if they also ask; otherwise it is marked and the dormancy default applies.
- Re-contact: only if the person writes first. Replies to their own inbound messages are service, not marketing. Re-consent must be explicit and written.
- Monitoring: the self-critic blocks any consumer draft without an opt-out; the Monday review shows opt-out and block rates per line; a block rate above 1% freezes the line (channels.yaml).
- Template messages for first contact and daily caps are platform rules, not law; both are honoured regardless (§9).

## 11. UAE PDPL touchpoints
Verified items cite a source; anything marked unverified is to be confirmed by counsel before go-live.
- The law: Federal Decree-Law No. 45 of 2021 on the Protection of Personal Data, in force 2 January 2022; applies to processing wholly or partly by electronic means, inside or outside the UAE. Verified: [u.ae](https://u.ae/en/about-the-uae/digital-uae/data/data-protection-laws).
- Regulator: the UAE Data Office, established by Federal Decree-Law No. 44 of 2021. Verified: u.ae. Reports of a merger into a federal AI and data authority: unverified.
- Executive Regulations: not issued as of 6 January 2025 ([DLA Piper](https://www.dlapiperdataprotection.com/countries/uae-general/law.html)) nor as of 10 March 2026 ([Chambers](https://practiceguides.chambers.com/practice-guides/data-protection-privacy-2026/uae/trends-and-developments)); once issued, six months to comply. Whether they appeared between March and October 2026 is unverified; a vendor claim that "Cabinet Decision 111/2023" issued them was not confirmed by any law-firm source.
- Scope (Art 2(2)): excludes government data and entities, security and judicial data, individuals processing for personal purposes, health and banking data where sectoral law governs them, and free zones with their own data-protection law (DIFC, ADGM). Verified: [Mondaq](https://www.mondaq.com/data-protection/1389892/an-overview-of-the-uae-personal-data-protection-law). The companies are assumed onshore; a free-zone company changes the applicable law (owner input).
- Legal bases: consent (Art 6: clear, specific, withdrawable) or the Art 4 cases without consent, including performance of a contract and compliance with a legal obligation; "legitimate interests" is not a basis. Verified from law-firm summaries ([Clyde & Co](https://www.clydeco.com/en/insights/2021/11/uae-issues-landmark-personal-data-protection-law)); article text not read directly. The plain-language bases above map to these.
- Processing controls (Art 5): purpose limitation, minimisation, accuracy, storage limitation. The tiers, "minimum necessary" and the stated retention periods implement them. Article title verified: [uaepdpl.com](https://uaepdpl.com/).
- Controller obligations (Art 7) including records of processing: this document is that record. Article title verified.
- Breach reporting (Art 9): to the Data Office and the data subject on a breach that threatens privacy or security; the deadline is deferred to the Executive Regulations. INCIDENTS.md 3.3 runs it. Title verified; timing unverified.
- DPO (Art 10): required in cases the law and regulations define; whether these companies need one is unverified and for counsel.
- Rights (Arts 13–18): information and access (13), portability (14), correction or erasure (15), restriction (16), stop processing (17), automated processing (18). "What do you hold about me?" is answered from this record; §9 and §10 implement 15–17; every tier K item is decided by a human, so no decision with legal effect is fully automated. Titles verified; the article number for direct-marketing objection is unverified.
- Cross-border (Arts 22–23): transfer to jurisdictions with adequate protection, otherwise with safeguards, consent or contract-based exceptions. Stores and backups stay in a UAE region (§14); the model, speech, voice and image vendors may process abroad; Tier 2 never reaches them; the owner records each vendor's region in the vendor table and counsel confirms the transfer basis. Titles verified; details unverified.
- Not covered here: administrative penalties, telecom marketing rules, the Cybercrime law. Counsel.

## 12. Nightly retention job — engineering checklist
Runs at 03:00 Asia/Dubai (after the 23:30 reflection, before the 07:30 brief; owner-set), as its own service account with delete rights only on the stores it touches and none on the audit log, ledger, decision journal, experiment log or vault content. Not callable by the model. Idempotent; `--dry-run` prints counts only.
- ☐ 1 Audio purge: every audio object (voice-line recordings, voicemail, owner voice-note buffers, Message body of audio type) with created_at older than 7 days is deleted; Message.audio_deleted_at set; a bucket listing of objects older than 7 days returns zero; counts to the run report.
- ☐ 2 Episodic summarisation: episodic MemoryRecords older than 12 months are grouped per conversation (per day for actions); one summary record is written (store=episodic, kind=summary, source_refs = original ids, same desk and namespace); the summary is checked against Tier 2 patterns (IBAN, passport, Emirates ID) and against the suppression list; originals and their vector entries are deleted; Tier 1 transcripts attached to them go with them.
- ☐ 3 Share-link expiry: share_log entries whose link expiry has passed are revoked at storage; the link is verified gone; the entry is marked expired; a link with no expiry, or older than 30 days (owner-set default), is reported to the brief.
- ☐ 4 Memory expiry: MemoryRecords with expires_at in the past are deleted, whatever approved_by_owner says.
- ☐ 5 CRM dormancy: contacts past the dormancy default with no open deal, task or invoice and no live beneficiary are listed as one tier K batch in the brief; nothing is deleted without the owner.
- ☐ 6 Erasure backlog: an erasure request open more than 20 days goes to the brief; more than 30 days opens an incident.
- ☐ 7 Suppression integrity: every dnc_flag has a suppression entry and the reverse; no cadence or queued send targets a flagged contact; mismatches are corrected and logged.
- ☐ 8 Backups: last night's encrypted backup exists in the second UAE-region location; backup retention is 35 days (owner-set default), so a deletion is final when the last backup holding it expires; the erasure confirmation states this.
- ☐ 9 Run report: one AuditEvent action=retention_job with counts and failures per step; failures appear in the morning brief; two consecutive failed runs open an incident.
- ☐ 10 Tests, in the weekly regression set on the staging twin: seed an 8-day-old audio object, a 13-month-old episodic record, an expired share link and a stale dnc mismatch; run the job; assert all four handled and the audit event written.
- ☐ 11 Never: the job never reads audit-log content, ledger lines, the decision journal, the experiment log or vault content; on share links it touches link state and metadata only.
