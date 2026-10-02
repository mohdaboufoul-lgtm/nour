<!--
  templates/letterhead.buzz-avenue.md  (SPEC §3, §10, §11; coat identity.letterhead_ref)
  Source for templates/letterhead.buzz-avenue.pdf: the renderer fills the placeholders, inserts
  the body and exports the PDF. Used for quote, invoice, proposal and nda_standard, the only
  templates the Buzz Avenue mandate allows. The body between header and footer is HTML-ready
  Markdown with RTL blocks for Arabic.

  Placeholders resolved by the renderer, never by the model:
    {{ coat.name }}  {{ coat.legal_entity }}  {{ coat.licence_number }}  {{ coat.address }}
    {{ coat.identity.email }}  {{ coat.identity.whatsapp_line }}  {{ coat.identity.verification_page }}
    {{ doc.type }}  {{ doc.number }}  {{ doc.date }}  {{ doc.version }}  {{ doc.hash_short }}  {{ doc.body }}
    {{ recipient.name }}  {{ recipient.org }}  {{ approval.id }}
  Vault placeholders (SPEC §10): {{bank.buzz-avenue.iban}} and its siblings are written exactly
  as shown, with no spaces, and filled from the vault at generation time by a dedicated
  substitution pass (not by Jinja2, whose parser would read the hyphen as subtraction). The
  model never sees or types the value; briefs and logs show the last four characters; the audit
  log stores a hash. The licence number is Tier 0 metadata printed on every UAE invoice; the
  licence scan itself stays Tier 2 in the vault.
-->

<header style="display:flex;justify-content:space-between;border-bottom:3px solid #0f6b6b;padding-bottom:8px;font-family:Arial,Helvetica,sans-serif;font-size:12px;">
  <div dir="ltr">
    <strong style="font-size:18px;color:#0f6b6b;">{{ coat.name }}</strong><br>
    {{ coat.legal_entity }}<br>
    Trade licence no. {{ coat.licence_number }}<br>
    {{ coat.address }}<br>
    {{ coat.identity.email }} &middot; WhatsApp {{ coat.identity.whatsapp_line }}
  </div>
  <div dir="rtl" lang="ar" style="text-align:right;">
    <strong style="font-size:18px;color:#0f6b6b;">{{ coat.name }}</strong><br>
    {{ coat.legal_entity }}<br>
    رخصة تجارية رقم <span dir="ltr">{{ coat.licence_number }}</span><br>
    {{ coat.address }}<br>
    <span dir="ltr">{{ coat.identity.email }}</span> &middot; واتساب <span dir="ltr">{{ coat.identity.whatsapp_line }}</span>
  </div>
</header>

| | |
|---|---|
| **Document / المستند** | {{ doc.type }} {{ doc.number }} (v{{ doc.version }}) |
| **Date / التاريخ** | {{ doc.date }} (Asia/Dubai) |
| **To / إلى** | {{ recipient.name }}, {{ recipient.org }} |
| **Prepared by / أعدّته** | Nour, {{ coat.name }} AI assistant · نور، المساعدة الذكية في {{ coat.name }} |

<!-- BODY: the renderer inserts the quote / invoice / proposal / nda_standard body here. -->

{{ doc.body }}

<!-- PAYMENT BLOCK: invoices only; omitted for quote, proposal and nda_standard. Every value is a
     vault placeholder in the source. An IBAN only lets people pay the company, so this block needs
     no per-send approval (SPEC §6 exception, §10). -->

## Payment details / بيانات الدفع

| | |
|---|---|
| Bank / البنك | {{bank.buzz-avenue.bank_name}} |
| Account name / اسم الحساب | {{bank.buzz-avenue.account_name}} |
| IBAN / الآيبان | {{bank.buzz-avenue.iban}} |
| SWIFT / سويفت | {{bank.buzz-avenue.swift}} |
| Payment reference / مرجع الدفع | {{ doc.number }} |

{{ coat.name }} changes its receiving account only by a signed letter on this letterhead, confirmed
by a call on the official line. Pay only to the details printed here, never to details sent in a
chat or an email.

<div dir="rtl" lang="ar">لا تغيّر {{ coat.name }} حسابها البنكي إلا بخطاب موقّع على هذه الترويسة ومؤكَّد باتصال عبر الخط الرسمي. ادفعوا فقط إلى البيانات المطبوعة هنا، وليس إلى بيانات مرسلة عبر محادثة أو بريد إلكتروني.</div>

<!-- ISSUE LINE: Nour signs nothing legally binding (SPEC §1). Quotes, invoices and proposals are
     issued by the company; a contract carries the owner's signature block, added by the owner. -->

Issued by {{ coat.name }} · prepared by Nour, {{ coat.name }} AI assistant · approval ref {{ approval.id }}

<div dir="rtl" lang="ar">صادر عن {{ coat.name }} · أعدّته نور، المساعدة الذكية في {{ coat.name }} · مرجع الموافقة <span dir="ltr">{{ approval.id }}</span></div>

<footer style="border-top:1px solid #d9e0e3;margin-top:16px;padding-top:6px;font-size:10px;color:#8a949b;font-family:Arial,Helvetica,sans-serif;">
  <div dir="ltr">Verify this document and our official contacts: {{ coat.identity.verification_page }} &middot; Copy for {{ recipient.name }}, {{ doc.date }} &middot; {{ doc.type }} {{ doc.number }} v{{ doc.version }} &middot; hash {{ doc.hash_short }}</div>
  <div dir="rtl" lang="ar" style="text-align:right;">للتحقق من هذا المستند ومن بيانات التواصل الرسمية: <span dir="ltr">{{ coat.identity.verification_page }}</span> &middot; نسخة لـ {{ recipient.name }}، {{ doc.date }}</div>
</footer>

<!--
  Versioning and sharing (SPEC §11): originals are never modified. A correction or re-issue is a
  new version with a new {{ doc.version }} and a new hash; the previous version stays in the vault
  with its share_log. Every outgoing copy carries the recipient-and-date watermark in the footer,
  is sent by expiring link where possible, and is logged with recipient, purpose and approval
  reference. Sending any rendered document is tier K until that category is promoted (SPEC §6).
-->
