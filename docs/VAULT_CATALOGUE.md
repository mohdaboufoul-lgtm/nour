# Nour vault — document catalogue and tier register

Reference for `nour/vault/` (DESIGN §3.13: `TierRegister.tier_of(document_type, entity_ref)`, `DocumentMeta`, `VaultStore.expiring`) and the `document` table (DESIGN §5.1). Governing text: SPEC §6 (data tiers), §10 (receiving details), §11 (record fields, indexing, sharing, expiry engine, intake), §14 (UAE rails), §15 (Document entity), §17 (owner inputs). Written 2026-10-02 for a Dubai owner holding several mainland and free-zone companies.

## 0. Conventions

- `document.type` holds the snake_case id from the catalogue (column 1). Each id belongs to one SPEC §11 class (`identity, corporate, financial, contract, insurance, vehicle, property, hr, medical, legal, other`); `class_of(type)` is the inverse of the `types` map in §2. A bare class name is accepted as a type (fixture `tests/fixtures/arabic_commands.yaml` uses `insurance` + `document_subtype: vehicle`) and resolves to the class default; intake should then narrow it (`insurance`/`vehicle` → `motor_insurance`). Existing fixture ids `passport`, `trade_licence`, `bank_statement` are kept as-is.
- `entity_ref` is `owner` or `coat:<slug>` (one folder per entity, §11). Documents about a dependant or an employee are filed under the sponsoring entity with `metadata.subject` (name, relation) — there is no third folder.
- Entity classes for overrides: `owner`, `company_mainland`, `company_free_zone`. The class comes from a proposed `coat.jurisdiction` key (`mainland` | `free_zone:<authority>`), default `mainland` (see §5).
- Tier 2 rows are indexed by metadata only (`CHECK (tier <> 2 OR content_text IS NULL)`). The `metadata` JSON may hold *public identifiers* (licence number, TRN, chamber number, Ejari number, plate, policy number, expiry) so Nour can answer "what is the licence number" without touching content — this is the §11 "a licence number rather than the scan" rule. It never holds IBANs (last4 only), ID numbers of people, salaries or health data.
- Verification markers in the tables: **[A]** confirmed on an authority page cited in §6; **[T]** third-party sources only; **[U]** not verified — treat as a placeholder the owner confirms at intake.
- Reminder payload (SPEC §11, 60/30/7 days): every reminder is a `Task(source="renewal")` carrying `fees_aed` (row value + marker), `forms` (the prerequisite documents as vault refs, metadata only), `portal` (authority + URL), `human_required` (true for every government portal and anything behind UAE Pass, §14), `submitter` (`owner` | `pro` | `nour` | `registrar_account`) and `steps`. The 60-day reminder lists prerequisites that themselves expire inside the window and chains them (motor insurance before registration, Ejari and chamber before licence, licence before establishment cards and bank KYC). The 7-day reminder adds the step list and offers to assemble the pack; Tier 2 content only ever leaves through `DocumentRenderer`/`VaultStore.share` under a typed passphrase.

## 1. Catalogue

Columns: id · name (EN · AR) · §11 class · entity · tier (reason) · validity and UAE rhythm · authority / portal · who submits (§14) · fee AED · what the reminder's `forms`/`steps` must carry.

### 1a. Owner personally (`entity_ref = owner`; dependants as `metadata.subject`)

| `document.type` | Name EN · AR | Class | Tier (why) | Validity / rhythm | Authority / portal | Submits | Fee AED | Renewal pack |
|---|---|---|---|---|---|---|---|---|
| `passport` | Passport · جواز السفر | identity | 2 (§6 "passport and ID") | Term set by issuing state (commonly 5 or 10 y); UAE visa renewal needs ≥6 months left [T] | Issuing consulate in the UAE; no UAE portal | Human (consulate appointment) | Per issuing state [U] | Consulate form, photos, old passport, Emirates ID copy; then two follow-on tasks: re-link new passport to the visa (GDRFA/ICP) and to Emirates ID |
| `emirates_id` | Emirates ID · بطاقة الهوية الإماراتية | identity | 2 | Term = residence permit term (1–3 y; 5/10 y Golden) [A]; renew up to 6 months early; late fee AED 20/day, cap 1,000 [A] | ICP smart services / ICP app https://icp.gov.ae/en/services-details/?serviceid=64afe3c1035448005bd52e5d | Owner (UAE Pass) or PRO at typing centre | AED 100 per year of validity [A] + service/typing ~40–70 + delivery ~20–35 [T] | Renewed with the visa; biometrics appointment if ≥15 y and fingerprints due [A]; passport + visa refs |
| `residence_visa` | Residence visa · تأشيرة الإقامة | identity | 2 (§6 "visas") | Usually 2 y (Dubai employment/investor), 3 y for some sponsors; 30-day grace then fines [T] | GDRFA Dubai https://gdrfad.gov.ae/ (Dubai-issued); ICP elsewhere | Company PRO via GDRFA/Amer for sponsored staff; owner's UAE Pass for self and family | Dubai dependants, expat sponsor, 2 y: AED 460 [A]; employment renewal all-in ~2,000–4,000 [T] | Medical fitness result, health insurance in force (DHA blocks visas without cover), Emirates ID renewal, passport ≥6 months, sponsor's `trade_licence` + `establishment_card_immigration` valid |
| `golden_visa` | Golden visa · الإقامة الذهبية | identity | 2 | 10 y investors/entrepreneurs (5 y some categories); renewable if the condition still holds [A] | ICP/GDRFA golden residency https://u.ae/en/information-and-services/visa-and-emirates-id/residence-visas/golden-visa | Owner (UAE Pass) | Varies by category [U] | Evidence the qualifying condition still holds (capital, property, licence); 10-y Emirates ID |
| `driving_licence` | Driving licence · رخصة القيادة | identity | 2 | Residents 5 y, UAE/GCC nationals 10 y; late fine AED 10/month cap 500 [T] | RTA https://www.rta.ae/ (RTA app, UAE Pass) | Owner (eye test at approved optician, then app) | AED 300 (age 21+) + eye test ~140–180 [T] | Eye-test certificate, valid Emirates ID + visa, fines cleared |
| `family_civil_certificate` | Marriage / birth certificate · شهادة زواج / ميلاد | identity | 2 | No expiry; MOFA attestation needed to sponsor family | MOFA https://www.mofa.gov.ae/ | Human | Attestation ~AED 150/doc [U] | No renewal; expiry engine off; surfaced as a prerequisite of dependant visa renewals |
| `attested_degree_certificate` | Attested degree · شهادة جامعية مصدّقة | identity | 2 | No expiry | MOFA / MoE equivalency | Human | [U] | No renewal; prerequisite for professional licences and some work permits |
| `police_clearance` | Good-conduct certificate · شهادة حسن سيرة وسلوك | legal | 2 | Issued on demand; requesters accept ~3 months [U] | Dubai Police https://www.dubaipolice.gov.ae/ (UAE Pass) | Owner | ~AED 220 [U] | On-demand only, no standing renewal |
| `personal_bank_statement` | Personal bank / card statement · كشف حساب شخصي | financial | 2 ("bank statements"; PAN never stored — Tier 3) | Monthly | Bank app / read-only feed | n/a | — | Not a renewal; intake masks card numbers to last4 |
| `personal_tenancy_contract` | Home tenancy + Ejari · عقد إيجار السكن / إيجاري | contract | 2 (contract; address is Tier 1 metadata) | Annual; Ejari re-registered each term [A] | DLD Ejari via Dubai REST https://dubailand.gov.ae/en/eservices/register-renew-ejari-contract/ | Owner (UAE Pass) or landlord/agent | AED 100 + 10 knowledge + 10 innovation; + partner AED 55/95 + VAT ≈ 177.75 (app) / 220 (trustee) [A] | Signed contract, landlord title deed + ID, tenant Emirates ID, DEWA premise no., cheque schedule; 90-day rent-change notice window |
| `title_deed` | Title deed · سند ملكية | property | 2 | No expiry | DLD https://dubailand.gov.ae/ | Owner | — | No renewal; service-charge invoices file as `household_bill` |
| `vehicle_registration` | Registration card (Mulkiya) · ملكية المركبة | vehicle | 2 (ID-linked; plate is Tier 1 metadata) | Annual; renew ≤150 days early; 30-day grace; annual test for vehicles >3 y [T] | RTA https://www.rta.ae/wpsv5/links/vehicle-renewal/en/vehicle-renewal.html [A process] | Owner (RTA app/UAE Pass or testing centre); Nour lists fines only | AED 350 light vehicle + ~20 knowledge/innovation + test ~150–170 + card ~20–25 [T] | `motor_insurance` renewed first (prerequisite), fines cleared, test certificate, Emirates ID |
| `motor_insurance` | Motor insurance · بوليصة تأمين المركبة | insurance | 2 (§6 "insurance") | Annual, usually a 13-month policy (12 + 1 grace) [T] | Insurer / broker | Nour gathers quotes; purchase is spend-tiered and owner-signed | Market [U] | Prior policy no., plate, chassis, Mulkiya, driving licence, no-claims letter; schedule ≥30 days before Mulkiya expiry |
| `health_insurance` | Health insurance (owner, dependants) · التأمين الصحي | insurance | 2 (insurance + medical) | Annual; DHA–GDRFA link: no Dubai visa issue/renewal without cover [T] | DHA https://www.dha.gov.ae/ ; insurer | Owner/PRO; Nour prepares comparison | Market; Dubai basic plan historically ~AED 500–700/yr [U, old figure] | Policy no., member list, Emirates IDs, sponsor licence; align to visa expiry |
| `medical_record` | Medical record / report · سجل طبي | medical | 2 | None | Hospital / DHA | n/a | — | Never shared without the owner; no renewal |
| `will` | Registered will · وصية مسجلة | legal | 2 | No expiry; review at life events | DIFC Wills Service https://www.difccourts.ae/ ; Dubai Courts / ADJD | Owner (appointment, witnesses) | Single full will AED 10,000; guardianship 5,000; mirror up to 15,000; no annual fee [T citing DIFC schedule] | Review trigger instead of expiry: new company, property, child |
| `power_of_attorney` | Power of attorney · وكالة | legal | 2 | As drafted; Dubai Courts e-notary property POA valid until revoked unless dated (Circular 29/R/2025) [U]; sale POAs commonly 2 y [T] | Dubai Courts Notary https://www.dc.gov.ae/ (UAE Pass) | Owner | Notary ~AED 250–500 + translation [T; Exec. Council Res. 4/2014] | Arabic+English draft, attorney ID, licence + MOA for corporate POA; expiry from the deed |
| `tax_residency_certificate` | Tax residency certificate · شهادة الإقامة الضريبية | financial | 2 | Covers one tax period / 12 months; reapply yearly [A] | FTA EmaraTax → TRC platform https://tax.gov.ae/en/services/issuance.of.tax.certificates.aspx | Owner / tax agent (EmaraTax via UAE Pass) | AED 50 submission + 500 (CT-registered) / 1,000 (natural person, no TRN) / 1,750 (legal person, no TRN); hard copy 250 [A] | ICP entry/exit report, Ejari, bank statements, salary certificate (all Tier 2 refs); 10 business days |
| `household_bill` | Household bill / school fees · فاتورة منزلية / رسوم مدرسية | other | 1 (home logistics) | Monthly / per term | DEWA, du/e&, school portals | Nour pays within spend tiers; new payee = K | — | Not a renewal; feeds the ledger |
| `travel_document` | Bookings, itineraries · مستندات السفر | other | 1 | Trip-bound | Airlines / hotels | Nour (A/N by spend) | — | Reminder = check-in and entry rules, not renewal |
| `uae_pass` | UAE Pass · الهوية الرقمية | — | 3 never held (§6, §14) | Personal, biometric, device-bound [A] | https://u.ae/en/about-the-uae/digital-uae/digital-transformation/platforms-and-apps/the-uae-pass-app | Owner only, always | — | Intake refuses; every portal row assumes the owner or PRO signs in |
| `credential` | Passwords, card PANs, bank logins, OTPs · كلمات المرور وبيانات البطاقات | — | 3 never held | — | Password manager issues scoped tokens | — | — | Intake refuses; LeakGuard fingerprints the value if it appears |

### 1b. Each company (`entity_ref = coat:<slug>`; mainland and free zone unless stated)

| `document.type` | Name EN · AR | Class | Tier (why) | Validity / rhythm | Authority / portal | Submits | Fee AED | Renewal pack |
|---|---|---|---|---|---|---|---|---|
| `trade_licence` | Trade licence · الرخصة التجارية | corporate | 2 (§6 names it; licence no., activities, expiry are Tier 0 metadata) | Annual (some zones sell multi-year); fines after lapse [T] | Mainland: DET / Invest in Dubai https://invest.dubai.ae/ (UAE Pass); free zone: member portal (DMCC, IFZA, Meydan, DSO…) | Owner (UAE Pass) or PRO; zone portals use a company account but approval and payment stay human | Mainland all-in ~8,000–15,000+ [T]; free zone per package [U] | Mainland: `office_tenancy_contract` Ejari valid, `chamber_membership_certificate`, cards, no fines. Free zone: `free_zone_lease`, `audited_financial_statements` filed, visa quota clear. After renewal cascade: chamber, both establishment cards, bank KYC, insurers |
| `establishment_card_immigration` | Establishment (immigration) card · بطاقة المنشأة (الإقامة) | corporate | 2 | 1 y [T]; late fine reported AED 500/month, another source says 100 [T, conflicting] | Mainland: GDRFA Dubai https://gdrfad.gov.ae/ ; free zone: ICP via the zone, bundled with licence | PRO | GDRFA ~300–500 + typing 50–150 [T]; ICP ~1,300 (100 + 100/yr + 1,000 e-system + 100 smart) [T] | Renewed `trade_licence`, signatory Emirates ID, MOA; do right after the licence |
| `establishment_card_labour` | MOHRE establishment card / labour file · بطاقة المنشأة (العمل) | corporate | 2 | Annual [U]; free-zone companies usually handled by the zone | MOHRE https://www.mohre.gov.ae/ (UAE Pass-only login) | PRO / owner UAE Pass | [U] | Renewed licence, immigration card, signatory Emirates ID; needed before any `work_permit` renewal |
| `certificate_of_incorporation` | Certificate of incorporation / registration · شهادة التأسيس | corporate | 2 (class default; registration no. is Tier 0 metadata) | No expiry | DET / zone registrar | n/a | — | No renewal; part of bank KYC packs |
| `memorandum_of_association` | MOA / AOA · عقد التأسيس / النظام الأساسي | corporate | 2 (contract; shareholdings) | No expiry; amended by notarised addendum | Dubai Courts notary / zone registrar | Owner (notary, UAE Pass) | Notary per Exec. Council Res. 4/2014 [T] | Any amendment → `ubo_register` and `shareholder_register` update within 15 days [A] |
| `shareholder_register` | Share certificates / register · شهادة أسهم / سجل المساهمين | corporate | 2 | No expiry; keep current | Company / zone registrar | Owner | — | Update on transfer; feeds UBO filing |
| `ubo_register` | UBO register · سجل المستفيد الحقيقي | corporate | 2 | Keep current; notify registrar within 15 days of change (Cabinet Decision 109/2023) [A]; some zones require annual confirmation [U]; ESR discontinued for FYs ending after 31 Dec 2022 (Cabinet Decision 98/2024) [T] | DET / zone registrar portal; law text https://uaelegislation.gov.ae/en/legislations/2314 | Owner / PRO | Penalties from AED 20,000 at second offence (CD 132/2023) [T] | Trigger on MOA change, share transfer, new manager; attach updated register |
| `board_resolution` | Board / shareholder resolution · قرار مجلس الإدارة / الشركاء | corporate | 2 | No expiry; AGM cycle annual (board-meeting cycle runs on the expiry engine, §11) | Company; notarised where a registrar needs it | Owner signs | — | Annual: approve FS, renew mandates, reconfirm `ai_representation_mandate` at the quarterly review |
| `ai_representation_mandate` | AI assistant mandate · تفويض المساعد الرقمي | corporate | 0 (meant to be shown to staff and partners, §14) | Reviewed each quarterly charter review | Company (internal) | Owner signs | — | Carries the current constitution hash; reminder at review date |
| `chamber_membership_certificate` | Dubai Chamber membership · عضوية غرفة دبي | corporate | 0 (public membership) | Annual; renewal needs the renewed licence copy, ~2 h approval [A] | Dubai Chambers https://www.dubaichambers.com/en/membership-renewal | PRO / owner (company account) | AED 300–2,200 by activity; general trading / free zone 1,200–2,200 [A] | Renewed `trade_licence` copy; immediately after the licence (mainland mandatory, free zone optional) |
| `free_zone_lease` | Free-zone lease / flexi-desk · عقد إيجار المنطقة الحرة | contract | 2 (free zone only) | Annual, bundled with licence | Zone portal | Owner / zone account | Per zone [U] | Prerequisite of licence renewal; expiry = licence expiry |
| `office_tenancy_contract` | Office tenancy + Ejari · عقد إيجار المكتب / إيجاري | contract | 2 (contract; Ejari no. is Tier 0 metadata — DET needs it) | Annual; Ejari mandatory for DET renewal [T] | DLD Ejari via Dubai REST [A] | Owner (UAE Pass) or landlord/agent | AED 100 + 10 + 10 (+ partner fee + VAT ≈ 177.75 / 220) [A] | Signed lease, landlord title deed, trade licence, signatory ID; Ejari certificate before licence renewal |
| `vat_registration_certificate` | VAT registration (TRN) · شهادة التسجيل في ضريبة القيمة المضافة | financial | 0 (TRN is printed on every invoice) | No expiry; amend registration details within 20 business days of a change [U] | FTA EmaraTax https://eservices.tax.gov.ae/ (UAE Pass) | Owner / tax agent | — | Not a renewal; TRN → Tier 0 metadata used by the invoice renderer |
| `vat_return` | VAT return + payment · إقرار ضريبة القيمة المضافة | financial | 2 (revenue data) | Quarterly (turnover < AED 150m) or monthly; due 28 days after period end, next business day if weekend/holiday [A] | FTA EmaraTax https://u.ae/en/information-and-services/finance-and-investment/taxation/vat/filing-a-tax-return-for-vat | Accountant / owner via EmaraTax; Nour prepares the ledger extract | Nil; late penalties [U] | Sales/purchase ledgers, tax invoices, prior return; payment released by owner (maker-checker); 60/30/7 counted from the 28th |
| `corporate_tax_registration_certificate` | Corporate tax registration · شهادة تسجيل ضريبة الشركات | financial | 0 | No expiry | FTA EmaraTax | Owner / tax agent | — | Not a renewal; late-registration penalty AED 10,000 [U] |
| `corporate_tax_return` | Corporate tax return · إقرار ضريبة الشركات | financial | 2 | Annual; file and pay within 9 months of tax-period end (FY to 31 Dec 2025 → 30 Sep 2026); 0% to AED 375k, 9% above [A] | FTA EmaraTax https://tax.gov.ae/ | Tax agent / owner | Nil; late filing AED 500/month for 12 months then 1,000/month [T] | Financial statements — audited if revenue > AED 50m, Qualifying Free Zone Person, or tax group (MD 84/2025) [A]; TP disclosure; small-business-relief election |
| `audited_financial_statements` | Audited financial statements · البيانات المالية المدققة | financial | 2 | Annual; DMCC/JAFZA/DWC filing within 90 days of FY end (advisers also cite 180) [T, conflicting]; condition of free-zone licence renewal | Auditor; zone portal upload | Auditor signs, owner approves | Audit fee market [U]; zone late penalty cited AED 5,000/month [T] | Trial balance, `bank_statement` set, ledgers, prior FS, resolution approving FS |
| `bank_receiving_details` | Receiving bank details (name, IBAN, SWIFT) · بيانات حساب التحصيل | financial | 2 field-level secret via `VaultStore.put_secret`; §10 exception | No expiry; changed only by owner CLI + callback rule | Bank | Owner enters once (§17) | — | Not a scanned document; placeholder `{{bank.<coat>.iban}}`; never OCR'd into metadata beyond last4 |
| `bank_statement` | Company bank statement · كشف حساب الشركة | financial | 2 (§6) | Monthly | Read-only bank feed | n/a | — | Reconciliation input; auditor/FTA requests go through share rules |
| `bank_facility_agreement` | Bank KYC letter / facility / account mandate · اتفاقية تسهيلات / تفويض الحساب | financial | 2 | Facility term; bank KYC refresh typically yearly on licence renewal [U] | Bank RM | Owner signs | — | Renewed licence, MOA, UBO register, signatory passports → KYC pack (recipient: bank RM) |
| `issued_invoice` | Issued invoice / quote · فاتورة صادرة / عرض سعر | financial | 0 (goes to customers freely; IBAN filled by renderer at generation, §10) | n/a | `DocumentRenderer` | Nour (A/N per category) | — | Not a renewal; TRN + IBAN placeholders; ledger entry |
| `supplier_invoice` | Supplier invoice / PO · فاتورة مورد / أمر شراء | financial | 0 (business counterpart data) | n/a | — | Payment = maker-checker (§10) | — | Bank-detail changes on an invoice → `BeneficiaryService.propose_change` + callback, never auto-update |
| `customer_contract` | Customer / service agreement · عقد عميل | contract | 2 (§6 contracts) | Term as drafted; auto-renewal clauses → expiry = notice date | Company | Owner for anything outside mandate (K) | — | Notice period, renewal price terms, counterparty contact |
| `supplier_contract` | Supplier / subscription contract · عقد مورد | contract | 2 | Term; recurring subscriptions always K (§10) | Company | Owner | — | Cancel/renew decision before the auto-renewal date |
| `nda` | NDA · اتفاقية عدم إفصاح | contract | 2 | Term (commonly 2–5 y) | Company | Nour drafts `nda_standard` within mandate; signature per mandate | — | Expiry = confidentiality term end |
| `group_medical_insurance` | Group medical insurance · التأمين الصحي الجماعي | insurance | 2 (insurance + employee health data) | Annual; mandatory for every Dubai visa holder, checked at visa renewal [T] | DHA / insurer | Owner / HR | Market [U] | Census (names, EIDs, DOB — Tier 2), prior policy, claims report; align to visa cycle |
| `professional_indemnity_insurance` | Professional indemnity / liability · تأمين المسؤولية المهنية | insurance | 2 | Annual | Insurer / broker | Owner | Market [U] | Prior policy, revenue declaration, claims history |
| `property_insurance` | Office / contents / workmen's comp · تأمين الممتلكات | insurance | 2 | Annual | Insurer | Owner | Market [U] | Asset schedule, Ejari |
| `employment_contract` | Employment contract (MOHRE / zone) · عقد عمل | hr | 2 | Fixed-term, aligned to the work permit (2 y) [T]; the owner's own contract files under `owner` | MOHRE / zone portal | PRO; employee signs | — | Renew with `work_permit` |
| `work_permit` | MOHRE work permit · تصريح عمل | hr | 2 (mainland; zones issue their own) | 2 y [T]; renew within 60 days of expiry [T] | MOHRE https://www.mohre.gov.ae/en/our-services/renewal-of-permit-and-work-contract-work-permit.aspx (UAE Pass-only) | PRO | AED 250 / 1,200 / 3,450 by establishment category 1/2/3 + AED 50 [A] | Signed contract, employee passport + EID + photo, `establishment_card_labour` valid, WPS compliance |
| `employee_identity_copy` | Employee passport / visa / EID copies · نسخ هوية الموظفين | identity | 2 (third-party personal data; minimum retention) | Follows the employee visa (2 y) | — | — | — | Employee visa reminders go to the PRO task list, not the owner brief, unless the owner is the sponsor |
| `noc_letter` | NOC / salary certificate · شهادة عدم ممانعة / شهادة راتب | hr | 2 (names + salary) | Per request; accepted ~30–90 days [U] | Company letterhead | Nour drafts; signature in owner's name = K | — | Not a renewal |
| `payroll_wps_file` | Payroll / WPS SIF file · ملف الرواتب (حماية الأجور) | hr | 2 | Monthly; WPS window after pay date [U] | MOHRE WPS via bank / exchange | Owner releases (maker-checker) | — | Salary ledger, release reference |
| `litigation_file` | Court / arbitration file · ملف قضية | legal | 2 | Hearing dates → calendar | Dubai Courts / DIFC Courts | Counsel | — | Hearing dates, filing deadlines, counsel contact |
| `legal_opinion` | Legal opinion / notarised document · رأي قانوني / مستند موثق | legal | 2 | None | Law firm / notary | — | — | — |
| `civil_defence_certificate` | Civil Defence fire-safety certificate · شهادة الدفاع المدني | other | 0 (public compliance certificate) | Annual [T]; AMC with a DCD-approved contractor | Dubai Civil Defence https://www.dcd.gov.ae/ ; DM fee sheet (§6) | Building management / PRO | [U] | AMC contract, prior certificate |
| `municipality_permit` | Dubai Municipality / activity permit · تصريح البلدية | other | 0 | Annual, activity-dependent [U] | Dubai Municipality https://www.dm.gov.ae/ | PRO | [U] | Prior permit, licence |
| `domain_registration` | Domain registration · تسجيل النطاق | other | 0 (public WHOIS; registrar login is Tier 3) | Annual; .ae auto-renews 1 y at the registrar [T] | aeDA-accredited registrar (TDRA registry) | Nour pays within spend tiers (first recurring = K) | .ae ≈ AED 125–145/yr [T] | Registrar, auto-renew flag, DNS/MX dependency — losing it kills the coat's email identity, treat as critical |
| `trademark_certificate` | Trademark certificate · شهادة العلامة التجارية | other | 0 (public register) | 10 y; renew in the final year [T] | MoE trademark services https://www.moec.gov.ae/en/web/guest/trademark-services | Owner (UAE Pass) or agent | Registration 5,000 + publication 750 + examination 750 [A]; renewal ~6,700 [T] | Class list, logo file, prior certificate, agent POA |
| `software_subscription` | SaaS / ad-account subscription · اشتراك برمجي | other | 0 | Monthly / annual auto-renew | Vendor | Nour within spend tiers; new recurring = K | — | Cancel-before date, seat count |
| `other` | Uncategorised · أخرى | other | 0 default; intake must ask when any Tier 2 signal is present (ID number, IBAN, TRN, health terms) | — | — | — | — | — |

## 2. TierRegister rules (transcribe into `config/vault_tiers.yaml` and `nour/vault/store.py:TierRegister`)

```yaml
# config/vault_tiers.yaml — proposed file; PermissionsConfig gains `vault_tiers: VaultTiersConfig` (DESIGN §3.6)
# and TierRegister(cfg) reads cfg.vault_tiers. Field names are the yaml keys. Tiers are DataTier ints 0..3.
version: 1

entity_classes:                      # entity_class(entity_ref): first match wins
  - {name: owner,             when: "entity_ref == 'owner'"}
  - {name: company_free_zone, when: "entity_ref startswith 'coat:' and coats[slug].jurisdiction startswith 'free_zone'"}
  - {name: company_mainland,  when: "entity_ref startswith 'coat:'"}        # default when coat.jurisdiction is absent
# coat.jurisdiction is a proposed CoatConfig key: "mainland" | "free_zone:<authority>" (e.g. free_zone:dmcc)

class_defaults:                      # SPEC §11 classes; used when a bare class name arrives as `type` (fixtures send "insurance")
  {identity: 2, corporate: 2, financial: 2, contract: 2, insurance: 2, vehicle: 2, property: 2, hr: 2, medical: 2, legal: 2, other: 0}

types:                               # class → {document.type: tier}; class_of(type) is the inverse map (ids are unique across classes)
  identity:  {passport: 2, emirates_id: 2, residence_visa: 2, golden_visa: 2, driving_licence: 2, family_civil_certificate: 2, attested_degree_certificate: 2, employee_identity_copy: 2}
  corporate: {trade_licence: 2, establishment_card_immigration: 2, establishment_card_labour: 2, certificate_of_incorporation: 2, memorandum_of_association: 2, shareholder_register: 2, ubo_register: 2, board_resolution: 2, ai_representation_mandate: 0, chamber_membership_certificate: 0}
  financial: {personal_bank_statement: 2, bank_statement: 2, bank_receiving_details: 2, bank_facility_agreement: 2, tax_residency_certificate: 2, vat_registration_certificate: 0, vat_return: 2, corporate_tax_registration_certificate: 0, corporate_tax_return: 2, audited_financial_statements: 2, issued_invoice: 0, supplier_invoice: 0}
  contract:  {personal_tenancy_contract: 2, office_tenancy_contract: 2, free_zone_lease: 2, customer_contract: 2, supplier_contract: 2, nda: 2}
  insurance: {motor_insurance: 2, health_insurance: 2, group_medical_insurance: 2, professional_indemnity_insurance: 2, property_insurance: 2}
  vehicle:   {vehicle_registration: 2}
  property:  {title_deed: 2}
  hr:        {employment_contract: 2, work_permit: 2, noc_letter: 2, payroll_wps_file: 2}
  medical:   {medical_record: 2}
  legal:     {police_clearance: 2, will: 2, power_of_attorney: 2, litigation_file: 2, legal_opinion: 2}
  other:     {household_bill: 1, travel_document: 1, civil_defence_certificate: 0, municipality_permit: 0, domain_registration: 0, trademark_certificate: 0, software_subscription: 0, other: 0}

overrides:                           # entity_class → {type: tier}; applied before `types`; an override may only RAISE the tier
  owner:                             # (lowering is a reviewed config change, never a runtime decision)
    issued_invoice: 2                # paper addressed to the owner personally is private, not company paper
    supplier_invoice: 1
    software_subscription: 1
    domain_registration: 1
    employment_contract: 2           # the owner's own contract with one of his companies files under `owner`
  company_mainland: {}
  company_free_zone: {}
  # entity-only types: free_zone_lease, office_tenancy_contract, establishment_card_labour → intake rejects a mismatched entity class

never_held:                          # Tier 3 (§6): intake refuses, LeakGuard registers the fingerprint, nothing is filed
  types: [uae_pass, credential]
  patterns: [password, card_number, cvv, otp, bank_login, biometric_template, uae_pass_pin]

exceptions:
  receiving_bank_details_on_invoices:          # SPEC §10 — the one Tier 2 value that leaves without per-send approval
    applies_to_types: [bank_receiving_details]
    only_when: {templates: [invoice, quote], placeholder: "{{bank.<coat>.iban}}", filled_by: DocumentRenderer}
    approval: none                             # no passphrase, no tier-K queue
    model_sees: placeholder_only               # full value exists only in code; briefs/logs show last4; audit stores a keyed hash
    still_required: [share_log_entry, audit_row_data_tier_2]
    never_via: [chat, prompt, memory, text_typed_by_model]

resolution:                          # TierRegister.tier_of(document_type, entity_ref) — in this order
  - "document_type in never_held.types → raise Refusal (tier 3 is never filed)"
  - "t = overrides[entity_class(entity_ref)].get(document_type)"
  - "if t is None: t = types[*].get(document_type)"
  - "if t is None and document_type in class_defaults: t = class_defaults[document_type]      # bare class name"
  - "if t is None: t = 2 and flag unknown_type → intake asks the owner; an unknown scan is never defaulted to 0"
  - "intake may raise the proposed tier at owner confirmation (VaultStore.file takes the confirmed tier); it may never go below this result"
may:                                 # TierRegister.may(desk, tier, op) — straight from permissions.yaml data_tiers (§6)
  operator:  {0: [know, use, share], 1: [], 2: [], 3: []}
  assistant: {0: [know, use, share], 1: [know, use, share_to_approved], 2: [retrieve_on_owner_request, share_with_passphrase], 3: []}
```

## 3. `allowed_recipients` defaults and the minimum-necessary form

- Default is `()` for every type (SPEC §11, §15). Only the owner adds entries, per coat, as `recipient + type + form` triples — the "approved-recipient lists per company and for the owner's personal matters" of §17. Nour never proposes an addition after a single successful share.
- Meaning per tier (decision to confirm, §5): Tier 0/1 — a listed recipient makes the send autonomous (A) in that category. Tier 2 — a listed recipient skips the "verify the requester with the owner first" step and the recipient-identity check, but the typed passphrase on the command is still required every time (`VaultStore.share` demands `released.passphrase_verified`; the only exception is `bank_receiving_details` on invoices).
- Any request from someone claiming to be family, a partner, a lawyer or a bank is verified on the owner's thread before anything moves, listed or not (§11).

| Form id | What leaves | Use when |
|---|---|---|
| `number_only` | The identifier from `metadata` in message text (licence no., TRN, Emirates ID no., policy no., plate, Ejari no.); no file | Default for every Tier 2 request; satisfies most portals, insurers and typing centres |
| `page_only` | One page re-rendered to a fresh PDF (licence front, passport bio page, policy schedule) | When the recipient needs to see the document, not the file |
| `redacted_copy` | Page with unrelated fields masked (other visas, transaction lines, salaries of others, dependants) | Bank statements to landlords, passports to hotels/airlines, census to brokers |
| `watermarked_link` | Full copy watermarked "for <recipient> · <date> · <purpose>", recipient-bound expiring link (default 7 days, single download) | When a regulator or bank demands the full document; always logged in `share_log` |
| `original_file` | Unwatermarked original | Never from Nour; the owner sends it from his own device if a portal rejects watermarks |

| Recipient role | Types commonly requested | Default form | Notes |
|---|---|---|---|
| Bank relationship manager / KYC | `trade_licence`, `memorandum_of_association`, `ubo_register`, `shareholder_register`, `certificate_of_incorporation`, `passport` + `emirates_id` + `residence_visa` of signatories, `audited_financial_statements`, `office_tenancy_contract` | `watermarked_link` | Annual KYC refresh; the bank is itself verified by callback to the number on file |
| Auditor / tax agent | `bank_statement`, `vat_return`, `corporate_tax_return`, `audited_financial_statements` (prior), `supplier_invoice`, `issued_invoice`, `payroll_wps_file`, `trade_licence` | `watermarked_link` (statements), `number_only` (TRN) | Engagement letter on file before the first share |
| Company PRO / typing centre | `trade_licence`, both establishment cards, `employee_identity_copy`, `employment_contract`, `work_permit`, `group_medical_insurance`, `emirates_id` | `page_only` | PRO performs the human submission of §14; share only what the step list names |
| Landlord / agent (Ejari) | `trade_licence`, `emirates_id`, `passport` bio page, `bank_statement` (residential screening) | `redacted_copy` | Rent cheques are owner-signed, never Nour |
| Insurer / broker | `vehicle_registration`, `driving_licence`, prior `motor_insurance`, `emirates_id`, employee census | `number_only` or `redacted_copy` | Quotes need numbers, not scans |
| Customers | `trade_licence` number, `vat_registration_certificate` TRN, `bank_receiving_details` (via renderer only) | `number_only` | §10: receiving details on invoices need no approval |
| Garage / repairer | `motor_insurance` policy number and `vehicle_registration` plate | `number_only` | Fixture cmd_027: policy number or one page, never the file |
| Government portals (DET, GDRFA, ICP, MOHRE, FTA, DLD, RTA) | Everything in the renewal pack | — | Human-only (§14): Nour hands the pack to the owner/PRO, it does not upload |

## 4. Intake extraction hints (propose entity, type, tier, expiry; file only after the owner confirms the tier)

Entity proposal, all classes: match company name, licence number, TRN or chamber number against the coat registry → `coat:<slug>`; match the owner's name or Emirates ID number against the owner profile → `owner`; a family name with a different given name → `owner` + `subject`; an employee name on company paper → the coat + `subject`. No match → ask, never guess. Dates: UAE paper is DD/MM/YYYY; Dubai Courts and some ICP output also print Hijri dates — take the Gregorian one. Many documents are bilingual; OCR both and prefer the Arabic for names on government paper. Tier 2 OCR runs on the private model in region (§11, §14); extracted values are kept only as the metadata fields listed here, never as text.

| Class | OCR anchors (type signal) | Fields to extract → `metadata` | Expiry source | Tier signal |
|---|---|---|---|---|
| identity | MRZ (2–3 lines of `<`) → `passport`; `784-YYYY-NNNNNNN-N` (15 digits) → `emirates_id`; "Residence" + "UID"/"File No." → `residence_visa`; "Golden" → `golden_visa`; RTA logo + "Licence No." → `driving_licence` | nationality, issuing authority, document number masked to last4, subject name, sponsor name | "Date of Expiry" / "تاريخ الانتهاء"; visas: "Expiry Date" of the permit, not the entry stamp | Always 2; a second person's name → `subject` |
| corporate | DET or zone logo + "Licence No." / "رقم الرخصة" → `trade_licence`; "Establishment Card" + GDRFA/ICP → `establishment_card_immigration`; MOHRE logo → `establishment_card_labour`; "Memorandum" / "عقد تأسيس" → `memorandum_of_association`; "Beneficial Owner" → `ubo_register`; Dubai Chambers logo + "Membership No." → `chamber_membership_certificate` | licence number, legal form, activities, jurisdiction (mainland / zone name → sets `coat.jurisdiction` hint), card number, membership number | "Expiry Date" / "Valid until"; MOA and incorporation: none | 2 except `ai_representation_mandate`, `chamber_membership_certificate` |
| financial | 15-digit TRN starting `100` → VAT/CT certificates (title says which); "VAT Return" / "Form 201" period → `vat_return`; FTA logo + "Corporate Tax Return" → `corporate_tax_return`; "Independent Auditor's Report" → `audited_financial_statements`; bank logo + "Statement" + account → `bank_statement`; "Tax Invoice" + the coat's own TRN as issuer → `issued_invoice`, as recipient → `supplier_invoice`; FTA "Tax Residency Certificate" → `tax_residency_certificate` | TRN (Tier 0), statement period, invoice number, counterpart name, amount; IBAN → fingerprint + last4 only, never the value | Returns: period end + 28 days (VAT) / + 9 months (CT); TRC: period end; statements: none | Any IBAN, balance or salary → 2; invoices → 0 unless addressed to the owner personally |
| contract | "Ejari" + contract number → tenancy (`office_tenancy_contract` if the tenant is a coat, else `personal_tenancy_contract`); zone lease wording → `free_zone_lease`; "Non-Disclosure" → `nda`; parties: coat as supplier → `customer_contract`, coat as buyer → `supplier_contract` | parties, Ejari number, annual rent, notice period, auto-renewal clause | "End Date"; auto-renewal → expiry = end date minus notice period | 2 |
| insurance | "Policy No." + "Period of Insurance"; plate/chassis present → `motor_insurance`; DHA/member list → `health_insurance` (owner) or `group_medical_insurance` (coat) | insurer, policy number, plate, member count (not names) | "Period of Insurance" end (13-month motor policies: use the stated end, not +12 months) | 2 |
| vehicle | RTA "Registration Card" / "ملكية"; plate (emirate code + letter + up to 5 digits), 17-char chassis/VIN | plate, make/model, chassis masked to last4 | "Reg. Exp." / "Exp. Date" | 2; plate alone may appear in Tier 1 reminders |
| property | DLD "Title Deed" + plot/unit number | unit, community, DLD certificate number | none | 2 |
| hr | MOHRE contract number, "Work Permit No.", "Salary"; letterhead + "To whom it may concern" → `noc_letter`; `.SIF` / "WPS" → `payroll_wps_file` | employee name → `subject`, permit number, contract type | permit/contract "Expiry Date" | 2 |
| medical | DHA facility stamp, "Patient", MRN, lab headers | facility, date only | none | 2; never OCR diagnoses into metadata |
| legal | Dubai Courts / DIFC Courts case number; notary serial + "Power of Attorney" / "وكالة"; "Will" + DIFC Wills; Dubai Police "Good Conduct" | case number, notary number, attorney name | POA: stated expiry (none → no expiry); police clearance: issue date + 90 days (flag [U]) | 2 |
| other | aeDA/registrar "Expiry" + domain name → `domain_registration`; MoE "Trademark" + class numbers → `trademark_certificate`; DCD/DM logos → compliance certificates; DEWA/du/school logos → `household_bill` | domain, trademark number and classes, certificate number, vendor | stated expiry; trademark registration date + 10 y | 0 or 1; any ID number, IBAN, TRN or health term present → propose 2 and ask |

## 5. Unverified or open items

1. **UAE Pass delegation**: no official text was found stating the account is non-transferable; SPEC §14 is the governing rule regardless (human-only, never held). Marked [A] only for what UAE Pass is.
2. **Trade licence fees** (mainland AED 8,000–15,000+, free-zone packages), **GDRFA establishment card fee and fine** (AED 300–500; fine AED 500/month vs AED 100/month in different sources), **MOHRE labour establishment card** term and fee, **free-zone establishment card** (AED 1,300 breakdown): third-party or conflicting. Confirm with the PRO per company.
3. **Free-zone audited FS deadline**: 90 days per the sources' reading of regulations vs 180 days per advisers; varies by zone. Set per coat from the zone's portal.
4. **Passport term and consulate fees**: depend on the owner's and dependants' nationalities — not looked up.
5. **Fees marked [U]**: Golden visa, MOFA attestation, police clearance, Civil Defence certificate, Dubai Municipality permits, insurance premiums, VAT late-filing and corporate-tax late-registration penalties (AED 10,000 commonly cited), Dubai basic health plan premium (2014 figure).
6. **Procedural windows marked [U]**: VAT registration amendment (20 business days), WPS filing window, NOC acceptance periods, police-clearance acceptance, POA validity under Circular 29/R/2025, annual UBO confirmation per zone.
7. **Design decisions this file assumes** (need owner/architect sign-off): (a) `document.type` = catalogue id, with the §11 class derived, rather than `type` = class; (b) proposed `coat.jurisdiction` key in `CoatConfig`; (c) proposed `vault_tiers` block in `PermissionsConfig` (DESIGN §3.6 lists none); (d) the Tier 2 meaning of `allowed_recipients` in §3 (passphrase still required) — SPEC §11 and §15 read slightly differently; (e) overrides may only raise tiers.
8. **Driving-licence terms** (5 y residents / 10 y nationals) and **RTA fees** are from press and RTA process pages, not an RTA fee table.

## 6. Sources (accessed 2026-10-02)

- Emirates ID: https://u.ae/en/information-and-services/visa-and-emirates-id/emirates-id ; ICP renewal https://icp.gov.ae/en/services-details/?serviceid=64afe3c1035448005bd52e5d ; ICP residency renewal https://icp.gov.ae/en/services-details/?serviceid=64afe3c1035448005bd52e66
- Residence visa (dependants, Dubai): https://www.gdrfad.gov.ae/en/node/2105 ; employment renewal costs [T] https://virtuzone.com/blog/uae-residence-visa-renewal/ ; Golden visa https://u.ae/en/information-and-services/visa-and-emirates-id/residence-visas/golden-visa
- UAE Pass: https://u.ae/en/about-the-uae/digital-uae/digital-transformation/platforms-and-apps/the-uae-pass-app ; Dubai mandate https://mediaoffice.ae/news/2020/May/16-05/smart-dubai ; MOHRE UAE Pass-only https://nukta.com/uae-pass-will-soon-be-required-to-use-mohre-services
- Trade licence [T]: https://safeledger.ae/blog/dubai-trade-license-renewal-fees ; https://www.shuraa.com/blog/2021/03/trade-license-renewal-in-dubai.html ; Invest in Dubai https://invest.dubai.ae/
- Establishment cards [T]: https://tlz.ae/how-to-renew-an-establishment-card-in-dubai/ ; https://uaefreezonefinder.com/establishment-card-renewal-uae-2026/ ; https://www.rizmona.com/news/failure-to-renew-this-uae-document-will-result-in-a-monthly-fine-of-dh100
- Work permits: https://u.ae/en/information-and-services/jobs/Sector-of-employment/employment-in-the-private-sector/work-permits ; https://www.mohre.gov.ae/en/our-services/renewal-of-permit-and-work-contract-work-permit.aspx ; labour card term [T] https://virtuzone.com/blog/labour-card-uae/
- VAT: https://u.ae/en/information-and-services/finance-and-investment/taxation/vat/filing-a-tax-return-for-vat ; Corporate tax: https://u.ae/en/information-and-services/finance-and-investment/taxation/corporate-tax ; https://tax.gov.ae/en/media.centre/news/federal.tax.authority.urges.submission.of.corporate.tax.returns.and.settlement.of.corporate.tax.liabilities.within.nine.months.from.the.end.of.the.tax.period.aspx ; penalties [T] https://www.khaleejtimes.com/business/fta-urges-corporate-taxpayers-to-file-returns-settle-dues-before-deadline
- Audited FS: https://mof.gov.ae/wp-content/uploads/2025/04/Ministerial-Decision-No.-84-of-2025-on-Audited-Financial-Statements.pdf ; free-zone deadlines [T] https://www.sovereigngroup.com/news/audit-requirements-for-free-zone-companies-based-in-dmcc-dwc-and-jafza/
- TRC: https://tax.gov.ae/en/services/issuance.of.tax.certificates.aspx
- Ejari: https://dubailand.gov.ae/en/eservices/register-renew-ejari-contract/
- RTA vehicle renewal: https://www.rta.ae/wpsv5/links/vehicle-renewal/en/vehicle-renewal.html ; fees [T] https://shory.com/blog/car-insurance/vehicle-registration-renewal-in-dubai-costs-documents-and-deadlines ; 13-month motor policies [T] https://www.shory.com/blog/car-insurance/what-is-the-validity-period-of-car-insurance-in-the-uae ; driving licence [T] https://gulfbusiness.com/dubai-confirms-implementation-of-new-rules-for-driving-licences-from-july-1/
- Dubai Chambers: https://www.dubaichambers.com/en/membership-renewal
- Trademarks: https://www.moec.gov.ae/en/w/register-trademark ; https://www.moec.gov.ae/en/web/guest/trademark-services ; renewal fee [T] https://igerent.com/trademark-renewal-uae
- .ae domains: https://en.wikipedia.org/wiki/.ae ; registrar pricing [T] https://aeserver.com/
- Health insurance–visa link [T]: https://www.tradearabia.com/news/HEAL_285325.html ; https://khaleejtimes.com/government/no-health-insurance-no-visa-says-dha
- UBO: Cabinet Decision 109/2023 https://uaelegislation.gov.ae/en/legislations/2314 ; penalties CD 132/2023 https://dubaihumanitarian.ae/wp-content/uploads/2025/09/Cabinet-Decision-No-132-of-2023-English.pdf ; ESR discontinued https://www.clydeco.com/en/insights/2024/10/uae-economic-substance-regulations
- Civil Defence fee sheet: https://www.dm.gov.ae/wp-content/uploads/2021/04/DM-DCLD-RD-IC-0025-Fee-Structure-for-Fire-Safety-Certificate-R1.pdf
- Wills [T]: https://gulfnews.com/living-in-uae/ask-us/how-to-register-a-will-in-the-uae--these-are-all-your-options-1.1624459177057 ; DIFC Courts https://www.difccourts.ae/
- POA: notary fees https://dlp.dubai.gov.ae/Legislation%20Reference/2014/Executive%20Council%20Resolution%20No.%20(4)%20of%202014%20Approving%20the%20Fees%20and%20Fines%20Related%20to%20Notaries%20Public.html ; validity [T] https://virtuzone.com/blog/poa/
