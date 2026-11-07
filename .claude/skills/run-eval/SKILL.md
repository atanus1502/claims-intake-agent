---
name: run-eval
description: Run the live eval against real Claude and report the results. It costs real API usage, so it only runs when the user asks.
disable-model-invocation: true
---

Run the live eval and report what it shows. Arguments, if any, go to `claims-eval` (for example `--limit 3` or `--case FT-01-glass`).

1. Run `pytest` first. If it fails, stop and report the failure, since the eval won't mean much.
2. Tell the user the expected cost: about $0.03 per claim for the last measured run, so about $0.50 for all 17. If no
   arguments were given and the code has changed since the last run, suggest `--limit 3` first.
3. Run `claims-eval` with the arguments. Results are saved to `evals/results/<timestamp>/`.
4. Report:
   - `wrongly_fast_tracked` first. Anything above zero is the main finding, so name each case.
   - Correct routes and reasons, `fast_track_sent_to_person`, field accuracy, and total cost.
   - `cases_with_model_errors`. If this is above zero, the results don't measure accuracy, so show the error instead.
   - What changed since the previous run in `evals/results/`, if there is one.
5. Say that the results come from synthetic claims and can't show real-world accuracy.

Don't change code, prompts or rules to fix a failure unless the user asks.
