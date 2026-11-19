"""Slack approval message (Block Kit buttons), request-signature check, and a mock that writes to a file."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path

import httpx

from .retry import check, with_retry

SEV_ICON = {"block": ":no_entry:", "warn": ":warning:", "info": ":information_source:"}


def approval_blocks(run: dict, public_url: str) -> list[dict]:
    msg, d = run["inbound"], run["draft"]
    blocked = any(f["severity"] == "block" for f in run["findings"])
    flags = "\n".join(f"{SEV_ICON[f['severity']]} *{f['code']}*: {f['message']}" for f in run["findings"]) or "No flags"
    tools = ", ".join(sorted({t["tool"] for t in run["tools"]})) or "none"
    actions = [
        {"type": "button", "text": {"type": "plain_text", "text": "Edit in browser"},
         "url": f"{public_url}/approvals/{run['id']}", "action_id": "open"},
        {"type": "button", "style": "danger", "text": {"type": "plain_text", "text": "Reject"},
         "action_id": "reject", "value": run["id"]},
    ]
    if not blocked:
        actions.insert(0, {"type": "button", "style": "primary", "text": {"type": "plain_text", "text": "Approve & send"},
                           "action_id": "approve", "value": run["id"],
                           "confirm": {"title": {"type": "plain_text", "text": "Send this reply?"},
                                       "text": {"type": "mrkdwn", "text": f"To {msg['from_email']}"},
                                       "confirm": {"type": "plain_text", "text": "Send"},
                                       "deny": {"type": "plain_text", "text": "Cancel"}}})
    return [
        {"type": "header", "text": {"type": "plain_text", "text": f"Reply needs approval: {msg.get('subject', '')[:60]}"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*From*\n{msg.get('from_name', '')} <{msg['from_email']}>"},
            {"type": "mrkdwn", "text": f"*Intent*\n{d.get('intent')} ({d.get('confidence', 0):.0%})"}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Draft reply*\n>" + d.get("reply", "").replace("\n", "\n>")}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"Tools used: {tools} · Run `{run['id']}`"}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": flags}},
        {"type": "actions", "block_id": "approval", "elements": actions},
    ]


def verify_slack_signature(secret: str, timestamp: str, body: bytes, signature: str, now: float | None = None) -> bool:
    """Slack signs requests: v0=HMAC_SHA256(secret, 'v0:{ts}:{body}'). Reject anything older than 5 minutes."""
    try:
        if abs((now or time.time()) - int(timestamp)) > 300:
            return False
    except (TypeError, ValueError):
        return False
    base = f"v0:{timestamp}:".encode() + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


class SlackNotifier:
    def __init__(self, token: str, channel: str, log_path: Path, http: httpx.Client | None = None, fail: bool = False):
        self.token, self.channel, self.log_path, self.fail = token, channel, log_path, fail
        self.http = http or httpx.Client(timeout=10)

    def post(self, text: str, blocks: list[dict] | None = None) -> str:
        if self.fail:
            raise ConnectionError("Slack unreachable")
        if not self.token:   # mock mode
            with self.log_path.open("a") as f:
                f.write(json.dumps({"channel": self.channel, "text": text, "blocks": blocks or []}) + "\n")
            return f"mock-{int(time.time() * 1000)}"

        def call():
            r = self.http.post("https://slack.com/api/chat.postMessage", headers={"Authorization": f"Bearer {self.token}"},
                               json={"channel": self.channel, "text": text, "blocks": blocks or []})
            data = check(r, "slack").json()
            if not data.get("ok"):
                raise RuntimeError(f"slack error: {data.get('error')}")
            return data["ts"]

        return with_retry(call, step="slack_post")
