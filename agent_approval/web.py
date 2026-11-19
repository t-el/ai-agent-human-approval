"""FastAPI app: inbound webhook, the web approval queue, Slack interactivity and tool endpoints for n8n.

    POST /inbound                 new email/message (JSON)            -> run
    GET  /approvals               approval queue (HTML)
    GET  /approvals/{run_id}      draft, guardrail flags, tool trace, audit trail (HTML)
    POST /approvals/{run_id}/approve | /reject                       (form posts from that page)
    POST /slack/interactions      Slack button clicks (signed)
    POST /tools/{name}            the same read-only tools, for the n8n AI Agent's HTTP tools
    GET  /api/runs, /api/runs/{id}/audit, /api/audit/verify, /health
"""

from __future__ import annotations

import base64
import hmac
import html
import json
import logging
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .config import load_settings
from .logging_setup import setup_logging
from .service import ApprovalError, Service, build_service
from .slack import verify_slack_signature

log = logging.getLogger("agent.web")


class Inbound(BaseModel):
    message_id: str = Field(min_length=3)
    from_email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    from_name: str = ""
    subject: str = ""
    body: str = Field(min_length=1, max_length=20000)
    channel: str = "email"


STATUS_COLOR = {"pending_approval": "#2563eb", "needs_human": "#dc2626", "sent": "#059669", "rejected": "#64748b",
                "ignored": "#94a3b8", "send_failed": "#d97706", "received": "#64748b"}
SEV = {"block": ("#fef2f2", "#b91c1c", "Blocks approval"), "warn": ("#fffbeb", "#b45309", "Check"),
       "info": ("#eff6ff", "#1d4ed8", "Info")}

CSS = """
*{box-sizing:border-box}body{margin:0;font:15px/1.5 Inter,system-ui,-apple-system,Segoe UI,sans-serif;background:#f1f5f9;color:#0f172a}
header{background:#0f172a;color:#fff;padding:14px 28px;display:flex;align-items:center;gap:14px}
header b{font-size:17px}header span{color:#94a3b8;font-size:13px}header a{color:#c7d2fe;margin-left:auto;font-size:13px;text-decoration:none}
main{max-width:1180px;margin:24px auto;padding:0 24px}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:18px 20px;margin-bottom:16px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:14px;text-transform:uppercase;letter-spacing:.06em;color:#64748b;margin:0 0 10px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px 8px;border-bottom:1px solid #f1f5f9;font-size:14px;vertical-align:top}
th{color:#64748b;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.05em}
.pill{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600;color:#fff}
.grid{display:grid;grid-template-columns:1.25fr 1fr;gap:16px}.muted{color:#64748b;font-size:13px}
.msg{white-space:pre-wrap;background:#f8fafc;border-radius:8px;padding:12px;font-size:14px}
textarea{width:100%;min-height:170px;font-family:inherit;font-size:14px;line-height:1.5;border:1px solid #cbd5e1;border-radius:8px;padding:10px}
.btns{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}button{font-family:inherit;font-weight:600;font-size:14px;border:0;border-radius:8px;padding:10px 16px;cursor:pointer}
.ok{background:#059669;color:#fff}.no{background:#fff;color:#b91c1c;border:1px solid #fecaca}.ok[disabled]{background:#cbd5e1;cursor:not-allowed}
.flag{border-radius:8px;padding:8px 12px;margin-bottom:8px;font-size:13.5px}
.tl{border-left:2px solid #e2e8f0;margin-left:6px;padding-left:14px}.tl div{margin-bottom:8px;font-size:13px}
code{background:#f1f5f9;padding:1px 6px;border-radius:5px;font-size:12.5px}.kpis{display:flex;gap:12px;margin-bottom:16px}
.kpi{flex:1;background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:14px 16px}.kpi b{font-size:26px;display:block}
.err{background:#fef2f2;color:#991b1b;border:1px solid #fecaca;border-radius:8px;padding:10px 12px;margin-bottom:12px}
a{color:#2563eb}
"""


def page(title: str, body: str, svc: Service) -> HTMLResponse:
    ok, _ = svc.store.verify_audit()
    chain = "audit chain verified ✓" if ok else "AUDIT CHAIN BROKEN"
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>{CSS}</style></head><body>
<header><b>Support Agent · Approvals</b><span>AI drafts, humans decide · {chain}</span><a href="/approvals">Queue</a></header>
<main>{body}</main></body></html>""")


def pill(status: str) -> str:
    return f'<span class="pill" style="background:{STATUS_COLOR.get(status, "#64748b")}">{status.replace("_", " ")}</span>'


def create_app(service: Service | None = None) -> FastAPI:
    if service is None:
        settings = load_settings()
        setup_logging(settings.log_json)
        service = build_service(settings)
    svc = service
    s = svc.settings
    app = FastAPI(title="AI agent with human approval", version="1.0.0")
    app.state.service = svc

    def approver(request: Request) -> str:
        """Basic auth for the approval UI when APPROVER_PASSWORD is set. Returns the approver's name."""
        if not s.approver_password:
            return s.approver_user or "demo-approver"
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            user, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
            if hmac.compare_digest(user, s.approver_user) and hmac.compare_digest(pw, s.approver_password):
                return user
        raise HTTPException(401, headers={"WWW-Authenticate": 'Basic realm="approvals"'})

    @app.get("/health")
    def health():
        ok, broken = svc.store.verify_audit()
        return {"ok": True, "mock_llm": s.mock_llm, "audit_chain_ok": ok,
                "pending": len(svc.store.list_runs("pending_approval")), "open_dead_letters": len(svc.store.dead_letters())}

    @app.post("/inbound")
    def inbound(msg: Inbound):
        run = svc.process(msg.model_dump())
        code = 200 if run.get("duplicate") else 201
        return JSONResponse({"run_id": run["id"], "status": run["status"], "duplicate": bool(run.get("duplicate")),
                             "review_url": f"{s.public_url}/approvals/{run['id']}"}, code)

    @app.post("/tools/{name}")
    async def tool(name: str, request: Request):
        tr = svc.tools.call(name, await request.json())
        return JSONResponse(tr.result, 200 if tr.ok else 422)

    @app.get("/api/runs")
    def api_runs(status: str | None = None):
        return [{k: r[k] for k in ("id", "status", "created_at", "actor")} | {"subject": r["inbound"].get("subject")}
                for r in svc.store.list_runs(status)]

    @app.get("/api/runs/{run_id}/audit")
    def api_audit(run_id: str):
        return svc.store.audit_for(run_id)

    @app.get("/api/audit/verify")
    def api_verify():
        ok, broken = svc.store.verify_audit()
        return {"ok": ok, "first_broken_row": broken}

    # ------------------------------------------------------------------ HTML
    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/approvals")

    @app.get("/approvals", response_class=HTMLResponse)
    def queue(user: str = Depends(approver)):
        runs = svc.store.list_runs()
        count = lambda st: sum(1 for r in runs if r["status"] == st)   # noqa: E731
        kpis = "".join(f'<div class="kpi"><span class="muted">{label}</span><b>{count(st)}</b></div>' for st, label in
                       [("pending_approval", "Waiting for approval"), ("needs_human", "Needs a human"),
                        ("sent", "Sent"), ("rejected", "Rejected"), ("ignored", "Ignored (spam)")])
        order = {"needs_human": 0, "pending_approval": 1, "send_failed": 2}
        rows = ""
        for r in sorted(runs, key=lambda r: (order.get(r["status"], 9), r["created_at"])):
            m, d = r["inbound"], r["draft"]
            flags = [f for f in r["findings"] if f["severity"] in ("block", "warn")]
            conf = f"{d['confidence']:.0%}" if d.get("intent") else "—"
            fl = " ".join(f'<span class="pill" style="background:{SEV[f["severity"]][1]}">{f["code"]}</span>'
                          for f in flags[:3])
            rows += (f'<tr><td>{pill(r["status"])}</td><td><a href="/approvals/{r["id"]}"><b>{html.escape(m.get("subject", ""))}</b></a>'
                     f'<div class="muted">{html.escape(m.get("from_name", ""))} &lt;{html.escape(m["from_email"])}&gt;</div></td>'
                     f'<td>{html.escape(d.get("intent", "—"))}</td><td>{conf}</td><td>{fl or "—"}</td>'
                     f'<td class="muted">{r["created_at"][11:16]} UTC</td></tr>')
        body = (f'<h1>Approval queue</h1><p class="muted">Signed in as <b>{html.escape(user)}</b>. '
                f'Every AI draft waits here (and in Slack) until a person approves, edits or rejects it.</p>'
                f'<div class="kpis">{kpis}</div><div class="card"><table><tr><th>Status</th><th>Message</th>'
                f'<th>Intent</th><th>Confidence</th><th>Guardrail flags</th><th>Received</th></tr>{rows}</table></div>')
        return page("Approval queue", body, svc)

    @app.get("/approvals/{run_id}", response_class=HTMLResponse)
    def detail(run_id: str, error: str = "", user: str = Depends(approver)):
        r = svc.store.get_run(run_id)
        if not r:
            raise HTTPException(404)
        m, d = r["inbound"], r["draft"]
        blocked = any(f["severity"] == "block" for f in r["findings"])
        flags = "".join(f'<div class="flag" style="background:{SEV[f["severity"]][0]};color:{SEV[f["severity"]][1]}">'
                        f'<b>{f["code"]}</b> · {SEV[f["severity"]][2]}<br>{html.escape(f["message"])}</div>'
                        for f in r["findings"]) or '<p class="muted">No guardrail flags.</p>'
        tools = "".join(f'<div><code>{t["tool"]}</code> {html.escape(json.dumps(t["args"]))} · {t["ms"]} ms '
                        f'{"✓" if t["ok"] else "✗ failed"}<br><span class="muted">{html.escape(t["result_preview"][:160])}</span></div>'
                        for t in r["tools"]) or '<p class="muted">No tools called.</p>'
        audit = "".join(f'<div><b>{a["event"].replace("_", " ")}</b> <span class="muted">{a["ts"][11:19]} · {html.escape(a["actor"])} · '
                        f'#{a["hash"][:8]}</span></div>' for a in svc.store.audit_for(run_id))
        reply = r["final_reply"] or d.get("reply", "")
        can_act = r["status"] in ("pending_approval", "send_failed")
        decision = ""
        if can_act:
            hint = ("This draft is <b>blocked</b> by a guardrail. Edit it so the flagged text is gone; your edit is "
                    "re-checked before sending.") if blocked else "Approve as written, or edit first."
            decision = f"""<form method="post" action="/approvals/{run_id}/approve">
<p class="muted">{hint}</p><textarea name="reply">{html.escape(reply)}</textarea>
<div class="btns"><button class="ok" type="submit">{"Approve edited reply & send" if blocked else "Approve & send"}</button>
<button class="no" formaction="/approvals/{run_id}/reject" type="submit">Reject</button></div></form>"""
        elif r["status"] == "needs_human":
            decision = (f'<p>No AI reply will be sent for this message. Reply from your inbox, then close it.</p>'
                        f'<form method="post" action="/approvals/{run_id}/reject"><button class="no">Close (handled manually)</button></form>')
        else:
            decision = f'<div class="msg">{html.escape(reply) or "—"}</div><p class="muted">Decided by {html.escape(r["actor"] or "system")}.</p>'
        cites = ", ".join(f"<code>{c}</code>" for c in d.get("citations", [])) or "—"
        err = f'<div class="err">{html.escape(error)}</div>' if error else ""
        body = f"""<p><a href="/approvals">← Queue</a></p>
<h1>{html.escape(m.get("subject", ""))} {pill(r["status"])}</h1>
<p class="muted">{html.escape(m.get("from_name", ""))} &lt;{html.escape(m["from_email"])}&gt; · run <code>{run_id}</code> ·
intent <b>{html.escape(d.get("intent", "—"))}</b> · confidence {d.get("confidence", 0):.0%}</p>{err}
<div class="grid"><div>
<div class="card"><h2>Customer message</h2><div class="msg">{html.escape(m["body"])}</div></div>
<div class="card"><h2>AI draft reply</h2>{decision}<p class="muted">Sources: {cites}</p>
{f'<p class="muted">Internal note: {html.escape(d["internal_note"])}</p>' if d.get("internal_note") else ""}</div>
</div><div>
<div class="card"><h2>Guardrails</h2>{flags}</div>
<div class="card"><h2>Tool calls</h2><div class="tl">{tools}</div></div>
<div class="card"><h2>Audit trail</h2><div class="tl">{audit}</div></div>
</div></div>"""
        return page(m.get("subject", "Run"), body, svc)

    @app.post("/approvals/{run_id}/approve")
    async def approve(run_id: str, request: Request, user: str = Depends(approver)):
        form = parse_qs((await request.body()).decode())
        try:
            svc.approve(run_id, user, (form.get("reply") or [""])[0])
        except ApprovalError as exc:
            from urllib.parse import quote
            return RedirectResponse(f"/approvals/{run_id}?error={quote(str(exc))}", 303)
        return RedirectResponse(f"/approvals/{run_id}", 303)

    @app.post("/approvals/{run_id}/reject")
    async def reject(run_id: str, user: str = Depends(approver)):
        try:
            svc.reject(run_id, user, "rejected in web UI")
        except ApprovalError as exc:
            raise HTTPException(409, str(exc))
        return RedirectResponse(f"/approvals/{run_id}", 303)

    # ------------------------------------------------------------------ Slack
    @app.post("/slack/interactions")
    async def slack_interactions(request: Request):
        raw = await request.body()
        if not verify_slack_signature(s.slack_signing_secret, request.headers.get("X-Slack-Request-Timestamp", ""),
                                      raw, request.headers.get("X-Slack-Signature", "")):
            raise HTTPException(401, "bad Slack signature")
        payload = json.loads(parse_qs(raw.decode())["payload"][0])
        action = payload["actions"][0]
        user = "slack:" + payload.get("user", {}).get("username", "unknown")
        run_id = action.get("value", "")
        try:
            if action["action_id"] == "approve":
                run = svc.approve(run_id, user)
                text = f":white_check_mark: Sent by {user}" if run["status"] == "sent" else f":warning: {run['status']}"
            elif action["action_id"] == "reject":
                svc.reject(run_id, user, "rejected in Slack")
                text = f":x: Rejected by {user}"
            else:
                return {"ok": True}
        except ApprovalError as exc:
            text = f":warning: {exc}"
        return {"replace_original": True, "text": text}

    return app
