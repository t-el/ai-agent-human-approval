import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from agent_approval.web import create_app
from conftest import sample


@pytest.fixture
def client(svc):
    return TestClient(create_app(svc))


def test_inbound_creates_run_and_dedupes(client):
    r = client.post("/inbound", json=sample(0))
    assert r.status_code == 201 and r.json()["status"] == "pending_approval"
    assert client.post("/inbound", json=sample(0)).json()["duplicate"] is True


def test_inbound_validates_payload(client):
    assert client.post("/inbound", json={"message_id": "x1", "from_email": "nope", "body": "hi"}).status_code == 422


def test_queue_and_detail_pages_render(client):
    run_id = client.post("/inbound", json=sample(4)).json()["run_id"]
    assert "Approval queue" in client.get("/approvals").text
    page = client.get(f"/approvals/{run_id}").text
    assert "REFUND_PROMISE" in page and "Approve edited reply" in page and "audit chain verified" in page


def test_approve_via_form(client, svc):
    run_id = client.post("/inbound", json=sample(1)).json()["run_id"]
    r = client.post(f"/approvals/{run_id}/approve", data={"reply": svc.store.get_run(run_id)["draft"]["reply"]},
                    follow_redirects=False)
    assert r.status_code == 303 and svc.store.get_run(run_id)["status"] == "sent"


def signed(secret, body: bytes, ts=None):
    ts = str(ts or int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig,
            "Content-Type": "application/x-www-form-urlencoded"}


def test_slack_button_approves_with_valid_signature(client, svc):
    run_id = client.post("/inbound", json=sample(2)).json()["run_id"]
    payload = {"type": "block_actions", "user": {"username": "taha"},
               "actions": [{"action_id": "approve", "value": run_id}]}
    body = urlencode({"payload": json.dumps(payload)}).encode()
    r = client.post("/slack/interactions", content=body, headers=signed(svc.settings.slack_signing_secret, body))
    assert "Sent by slack:taha" in r.json()["text"] and svc.store.get_run(run_id)["actor"] == "slack:taha"


def test_slack_rejects_bad_or_replayed_signature(client, svc):
    body = urlencode({"payload": "{}"}).encode()
    assert client.post("/slack/interactions", content=body, headers=signed("wrong", body)).status_code == 401
    old = signed(svc.settings.slack_signing_secret, body, ts=int(time.time()) - 600)
    assert client.post("/slack/interactions", content=body, headers=old).status_code == 401


def test_tool_endpoint_for_n8n(client):
    r = client.post("/tools/sheets_lookup_order", json={"order_id": "ORD-10455"})
    assert r.status_code == 200 and r.json()["tracking"].startswith("DHL")


def test_basic_auth_when_password_set(svc):
    svc.settings.approver_user, svc.settings.approver_password = "taha", "s3cret"
    c = TestClient(create_app(svc))
    assert c.get("/approvals").status_code == 401
    assert c.get("/approvals", auth=("taha", "s3cret")).status_code == 200
