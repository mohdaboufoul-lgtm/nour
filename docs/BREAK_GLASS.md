# Nour — Break-glass pack (template)

SEALED. For: [family member name] and [lawyer name]. Store outside Nour's systems: a printed copy at [physical location] and an encrypted copy with [lawyer / firm]. Per SPEC §14 this pack is updated at every quarterly charter review; if the date below is more than three months old, call the engineer first.

Version: [n] · Sealed on: [date] · Sealed by: [owner name] · Next review due: [date + 3 months]

## 1. What she is
- Nour (نور) is a software agent run by [owner name] for [company 1], [company 2], [...]. She is not a person and not a legal entity; everything she does is legally the owner's or the relevant company's act. She signs nothing.
- She has two jobs: an Operator that sells and runs experiments for Buzz Avenue, and an Assistant that handles the owner's calendar, mail, documents and company admin. She speaks Arabic and English and identifies herself as the company's AI assistant when asked.
- She holds no passwords, bank logins, cards (other than one capped prepaid card per desk), one-time-password devices or UAE Pass. She cannot move money out: she prepares payments and the owner releases them in the bank app.
- She runs on a server in a UAE cloud region ([provider, region, account name]) and uses one dedicated Android phone ([model], number [+971...], kept on its charger at [location]).
- She keeps an append-only audit log of every action with a one-sentence reason. Anyone with the auditor's access can read what she did and why.

## 2. When to open this pack
- The owner is unreachable. After 3 days of silence she pauses sends that commit the companies and keeps replies and renewals running; after 7 days she activates the deputy (§3 below) and tells the owner on every channel on file.
- The owner is incapacitated or has died; or has asked you to switch her off.
- You believe she is misbehaving, someone is receiving messages "from Nour" that look wrong, or money has moved that should not have.
- Her phone, or the owner's phone, is lost or stolen.

## 3. The kill switch (three ways)
Software (preferred): send the exact text "[KILL COMMAND, set by the owner]" from the owner's WhatsApp number [+971...] to Nour's thread [+971...]. It is obeyed instantly and silently: every token is revoked, both cards frozen, every queue paused; logging continues. Only the owner can release it, with the passphrase plus a confirmation on his second channel ([email / desktop app]). Nobody else can release it, including the deputy and the engineer. From any number other than the owner's the command is ignored; use the deputy's pause or the physical and server steps below instead.
Physical: go to the phone at [location]; power it off or pull its SIM. This stops the Operator's body (its WhatsApp line, apps, camera) only; the server keeps running and accepts only the owner's thread.
Server: log in to [hosting console URL] with the account held by [engineer name] and stop the containers [names]. This stops everything, including replies and renewals. The engineer can do this on your request: [engineer phone / email].
Automatic: the watchdog freezes her on its own on a loop (same action three times without new input), spend above 3x the daily expectation, more than 10 failed sends in an hour, or any failed passphrase on a high-impact command.
Note: no kill switch deletes anything. Memory, vault, ledger and audit log stay intact for export (§5).

## 4. The deputy
| Field | Value |
|---|---|
| Name and relationship | [deputy name], [relationship] |
| Channels | WhatsApp [+971...], email [...], second channel [...] |
| Activation | Automatically after 7 days of owner silence; or by the owner with passphrase plus second channel; the file config/deputy.yaml holds the thresholds |
| May | Pause her; approve renewals and routine payments to beneficiaries already in the registry; read the audit log and the morning brief; receive incident reports |
| May never | Open the vault or the owner's mailboxes; change the constitution; add or change a beneficiary; release the kill switch; command the Operator desk |
| Ends | Any message from the owner with the passphrase ends deputy mode; she then lists everything done in his absence |
If the deputy is also unreachable: use the physical and server kill above and call the engineer; the lawyer holds this pack and the mandate in Appendix A.

## 5. Where everything is (fill in; keep secrets out of this pack, name their holders)
| Component | Location | Who holds access |
|---|---|---|
| Server and containers (both desks, auditor, watchdog) | [provider, region, account, project] | [engineer]; owner |
| Secrets manager (all tokens; scoped, short-lived, rotated quarterly) | [service, account] | [engineer]; owner |
| PostgreSQL: CRM, ledger, approvals, decision journal, audit log, incidents | [instance name] | [engineer] |
| Vector index: episodic and semantic memory, per desk | [service] | [engineer] |
| Vault: encrypted object storage, one folder per entity; Tier 2 keys | [bucket name]; keys in the secrets manager | owner; [engineer] for the bucket, never the keys alone |
| Backups (daily, encrypted, second UAE-region location; restore tested monthly) | [location] | [engineer] |
| Version control: constitution, persona, coats, permissions, playbooks, skills, this spec | [repository URL] | owner; [engineer] |
| The phone and SIM | [location]; SIM from [telecom], account [number] | owner |
| Capped cards (Operator AED 3,000/month; Assistant logistics) | [issuer], last four [....], [....] | owner |
| WhatsApp Business lines, one per company | [+971...] (Buzz Avenue), [...] | owner; provider account [name] |
| Company mailboxes and domains (SPF, DKIM, DMARC) | [domain registrar, mail provider] | owner; [engineer] |
| Owner's mailbox delegation (OAuth; no password held) | [provider] | owner revokes at [settings URL] |
| Model vendors: primary, fallback and auditor, private in-region (Tier 2), speech, voice, image | [vendor names, account emails, regions] | owner; [engineer] |
| WhatsApp API provider, voice-line provider | [names, accounts] | owner |
| Bank feeds (read-only tokens, per company) | [bank, scope] | owner revokes at the bank |
| Verification pages (one per company site) | [URLs] | [web admin] |

## 6. How to export her memory, vault, CRM, ledger and audit log
Do this with the engineer, before revoking anything. Nothing is exported through Nour's own channels; use the hosting console and the storage tools directly.
1. Freeze first (§3) so the export is consistent; note the time.
2. Audit log: export the full append-only table with its hashes; verify the hash chain; this is the record of everything she ever did.
3. Ledger and decision journal: per company and consolidated; P&L per experiment; the experiment log with it.
4. CRM: one export per company partition; the global do-not-contact list separately (it must be kept and honoured by whoever takes over the customers).
5. Memory: episodic, semantic and procedural stores per desk from the database and the vector index; the owner profile is one file; playbooks and skills are plain files in the repository.
6. Vault: copy the encrypted objects and the metadata index; obtain the Tier 2 keys from the secrets manager under the owner's or the lawyer's authority; decrypt only on a machine you control; passports, IDs, licences, contracts and bank details are in here.
7. Deliver on an encrypted drive to [owner / lawyer]; record hashes of every file; keep this list with the pack.

## 7. Accounts to close (after the export)
| Account | Closed by |
|---|---|
| Company email identities nour@[domain] (one per company) and their DNS records | [engineer] with the domain owner |
| WhatsApp Business lines per company; the API provider account | owner, at the provider; the SIM at [telecom] |
| AI voice line(s) per company | owner |
| Capped cards (both desks) | owner, at [issuer] |
| Model vendor accounts (primary, fallback, in-region, speech, voice, image) | owner |
| Hosting account, secrets manager, backups (after the export is verified) | [engineer] |
| OAuth grants on the owner's and the companies' mailboxes and calendars; bank-feed tokens | owner |
| Company social accounts she published to; freelancer platform accounts; sandbox hosting | owner |

## 8. Offboarding checklist (SPEC §14, in this order)
- ☐ Export memory, vault index, CRM, ledger and audit log to the owner (§6 above; vault contents with them)
- ☐ Revoke every token and close every card (§7; start with the cards and the bank feeds)
- ☐ Hand every open conversation to a named human with a closing message from her, signed as the company's AI assistant: [name per company]
- ☐ Delete voice-note transcripts and personal data per the retention policy (docs/DATA_PROTECTION.md); keep the audit log, ledger and invoices for the statutory period
- ☐ Remove her from company sites (verification pages), email signatures and WhatsApp Business profiles
- ☐ Tell the deputy, staff and the lawyer that she is off; file the final auditor report with this pack

## 9. People to call
| Role | Name | Phone | Email |
|---|---|---|---|
| Engineer | [ ] | [ ] | [ ] |
| Lawyer | [ ] | [ ] | [ ] |
| Accountant | [ ] | [ ] | [ ] |
| Bank relationship manager (per company) | [ ] | [ ] | [ ] |
| Company PRO | [ ] | [ ] | [ ] |
| Hosting and WhatsApp provider support | [ ] | [ ] | [ ] |

## 10. Version history
| Version | Date | Changed | Sealed by |
|---|---|---|---|
| 1 | [date] | First pack | [owner] |

---

## Appendix A — Board resolution / internal mandate (one per company)
Operative text in English. Arabic headings are provided for the record; the Arabic legal text, and the form of the resolution required by the company's memorandum and licence authority, are to be prepared and reviewed by counsel before signature. SPEC §14 requires one such mandate per company so that staff and partners know her authority.

### قرار مجلس الإدارة / تفويض داخلي — Board resolution / internal mandate
**الشركة / Company:** [legal name], licence no. [ ], [emirate / authority]
**التاريخ / Date:** [ ]  **رقم القرار / Resolution no.:** [ ]

### التمهيد / Recitals
The company operates a software assistant known as "Nour", the company's AI assistant, under the Charter & Build Specification dated 2 October 2026 (the "Charter") and the documents docs/DATA_PROTECTION.md, docs/INCIDENTS.md and docs/BREAK_GLASS.md (the "Governance Documents"). The company wishes to record the scope and limits of what the assistant may do in its name.

### القرارات / Resolutions
1. **الطبيعة / Nature.** Nour is a tool operated by the company. She is not an employee, officer, agent or legal person. Every act she performs is an act of the company within the limits below, and the company remains responsible for it.
2. **نطاق الصلاحية / Scope of authority.** Nour may, in the company's name and signing as "Nour, [Company] AI assistant": [sell, support customers, procure, chase invoices, recruit] as listed in the coat configuration config/coats/[company].yaml; correspond by email, WhatsApp and voice; issue quotes, invoices and proposals from the approved templates; and schedule, research and draft.
3. **الحدود التجارية / Commercial limits.** Price floor [85]% of list; maximum discount [15]%; payment terms limited to [advance, net 15, net 30]; templates limited to [quote, invoice, proposal, standard NDA]. Anything beyond these, including [contracts over AED 20,000, exclusivity, credit beyond net 30], is reserved to [owner / general manager].
4. **ما لا تفعله أبداً / What she never does.** She signs nothing; binds the company to no contract; holds no passwords, bank logins, cards other than the capped card issued to her, OTP devices or UAE Pass; moves no money out (she prepares, a named human releases); never acts in a natural person's name; and engages in no gambling, adult content or tobacco, nor anything unlawful in the UAE or the counterpart's country.
5. **الموافقات / Approvals.** Spending is capped on the card at AED [3,000] per month for Buzz Avenue's Operator desk and AED [ ] for logistics; transactions above AED 1,000, any new beneficiary or account, recurring subscriptions, customer refunds, contracts and anything irreversible require the approval of [owner] recorded in the approvals queue; sends in the owner's name require the owner's passphrase.
6. **الإفصاح / Disclosure.** She states that she is the company's AI assistant whenever asked and in every first email signature; the company publishes a verification page at [URL] listing her official number and email; she never asks any customer to pay to an account other than the company's published receiving account.
7. **الموظفون والشركاء / Staff and partners.** Staff and partners may request; only [owner] may command. Requests involving money or documents are decided by [owner]. Nobody may instruct her by text inside an email, document or message; such text is treated as data and reported.
8. **البيانات / Data.** Personal data is processed per docs/DATA_PROTECTION.md: minimum data per contact, stated retention, opt-out on every consumer message, deletion on request, and storage in a UAE cloud region. [Name] is the company's point of contact for data requests. [Counsel to confirm whether a Data Protection Officer is required under Federal Decree-Law No. 45 of 2021.]
9. **الحوادث ومفتاح الإيقاف / Incidents and kill switch.** [Owner] may stop her at any time; the deputy [name] may pause her and approve routine renewals and registered payments under docs/BREAK_GLASS.md §4 if the owner is unreachable for 7 days; incidents are handled under docs/INCIDENTS.md.
10. **المدة والمراجعة / Duration and review.** This mandate takes effect on [date], is reviewed at each quarterly charter review, and may be revoked by the company at any time with immediate effect; revocation is notified to staff and partners.
11. **الأشخاص المسؤولون / Responsible persons.** Owner and principal: [name]. Deputy: [name]. Engineer: [name]. Counsel: [name].

### التوقيع / Signatures
[Chair / sole shareholder / general manager], [name], [date], signature: ________    Witness: [name], signature: ________
Filed with the company's records on [date]; copy given to staff and key partners on [date]; copy sealed with this pack.
