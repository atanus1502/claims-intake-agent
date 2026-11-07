from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from claims.claims_system import PENDING_REVIEW
from claims.config import Settings
from claims.graph import REVIEW_OUTCOMES
from claims.rules import RulesError, load_rules
from claims.service import DISABLED, PROCESSED, AWAITING_REVIEW, ClaimsAgent

log = logging.getLogger("claims")


def _summary(result: dict) -> str:
    line = f"{result.get('claim_id', '-')}: {result['status']}"
    if "claim_status" in result:
        line += f" -> {result['claim_status']}"
    return line


def _run(agent: ClaimsAgent, args: argparse.Namespace) -> int:
    for path in args.files:
        result = agent.process_email(path)
        print(_summary(result))
        if result["status"] in (PROCESSED, AWAITING_REVIEW):
            for item in result["state"].get("decision", {}).get("reasons", []):
                print(f"  - {item['code']}: {item['detail']}")
    return 0


def _watch(agent: ClaimsAgent, args: argparse.Namespace) -> int:
    print(f"Watching {args.folder} every {args.interval}s. Press Ctrl-C to stop.")
    seen: set[Path] = set()
    while True:
        for path in sorted(args.folder.glob("*.eml")):
            if path in seen:
                continue
            try:
                result = agent.process_email(path)
            except Exception:
                # Left out of seen, so it's retried. The checkpoint means finished steps aren't repeated.
                log.exception("could not process %s", path)
                continue
            print(f"{path.name}: {_summary(result)}", flush=True)
            if result["status"] != DISABLED:
                seen.add(path)
        time.sleep(args.interval)


def _review(agent: ClaimsAgent, args: argparse.Namespace) -> int:
    if args.action == "list":
        for claim in agent.claims.by_status(PENDING_REVIEW):
            codes = sorted({r["code"] for r in claim["details"].get("decision", {}).get("reasons", [])})
            print(f"{claim['id']}  {claim['sender']}  {', '.join(codes)}")
    elif args.action == "show":
        claim = agent.claims.get(args.claim_id)
        if claim is None:
            print(f"No claim {args.claim_id}", file=sys.stderr)
            return 1
        print(json.dumps(claim, indent=2))
    else:
        print(_summary(agent.review(args.claim_id, args.decision, args.reviewer, args.note)))
    return 0


def _audit(agent: ClaimsAgent, args: argparse.Namespace) -> int:
    if args.action == "verify":
        intact, message = agent.audit.verify()
        print(message)
        return 0 if intact else 1
    for record in agent.audit.read(args.claim_id):
        print(json.dumps(record))
    return 0


def _rules(agent: ClaimsAgent, args: argparse.Namespace) -> int:
    rules = load_rules(agent.settings.rules_file)
    print(f"{agent.settings.rules_file} is valid: {rules.version} ({rules.digest})")
    print(f"Confidence threshold {rules.confidence_threshold}, amount tolerance {rules.amount_tolerance}")
    for name, line in rules.lines.items():
        print(f"{name}: fast-track limit {line.fast_track_limit}, late report {line.late_report_days} days, "
              f"new policy {line.new_policy_days} days, max prior claims {line.max_prior_claims}")
    return 0


def _graph(agent: ClaimsAgent, args: argparse.Namespace) -> int:
    print(agent.graph.get_graph().draw_mermaid())
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="claims", description="Claims intake agent")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="process .eml files")
    run.add_argument("files", nargs="+", type=Path)
    run.set_defaults(handler=_run)

    watch = commands.add_parser("watch", help="process .eml files as they appear in a folder")
    watch.add_argument("folder", type=Path)
    watch.add_argument("--interval", type=float, default=30)
    watch.set_defaults(handler=_watch)

    review = commands.add_parser("review", help="list, read and decide claims waiting for a person")
    actions = review.add_subparsers(dest="action", required=True)
    actions.add_parser("list")
    actions.add_parser("show").add_argument("claim_id")
    decide = actions.add_parser("decide")
    decide.add_argument("claim_id")
    decide.add_argument("decision", choices=sorted(REVIEW_OUTCOMES))
    decide.add_argument("--reviewer", required=True)
    decide.add_argument("--note", default="")
    review.set_defaults(handler=_review)

    audit = commands.add_parser("audit", help="read the audit log or check it hasn't been altered")
    audit_actions = audit.add_subparsers(dest="action", required=True)
    audit_actions.add_parser("verify")
    audit_actions.add_parser("show").add_argument("claim_id")
    audit.set_defaults(handler=_audit)

    commands.add_parser("rules", help="check the rules file and show what it sets").set_defaults(handler=_rules)

    commands.add_parser("graph", help="print the workflow as a Mermaid diagram").set_defaults(handler=_graph)

    args = parser.parse_args()
    agent = ClaimsAgent(Settings.from_env())
    try:
        return args.handler(agent, args)
    except KeyboardInterrupt:
        return 0
    except RulesError as exc:
        print(f"Rules file problem, so nothing was processed: {exc}", file=sys.stderr)
        return 1
    finally:
        agent.close()


if __name__ == "__main__":
    sys.exit(main())
