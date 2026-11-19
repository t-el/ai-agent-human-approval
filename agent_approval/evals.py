"""Eval runner: replays evals/cases.json through the real pipeline and checks behaviour, not exact wording.

Run it with the mock LLM in CI, and with your real model (OPENAI_API_KEY) before every prompt or model change.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT, Settings
from .service import build_service


@dataclass
class EvalResult:
    case_id: str
    passed: bool
    failures: list[str]
    status: str


def check_case(case: dict, run: dict) -> list[str]:
    e, d = case["expect"], run["draft"]
    reply = (d.get("reply") or "").lower()
    codes = {f["code"] for f in run["findings"]}
    tools = {t["tool"] for t in run["tools"]}
    fails = []
    if run["status"] != e["status"]:
        fails.append(f"status {run['status']} != {e['status']}")
    if "intent" in e and d.get("intent") != e["intent"]:
        fails.append(f"intent {d.get('intent')} != {e['intent']}")
    fails += [f"tool {t} not called" for t in e.get("tools", []) if t not in tools]
    fails += [f"reply missing {s!r}" for s in e.get("must_include", []) if s.lower() not in reply]
    fails += [f"reply contains {s!r}" for s in e.get("must_not_include", []) if s.lower() in reply]
    fails += [f"flag {c} not raised" for c in e.get("flags", []) if c not in codes]
    fails += [f"citation {c} missing" for c in e.get("citations", []) if c not in d.get("citations", [])]
    blocked = any(f["severity"] == "block" for f in run["findings"])
    if e.get("no_blocking") and blocked:
        fails.append(f"unexpected blocking flags {[f['code'] for f in run['findings'] if f['severity'] == 'block']}")
    if e.get("blocked") and not blocked:
        fails.append("expected the draft to be blocked")
    if "note_includes" in e and e["note_includes"].lower() not in (d.get("internal_note") or "").lower():
        fails.append(f"internal note missing {e['note_includes']!r}")
    return fails


def run_evals(settings: Settings | None = None, cases_path: Path = ROOT / "evals" / "cases.json") -> list[EvalResult]:
    cases = json.loads(cases_path.read_text())
    with tempfile.TemporaryDirectory() as tmp:
        s = settings or Settings(data_dir=Path(tmp), log_json=False)
        if settings:
            s.data_dir = Path(tmp)
        svc = build_service(s)
        out = []
        for c in cases:
            run = svc.process(c["message"] | {"channel": "email"})
            fails = check_case(c, run)
            out.append(EvalResult(c["id"], not fails, fails, run["status"]))
        return out
