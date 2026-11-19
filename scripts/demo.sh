#!/usr/bin/env bash
# End-to-end demo with the deterministic mock LLM (no API keys, no network).
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
PORT=${PORT:-8800}
export DATA_DIR=${DATA_DIR:-data/demo} LOG_JSON=false PUBLIC_URL="http://localhost:$PORT" RETRY_BASE_DELAY=0.1 \
       SLACK_SIGNING_SECRET=demo-signing-secret
unset OPENAI_API_KEY || true
rm -rf "$DATA_DIR" && mkdir -p "$DATA_DIR"
$PY -m uvicorn --factory agent_approval.web:create_app --port "$PORT" --log-level warning > "$DATA_DIR/server.log" 2>&1 &
PID=$!; trap 'kill $PID 2>/dev/null' EXIT
for _ in $(seq 1 60); do curl -sf "localhost:$PORT/health" >/dev/null && break; sleep 0.25; done

echo "### 1. Eight inbound emails hit POST /inbound (agent: CRM + Google Sheet + knowledge base tools)"
$PY - <<PY
import json, httpx
for m in json.load(open("samples/inbound.json")):
    r = httpx.post("http://127.0.0.1:$PORT/inbound", json=m, timeout=30).json()
    print(f"  {r['run_id']}  {r['status']:<17} {m['from_email']:<28} {m['subject'][:40]}")
r = httpx.post("http://127.0.0.1:$PORT/inbound", json=json.load(open("samples/inbound.json"))[0])
print(f"  (same email delivered twice) -> HTTP {r.status_code}, duplicate={r.json()['duplicate']}")
PY

echo; echo "### 2. What the approver sees in Slack (first approval request, Block Kit)"
$PY - <<PY
import json
msg = json.loads(open("$DATA_DIR/slack.jsonl").readline())
for b in msg["blocks"]:
    if b["type"] == "header": print("  #", b["text"]["text"])
    elif b["type"] == "section" and "fields" in b: print("  ", " | ".join(f["text"].replace("\n", ": ") for f in b["fields"]))
    elif b["type"] == "section": print("  ", b["text"]["text"].replace("\n", "\n   "))
    elif b["type"] == "actions": print("   [" + "] [".join(e["text"]["text"] for e in b["elements"]) + "]")
PY

RUN() { $PY -c "import httpx,sys; print(next(r['id'] for r in httpx.get('http://127.0.0.1:$PORT/api/runs').json() if r['subject'].startswith(sys.argv[1])))" "$1"; }
LEO=$(RUN "Where is"); MIKE=$(RUN "Refund please"); TOM=$(RUN "urgent"); PRIYA=$(RUN "Upgrade")

echo; echo "### 3. Approve Leo's order-status reply with the Slack button (signed request)"
$PY - <<PY
import hashlib, hmac, json, time, httpx
from urllib.parse import urlencode
body = urlencode({"payload": json.dumps({"type": "block_actions", "user": {"username": "taha"},
                  "actions": [{"action_id": "approve", "value": "$LEO"}]})}).encode()
ts = str(int(time.time()))
sig = "v0=" + hmac.new(b"demo-signing-secret", f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
h = {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig, "Content-Type": "application/x-www-form-urlencoded"}
print("  Slack ->", httpx.post("http://127.0.0.1:$PORT/slack/interactions", content=body, headers=h).json()["text"])
print("  Slack click retried ->", httpx.post("http://127.0.0.1:$PORT/slack/interactions", content=body, headers=h).json()["text"])
h["X-Slack-Signature"] = "v0=forged"
print("  Forged click -> HTTP", httpx.post("http://127.0.0.1:$PORT/slack/interactions", content=body, headers=h).status_code)
PY

echo; echo "### 4. Approve Mike's refund reply and Priya's pricing answer from the web page"
for R in $MIKE $PRIYA; do
  curl -s -o /dev/null -w "  POST /approvals/$R/approve -> HTTP %{http_code}\n" -X POST "localhost:$PORT/approvals/$R/approve" --data-urlencode "reply="
done

echo; echo "### 5. Prompt-injection email: the naive draft is blocked; an edited reply goes through"
$PY -m agent_approval.cli approve "$TOM" --as taha 2>/dev/null || true
$PY -m agent_approval.cli approve "$TOM" --as taha --reply "Sure, I have issued your refund." 2>/dev/null || true
$PY -m agent_approval.cli approve "$TOM" --as taha --reply "Hi Tom, for your security we can't make billing changes from an email request. Please reply with your order number and our billing team will review it." 2>/dev/null

echo; echo "### 6. Outbox (mock email sender: data/demo/outbox.jsonl)"
$PY -c "
import json
for l in open('$DATA_DIR/outbox.jsonl'):
    m = json.loads(l); print(f\"  to {m['to']:<26} {m['body'][:95]}...\")"

echo; echo "### 7. Audit trail for Leo's run (hash-chained, tamper-evident)"
$PY -m agent_approval.cli audit "$LEO" 2>/dev/null
$PY -m agent_approval.cli verify-audit 2>/dev/null

echo; echo "### 8. Queue after the decisions"
$PY -m agent_approval.cli queue 2>/dev/null

echo; echo "### 9. Failure drills: LLM outage and SMTP outage (separate data dir)"
DATA_DIR=$DATA_DIR/drill $PY - <<'PY'
import json, logging
logging.disable(logging.CRITICAL)
from agent_approval.config import load_settings
from agent_approval.llm import MockLLM
from agent_approval.sender import MockSender
from agent_approval.service import build_service
s = load_settings()
msgs = json.load(open("samples/inbound.json"))
svc = build_service(s, llm=MockLLM(fail_times=3), sender=MockSender(s.data_dir / "outbox.jsonl", fail_times=3), sleep=lambda x: None)
run = svc.process(msgs[2])
print(f"  LLM down (3 x 503)        -> {run['status']}; dead letter: {svc.store.dead_letters()[0]['error']}")
run = svc.replay(svc.store.dead_letters()[0]["id"])
print(f"  LLM back, replay          -> {run['status']}")
run = svc.approve(run["id"], "taha")
print(f"  approve, SMTP 451 x3      -> {run['status']}; dead letter kind: {svc.store.dead_letters()[0]['kind']}")
run = svc.replay(svc.store.dead_letters()[0]["id"])
print(f"  SMTP back, replay         -> {run['status']}; emails sent: {sum(1 for _ in open(s.data_dir / 'outbox.jsonl'))}")
PY

echo; echo "### 10. Eval suite (10 cases: refunds, injection, legal, spam, unknown sender...)"
$PY -m agent_approval.cli eval 2>/dev/null
echo; echo "Demo complete. Approval UI: ./scripts/serve.sh then open http://localhost:$PORT/approvals"
