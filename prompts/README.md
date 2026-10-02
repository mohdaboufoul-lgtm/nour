# prompts/

Prompt files for Nour (SPEC §18). Every value is owner-editable; the system prompt is assembled
from these and the config files at session start. Placeholders use Jinja2 `{{ ... }}` and are
rendered by the assembler, never by the model.

| File | Used by | What it is |
|---|---|---|
| nour.system.md | Both desks | System-prompt skeleton: authority, hard rules, permissions, persona, language, output contract, memory. Rendered once per desk and coat. |
| critic.system.md | Self-critic pass (SPEC §8) | Scores every outbound draft for tone, claims, compliance and register; hard fails block the send. Primary model, separate context, per draft. |
| auditor.system.md | Auditor agent (SPEC §4, §12) | Read-only nightly review of the full audit log on a different vendor's model in its own container; reports to the owner only. Nour cannot see it. |
| face_lock.md | Image model (SPEC §3) | One-time portrait prompt; the owner approves one candidate and every later image derives from it. |

## How the system prompt is assembled (SPEC §18, §4, §5)

1. The event bus stamps the event with desk (operator or assistant) and coat slug; a task with no
   coat is refused.
2. The assembler renders `nour.system.md` with: `constitution.hard_rules` (the Hard rules section
   of config/constitution.md, verbatim); `permissions.data_tiers` and `permissions.ask_every_time`
   (config/permissions.yaml); `spend_tiers` (config/spend_tiers.yaml); `coat.*` (config/coats/
   <slug>.yaml: name, identity, approval_rules, mandate); `persona` (config/persona.md, verbatim);
   `today` in Asia/Dubai.
3. It appends two sections the skeleton does not list: `## Tone` from the coat's tone_guide_ref and
   `## Knowledge` from the coat's knowledge_pack_ref, so one coat gives one coherent pack.
4. The Operator desk's prompt is built only for the Buzz Avenue coat and only with Tier 0 facts;
   the Assistant desk's prompt may carry any coat. Two prompts, two credential sets, two memory
   namespaces; the only text they share is the constitution.
5. The rendered prompt is hashed and the hash goes into input_hash of every audit event in the
   session. A constitution change takes effect only at the next assembly.

The critic and auditor prompts are rendered the same way with their own inputs (draft, mandate,
knowledge pack; audit events, spend expectation), per call rather than per session.

## The one rule for every prompt

No Tier 2 content, no passwords, no card numbers, no tokens and no secrets ever enter any prompt,
in any field, on either desk (SPEC §2 hard rule 10, §6, §13). Prompts carry references and hashes
only. Bank details are written as the literal placeholder `{{bank.<coat>.iban}}` and filled by the
document renderer from the vault after the model has finished, in a dedicated substitution pass
(not Jinja2: the hyphen in a coat slug would parse as subtraction). Licence numbers, addresses and
contact lines come from the coat record. If the assembler finds a secret-shaped value (IBAN,
Emirates ID, card, passport, key) in a rendered prompt, it refuses to start the session and raises
an incident. The same rule applies to test fixtures under tests/.
