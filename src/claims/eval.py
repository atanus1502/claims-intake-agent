"""Runs synthetic claims through the real graph and real Claude, and grades each result.

The number that matters most is wrongly_fast_tracked: a claim a person should have seen. It should be zero.
Each case gets its own empty data folder. Mock policies are shared and read-only.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from claims.config import Settings
from claims.llm import estimate_cost_usd
from claims.routing import FAST_TRACK
from claims.service import ClaimsAgent

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CASES_DIR = PROJECT_ROOT / "evals" / "cases"
RESULTS_DIR = PROJECT_ROOT / "evals" / "results"
HIGH_CONFIDENCE = 0.9


def _same(name: str, actual: Any, expected: Any) -> bool:
    if actual is None:
        return False
    if name == "amount":
        return Decimal(str(actual)) == Decimal(str(expected))
    return str(actual).casefold() == str(expected).casefold()


def grade(expected: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    decision = state["decision"]
    claim = state["claim"]
    codes = {r["code"] for r in decision["reasons"]}
    wanted_any = expected.get("any_reason", [])

    checks = []
    for name, value in expected.get("fields", {}).items():
        if name == "amount":
            actual, confidence = claim["amount"], claim["amount_confidence"]
        else:
            field = claim["fields"].get(name, {})
            actual, confidence = field.get("value"), field.get("confidence", 0.0)
        checks.append({"field": name, "expected": value, "actual": actual, "confidence": confidence, "correct": _same(name, actual, value)})

    route_correct = decision["route"] == expected["route"]
    reason_correct = not wanted_any or bool(codes & set(wanted_any))
    return {
        "route": decision["route"],
        "expected_route": expected["route"],
        "route_correct": route_correct,
        "reason_correct": reason_correct,
        "wrongly_fast_tracked": decision["route"] == FAST_TRACK and expected["route"] != FAST_TRACK,
        "reason_codes": sorted(codes),
        "reasons": decision["reasons"],
        "calls_by_model": dict(Counter(call["model"] for call in state.get("usage", []))),
        "fields": checks,
        "cost_usd": estimate_cost_usd(state.get("usage", [])),
    }


def run_case(case_dir: Path, settings: Settings) -> dict[str, Any]:
    expected = json.loads((case_dir / "expected.json").read_text())
    with tempfile.TemporaryDirectory() as data_dir:
        agent = ClaimsAgent(replace(settings, data_dir=Path(data_dir)))
        started = time.monotonic()
        try:
            result = agent.process_email(case_dir / "email.eml", received_at=expected["received_at"])
            graded = grade(expected, result["state"])
            # Failed calls send a claim to a person, which can look like a correct escalation. Keep them visible.
            graded["model_errors"] = [
                r["details"]["error"] for r in agent.audit.read() if r["event"] in ("extraction_failed", "coverage_failed")
            ]
        except Exception as exc:  # one bad case shouldn't lose the run
            return {"case": case_dir.name, "error": f"{type(exc).__name__}: {exc}", "route_correct": False, "reason_correct": False}
        finally:
            agent.close()
    return {"case": case_dir.name, "seconds": round(time.monotonic() - started, 1), **graded}


def summarise(results: list[dict[str, Any]], settings: Settings) -> dict[str, Any]:
    fields = [check for r in results for check in r.get("fields", [])]
    confident = [check for check in fields if check["confidence"] >= HIGH_CONFIDENCE]
    costs = [r.get("cost_usd") for r in results]
    calls: Counter[str] = Counter()
    for r in results:
        calls.update(r.get("calls_by_model", {}))
    return {
        "model": settings.model,
        "complex_model": settings.complex_model,
        "calls_by_model": dict(calls),
        "cases_using_complex_model": sum(settings.complex_model in r.get("calls_by_model", {}) for r in results),
        "effort": settings.effort,
        "cases": len(results),
        "errors": sum("error" in r for r in results),
        "cases_with_model_errors": sum(bool(r.get("model_errors")) for r in results),
        "route_correct": sum(r["route_correct"] for r in results),
        "reason_correct": sum(r["reason_correct"] for r in results),
        "wrongly_fast_tracked": sum(r.get("wrongly_fast_tracked", False) for r in results),
        "fast_track_sent_to_person": sum(
            r.get("expected_route") == FAST_TRACK and r.get("route") != FAST_TRACK for r in results
        ),
        "field_accuracy": round(sum(f["correct"] for f in fields) / len(fields), 3) if fields else None,
        "high_confidence_field_accuracy": round(sum(f["correct"] for f in confident) / len(confident), 3) if confident else None,
        "total_cost_usd": round(sum(c for c in costs if c is not None), 4),
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="claims-eval", description=__doc__)
    parser.add_argument("--case", action="append", help="run only this case (can be repeated)")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    settings = replace(Settings.from_env(), policy_file=PROJECT_ROOT / "config" / "mock_policies.yaml", rules_file=PROJECT_ROOT / "config" / "rules.yaml")
    case_dirs = sorted(p for p in CASES_DIR.iterdir() if (p / "expected.json").exists())
    if args.case:
        case_dirs = [p for p in case_dirs if p.name in args.case]
    case_dirs = case_dirs[: args.limit]

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda case: run_case(case, settings), case_dirs))

    for r in results:
        mark = "ok  " if r["route_correct"] and r["reason_correct"] and not r.get("model_errors") else "FAIL"
        detail = r.get("error") or f"{r['route']} ({', '.join(r['reason_codes']) or 'no reasons'}) {r['seconds']}s"
        print(f"{mark} {r['case']}: {detail}")
        if r.get("model_errors"):
            print(f"     model error: {r['model_errors'][0][:300]}")

    summary = summarise(results, settings)
    out = RESULTS_DIR / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in results))
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    clean = summary["wrongly_fast_tracked"] == 0 and summary["errors"] == 0 and summary["cases_with_model_errors"] == 0
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
