---
name: add-eval-case
description: Add a synthetic claim to the eval set. Use when the user wants to test a new claim scenario or cover a routing gap.
---

Add a case to `CASES_SPEC` in `scripts/generate_cases.py`, then regenerate the cases.

1. Name it by expected route: `FT-NN-short-name` for fast-track, `HR-NN-short-name` for human review, using the next number.
2. Use a synthetic policyholder from `config/mock_policies.yaml`, and send from the address on that policy unless the case
   tests a mismatch. Add a new mock policy if the scenario needs one. Keep every person and number made up.
3. Make dates fit the fixed received date of 2026-11-02 and the policy's period.
4. For `HR` cases, set `any_reason` to the reason codes that make the case pass, so that going to a person for the wrong
   reason counts as a miss. Keep each case to one problem, so a pass shows that this rule fired.
5. For `FT` cases, check that nothing else would send it to a person: the amount must exceed the deductible and stay under
   the limits, and a collision must state that no one was hurt and no other party was involved.
6. Set `fields` to the values the eval should check.
7. Run `python scripts/generate_cases.py`, then `pytest`.

Don't run the live eval without asking. Tell the user that `/run-eval --case <name>` will test the new case.
