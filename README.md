# Claims intake agent

> **Status:** reference implementation built on mock systems and synthetic claims. The tests run offline with
> scripted model answers. See [Before using it for real](#before-using-it-for-real).

This LangGraph workflow reads a claim email and its attachments (photos, invoices, police reports), extracts the
details into a claims system and checks them against the policy. It then either sends the claim to fast-track or
pauses it for a person. Claude reads the documents and judges policy wording. Plain code decides the route, so
nothing written in a document can approve a claim.

```mermaid
flowchart TD
    start(["Claim email (.eml)"]) --> intake["<b>intake</b><br/>Registers the claim, flags attachments<br/>already seen on another claim"]
    intake -. "one per document, in parallel" .-> extract_document["<b>extract_document</b><br/>Reads the document, with a confidence score per field.<br/>Sonnet first, Opus re-reads if unsure"]
    extract_document --> consolidate["<b>consolidate</b><br/>Merges the results, records every<br/>disagreement, picks the coverage model"]
    consolidate --> coverage_agent["<b>coverage_agent</b><br/>Finds the policy and judges exclusions"]
    coverage_agent -. "tool calls" .-> coverage_tools["<b>coverage_tools</b><br/>Read-only policy lookups"]
    coverage_tools -. "tool results, or redo on Opus if unsure" .-> coverage_agent
    coverage_agent -. "no tool call: one reminder" .-> coverage_agent
    coverage_tools -. "assessment submitted" .-> verify_coverage["<b>verify_coverage</b><br/>Checks identity, status, dates, coverage<br/>and exclusions against the policy record"]
    coverage_agent -. "call failed or turn limit" .-> verify_coverage
    verify_coverage --> route["<b>route</b><br/>Applies the rules, queues the first response"]
    route -. "no reason to involve a person" .-> fast_track(["Fast-track"])
    route -. "any reason" .-> human_review["<b>human_review</b><br/>Pauses until a reviewer decides,<br/>even across restarts"]
    human_review --> apply_review["<b>apply_review</b><br/>Records the decision"]
    apply_review --> reviewed(["Approved, denied or info requested"])

    classDef claude fill:#ede9fe,stroke:#7c3aed,color:#1f1147
    classDef code fill:#e0f2fe,stroke:#0284c7,color:#082f49
    classDef person fill:#fef3c7,stroke:#d97706,color:#451a03
    classDef terminal fill:#f1f5f9,stroke:#64748b,color:#0f172a
    class extract_document,coverage_agent claude
    class intake,consolidate,coverage_tools,verify_coverage,route,apply_review code
    class human_review person
    class start,fast_track,reviewed terminal
```

Purple steps call Claude, blue steps are plain code, and the amber step waits for a person. Dotted lines are
conditional. Every step writes to a tamper-evident audit log.

## Quick start

The tests need no API key:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

The eval runs 22 synthetic claims through real Claude, which costs real API usage:

```bash
export ANTHROPIC_API_KEY=your-key
claims-eval --limit 3    # start small and check the cost in the summary
claims-eval
```

## Benefits

Simple claims can move without waiting in a queue, and claims handlers can spend their time on the ones that need
judgment. Every claim gets a first response as soon as it's processed.

This repo ships only synthetic test data, so it can't show accuracy, fast-track rates or time saved on real claims. On
the 22 test claims, a claim took about 12 seconds and $0.035 in API usage. Real claims with longer documents will take longer and cost more. To measure your own results, use the
[eval](#checking-accuracy) with real, reviewed claims.

**For claimants**

- **A fast first response:** each claim gets an acknowledgement with its reference and next step, whichever route it takes.
- **Quicker handling of simple claims:** a clean claim doesn't wait for a person to read it first.
- **A person for anything uncertain:** injuries, other parties, exclusions and unclear documents all go to a claims handler.

**For claims handlers**

- **Less data entry:** details are extracted from emails, invoices and police reports into the claims system.
- **Reviews that start with the reasons:** each claim arrives with why it needs a person and the coverage findings.
- **Evidence you can check:** extracted values are recorded with the passage they came from and a confidence score.
- **Problems surfaced early:** disagreements between documents, reused attachments and text aimed at automated systems are all flagged.

**For compliance and risk**

- **A full record of every claim:** document fingerprints, model readings, policy lookups, rule outcomes and reviewer decisions are all logged.
- **Tampering is detectable:** `claims audit verify` shows whether the log has been edited or had records removed.
- **Decisions you can explain:** routing comes from plain rules, not a model, so each outcome traces to named reasons and a rules version.
- **Safe failure:** a failed or refused model call sends the claim to a person, never to fast-track.
- **Resistance to manipulation:** nothing written in a document can approve a claim, and read-only tools mean Claude can't change a policy.

**For operations**

- **Cost that follows complexity:** Sonnet handles most of the work, and Opus is used only where a claim calls for it.
- **Limits you control:** thresholds, a daily cap and a kill switch are all settings, with no code changes needed.
- **No lost work:** a crash resumes from the last finished step, and a claim waiting for review survives restarts.
- **Replaceable connections:** the claims system and policy store sit behind small interfaces, so real systems can replace the mocks.

## Using it

```bash
claims run evals/cases/FT-01-glass/email.eml     # process one or more emails
claims watch ./inbox                             # process .eml files as they appear
claims review list                               # claims waiting for a person, with reasons
claims review show CLM-XXXXXXXXXXXX
claims review decide CLM-XXXXXXXXXXXX approve --reviewer "J. Reviewer" --note "Confirmed by phone"
claims audit show CLM-XXXXXXXXXXXX               # everything that happened to a claim
claims audit verify                              # check the log hasn't been altered
```

Review decisions are `approve`, `deny` and `request_info`. Settings are in [.env.example](.env.example), and the fast-track rules are in [config/rules.yaml](config/rules.yaml). To stop all
new processing, create the file `data/CLAIMS_DISABLED`.

## When a claim goes to a person

A claim is fast-tracked only when none of these apply. The values are in [config/rules.yaml](config/rules.yaml), and the checks
are in [routing.py](src/claims/routing.py) and [coverage.py](src/claims/coverage.py). Some of these checks can be bypassed
today, as listed under [Known limits](#known-limits).

| Area | Sent to a person when |
|---|---|
| **Extraction** | A required field is missing, or any required field is below the confidence threshold (default 0.85) |
| **Documents** | A document is unreadable, unsupported or too large, documents disagree, or an attachment already appears on another claim |
| **Manipulation** | A document contains text aimed at an automated system, or Claude notes a specific concern |
| **Evidence** | There's no readable invoice, the invoice has no date or predates the loss, or a theft or vandalism claim has no police report document |
| **Identity** | The claimant isn't the policyholder, or the email didn't come from the address on the policy |
| **Coverage** | The policy is inactive, the date is outside its period, the loss type isn't covered, or an exclusion applies |
| **Amount** | The invoice or the claimed amount is over the fast-track limit (default $5,000), or the amount is over the coverage limit or not above the deductible |
| **Risk** | Injury or another party is involved, a collision claim doesn't rule both out, the report is late, the policy is new, or there are many recent claims |
| **Failure** | A Claude call fails or is refused, or the coverage check doesn't finish |

## Changing the rules

Limits, required fields and the evidence each loss type needs are in [config/rules.yaml](config/rules.yaml), so most rule
changes need no code. Each line of business can override the defaults:

```yaml
lines:
  auto: {}
  property:
    fast_track_limit: 2000
    loss_types:
      water_damage: {requires: [police_report]}
```

| Change | Where |
|---|---|
| Limits, time windows, the confidence threshold and the amount tolerance | `config/rules.yaml` |
| Required fields, and the evidence a loss type needs | `config/rules.yaml` |
| Different rules for a line of business | `config/rules.yaml` |
| Which policy statuses count as active, and the window for counting prior claims | `config/rules.yaml` |
| Whether an injury or another party always goes to a person | `config/rules.yaml` |
| The list of loss types, which also changes Claude's prompt, so run the eval | `config/rules.yaml` |
| When Opus steps in | `config/rules.yaml` |
| A new kind of check, such as comparing two dates | Code, in [routing.py](src/claims/routing.py) or [coverage.py](src/claims/coverage.py) |

The confidence threshold, amount tolerance, loss types and Opus settings apply to every line, because they're used before the
claim's policy is known.

To change a rule:

1. Edit the file and bump `version`.
2. Run `claims rules` to validate it.
3. Run the tests and the eval, and have the change reviewed before it goes live.

A mistyped setting name, a missing setting or many out-of-range values stop processing. This doesn't catch every mistake:
some valid-looking values can silently weaken a rule, as listed under [Known limits](#known-limits), so review every change to
the file. A policy on a named line the file doesn't list goes to a person. Every decision records the rules version and a
fingerprint of the file in the audit log, so you can tell which rules applied even if someone forgets to bump the version.

There's no approval step built in, so anyone who can edit the file can change how claims are decided. Keep it under change
control. Loosening a rule can fast-track claims that should reach a person.

### What stays in code, and why

**Safety rules** stay in code so that a one-line edit can't remove them:

- A failed or refused Claude call goes to a person.
- Injection flags and document concerns go to a person.
- A second reading can't clear a warning.
- The email body can't count as an invoice or police report.
- Claude's policy tools are read-only.
- A claimant who doesn't match the policyholder, or a sender who doesn't match the policy's email, goes to a person.

**Check logic** stays in code too, such as comparing dates or merging readings across documents, although the file can currently
switch off the date checks by removing `date_of_loss` from the required fields. Writing logic in YAML would
mean inventing a small programming language with no type checking or debugger. If your team needs to change logic often, a
dedicated rules engine would be the better step, and a trial on real claims would show whether that's worth building.

**Prompts** stay in code because a small wording change can shift how Claude reads documents, and that needs the live eval.

## Design decisions

- **Confidence isn't enough on its own.** A model's confidence score is its own estimate and isn't calibrated, so
  the threshold is one rule among many. The eval reports accuracy on high-confidence fields so you can see how far to
  trust it.
- **Documents are read one at a time.** Text in one document can't influence how another is read, and the code can
  compare independent readings.
- **Documents are untrusted.** Instructions inside them are flagged, not followed. A successful injection could still
  corrupt the values Claude reports, which is why identity, dates, limits and evidence are checked separately.
- **The email body can't serve as evidence.** A total stated there counts as the amount claimed, not as an invoice.
- **Failures go to a person.** There's no path where an error results in a fast-track.
- **The first response says nothing about the claim.** It gives only a reference and the next step, because the
  sender may not be who they claim to be.
- **Fast-track isn't a payment.** This workflow sets a status. Payment belongs to the downstream process.

## Which model handles what

Sonnet 5.5 handles most of the work. Opus 5.5 steps in for complex cases, chosen by code in
[escalation.py](src/claims/escalation.py):

| Step | Opus is used when |
|---|---|
| **Reading a document** | Sonnet's reading failed, it marked the document unreadable, or it was unsure of a field routing relies on. The document is then read again. |
| **Coverage check** | The claim is at or above `complex_amount` in the rules file (default $2,000, a placeholder), or one of its documents was re-read |
| **Coverage check, second try** | Sonnet's result was unsure and found no exclusion |

A claim already going to a person skips Opus, except for re-reading unclear documents. A second reading can't remove a
warning: injection flags, concerns and exclusions stay, and if the two readings disagree on a value, a person looks at it.

## Checking accuracy

`claims-eval` grades the route, the reason for it and key field values, then saves the results to `evals/results/`.
It exits with an error if any claim is wrongly fast-tracked.

| Metric | Meaning |
|---|---|
| `wrongly_fast_tracked` | Claims a person should have seen. This should be zero. |
| `fast_track_sent_to_person` | Simple claims that were over-escalated. This sets your fast-track rate. |
| `reason_correct` | Whether it escalated for the right reason, not by luck |
| `high_confidence_field_accuracy` | How far to trust high confidence scores |

To stress-test the rules, the `adversarial-case-writer` agent in Claude Code adds `ADV-` claims designed to get wrongly
fast-tracked. Each one is otherwise clean, so a fast-track result means the attack got through, and `claims-eval` reports
that as a failure. Check that each case is fair before trusting its result.

The 22 synthetic cases are a starting point and can't show real-world accuracy. They don't test real damage photos,
handwriting or unusual document layouts. Regenerate them with `python scripts/generate_cases.py`.

## Before using it for real

**Replace the mocks**
- `ClaimsSystem` and `PolicyStore` with clients for your systems, using a read-only account for policies.
- `claims watch` and the outbox folder with your mailbox and email sending.
- The SQLite checkpoints with a server database such as Postgres if you run more than one worker.
- The CLI review commands with your case-handling tool, using authenticated reviewer identities. The CLI accepts any
  reviewer name.

**Security and compliance**
- Check SPF, DKIM and DMARC results before trusting the sender. A `From` header alone can be forged.
- Have your data protection team approve what's sent to the API. Redaction covers email text only, so attachments
  are sent as received. Check your data retention terms with Anthropic.
- Encrypt `data/` and set retention rules, because it holds attachments, extracted details and checkpoints. Don't
  keep real claims data in a synced folder.
- Ship the audit log to write-once storage. Someone who can rewrite the whole file can rebuild the chain.
- Check your obligations for automated insurance decisions in each jurisdiction, including fairness testing and
  claimants' rights to human review. Have your compliance team sign off on the rules.

**Accuracy**
- Build an eval set from real, reviewed claims and measure against it before turning on fast-track.
- Run in shadow mode first, with every claim going to a person, and compare decisions.
- Sample fast-tracked claims for review on an ongoing basis.
- Set the limits in `config/rules.yaml` with your claims team. The current values are placeholders.

### Known limits

**General**
- A `request_info` decision ends the run, and the claimant's reply starts a new claim, so replies need linking to the original.
- Claims with several invoices aren't handled reliably. Differing totals count as a conflict and go to a person, but
  separate invoices with the same total can be counted as one and reach fast-track.
- Forged documents that look consistent won't be caught. Fraud screening needs its own tooling.

**Ways a claim can still be wrongly fast-tracked**

Safety reviews found these, and they're still open. They were reproduced with scripted model readings, not on live Claude.

- A pre-loss date on a second document that isn't labelled an invoice is dropped, so the invoice date check doesn't see it.
- A second reading can change a document's type without raising a warning, which can remove that document's date.
- A second invoice with no date, or a date Claude is unsure of, doesn't trigger the date checks.
- An invoice dated in the future isn't flagged.
- A claimed amount over the coverage limit can pass if it's within the 1% tolerance of the invoice.
- A policy with an empty `line` uses the default rules, even when every listed line is stricter.
- The coverage check sees extracted fields, not the documents, so an exclusion mentioned only in an attachment relies on Claude
  raising a concern.

**Ways an edit to the rules file can weaken a rule without an error**

These pass `claims rules`.

- Removing `date_of_loss` from `required_fields` turns off every date check.
- A line's `requires` list replaces that loss type's default requirements instead of adding to them. For example,
  `collision: {requires: [police_report]}` removes the no-injury requirement.
- An empty `loss_types` under `defaults` removes all evidence rules.
- `.nan` and `.inf` are accepted as numbers. A `.nan` confidence threshold turns off the confidence checks.
- If a key appears twice, the last one wins with no error.
- Some numbers don't mean what they appear to. `amount_tolerance: 1` means 100%, and YAML reads `030` as 24 and `1:30` as 90.
- Thresholds and windows can be set low enough to turn a check off, such as `confidence_threshold: 0`, and `confidence_checked`
  can be trimmed.

**Other rules file behavior**

- The old `CLAIMS_*` rule settings, such as `CLAIMS_FAST_TRACK_LIMIT`, are ignored with no warning. Move any you've set to the file.
- A broken rules file also stops reviewers from deciding claims that are already waiting.
- A running `claims watch` keeps the rules it started with, so restart it after an edit. A claim resumed after an edit can mix
  results from both versions.
- When someone holds policies on two lines and the claim gives no policy number, Claude's choice of policy also decides which
  line's rules apply.

## Claude Code setup

The repo includes project files for working on it with [Claude Code](https://code.claude.com/docs). Rules for working on
the code are in [CLAUDE.md](CLAUDE.md). Create a `.venv`, or set `CLAIMS_PYTHON`, so that the test hook can run.

| Item | Type | What it does |
|---|---|---|
| [routing-safety-reviewer](.claude/agents/routing-safety-reviewer.md) | Agent | Checks a change for any new way a claim could be wrongly fast-tracked. It's read-only. |
| [adversarial-case-writer](.claude/agents/adversarial-case-writer.md) | Agent | Writes synthetic `ADV-` claims designed to get wrongly fast-tracked. It never runs the live eval. |
| [/run-eval](.claude/skills/run-eval/SKILL.md) | Skill | Runs the live eval and reports the results. Only you can start it, because it costs money. |
| [add-eval-case](.claude/skills/add-eval-case/SKILL.md) | Skill | Adds a synthetic claim to the eval set. |
| [run_tests.sh](.claude/hooks/run_tests.sh) | Hook | Runs the offline tests after each Python edit, with the API key removed. |
| [settings.json](.claude/settings.json) | Settings | Lets tests and read-only git commands run without asking, and asks before `claims-eval` and `git push`. |

## Project layout

| Path | Contents |
|---|---|
| [src/claims/graph.py](src/claims/graph.py) | The LangGraph workflow |
| [src/claims/llm.py](src/claims/llm.py) | All calls to Claude |
| [src/claims/prompts.py](src/claims/prompts.py) | Prompts |
| [src/claims/routing.py](src/claims/routing.py), [coverage.py](src/claims/coverage.py), [consolidation.py](src/claims/consolidation.py) | The rules, with no model involved |
| [config/rules.yaml](config/rules.yaml), [src/claims/rules.py](src/claims/rules.py) | The fast-track rules, and the code that loads and checks them |
| [src/claims/audit.py](src/claims/audit.py) | The audit log |
| [src/claims/claims_system.py](src/claims/claims_system.py), [policies.py](src/claims/policies.py) | Mock systems to replace |
| [evals/cases/](evals/cases) | Synthetic claims |
| [tests/](tests) | Offline tests, with Claude's answers scripted |
| [CLAUDE.md](CLAUDE.md) | Instructions for Claude Code: commands, architecture and rules to keep |
| [.claude/](.claude) | Claude Code settings, the test hook, skills, and agents for safety review and adversarial cases |
