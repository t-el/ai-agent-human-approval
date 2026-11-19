"""CLI.

    python -m agent_approval.cli process samples/inbound.json
    python -m agent_approval.cli queue
    python -m agent_approval.cli approve <run_id> [--as name] [--reply "edited text"]
    python -m agent_approval.cli reject <run_id>
    python -m agent_approval.cli eval
    python -m agent_approval.cli dlq | replay <id>
    python -m agent_approval.cli audit <run_id> | verify-audit
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import load_settings
from .evals import run_evals
from .logging_setup import setup_logging
from .service import ApprovalError, build_service


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agent_approval")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("process").add_argument("file")
    sub.add_parser("queue")
    a = sub.add_parser("approve")
    a.add_argument("run_id")
    a.add_argument("--as", dest="actor", default="cli-approver")
    a.add_argument("--reply")
    sub.add_parser("reject").add_argument("run_id")
    sub.add_parser("eval")
    sub.add_parser("dlq")
    sub.add_parser("replay").add_argument("id", type=int)
    sub.add_parser("audit").add_argument("run_id")
    sub.add_parser("verify-audit")
    args = ap.parse_args(argv)

    s = load_settings()
    setup_logging(s.log_json)
    if args.cmd == "eval":
        res = run_evals(s)
        for r in res:
            print(f"  {'PASS' if r.passed else 'FAIL'}  {r.case_id:<18} status={r.status:<17} {'; '.join(r.failures)}")
        print(f"\n{sum(r.passed for r in res)}/{len(res)} eval cases passed (model: {'mock' if s.mock_llm else s.openai_model})")
        return 0 if all(r.passed for r in res) else 1

    svc = build_service(s)
    if args.cmd == "process":
        for m in json.load(open(args.file)):
            run = svc.process(m)
            flags = ",".join(f["code"] for f in run["findings"] if f["severity"] != "info")
            print(f"  {run['id']}  {run['status']:<17} {run['draft'].get('intent', '-'):<13} "
                  f"{m['from_email']:<28} {'DUPLICATE' if run.get('duplicate') else flags}")
    elif args.cmd == "queue":
        for r in svc.store.list_runs():
            print(f"  {r['id']}  {r['status']:<17} {r['inbound'].get('subject', '')[:50]}")
    elif args.cmd == "approve":
        try:
            run = svc.approve(args.run_id, args.actor, args.reply)
            print(f"  {args.run_id}: {run['status']}{' (already sent, not resent)' if run.get('already_sent') else ''}")
        except ApprovalError as exc:
            print(f"  {args.run_id}: refused: {exc}")
            return 1
    elif args.cmd == "reject":
        print(f"  {args.run_id}: {svc.reject(args.run_id, 'cli-approver')['status']}")
    elif args.cmd == "dlq":
        items = svc.store.dead_letters()
        print("  Dead-letter queue is empty." if not items else "")
        for d in items:
            print(f"  #{d['id']} {d['run_id']} kind={d['kind']} {d['error'][:100]}")
    elif args.cmd == "replay":
        run = svc.replay(args.id)
        print(f"  replay #{args.id}: {run['id']} -> {run['status']}")
    elif args.cmd == "audit":
        for a in svc.store.audit_for(args.run_id):
            print(f"  {a['ts']}  {a['event']:<20} {a['actor']:<16} #{a['hash'][:10]}  {json.dumps(a['data'])[:90]}")
    elif args.cmd == "verify-audit":
        ok, broken = svc.store.verify_audit()
        print("  audit chain OK" if ok else f"  audit chain BROKEN at row {broken}")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
