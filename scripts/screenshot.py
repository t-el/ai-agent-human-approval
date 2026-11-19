"""Capture the approval UI with Playwright (headless) into docs/.

Starts its own server on a fresh data dir, feeds the sample inbox, approves one reply via the API, then shoots:
  docs/approval-queue.png         the queue with KPIs and guardrail flags
  docs/approval-blocked.png       a prompt-injection draft blocked by guardrails (approve disabled until edited)
  docs/approval-sent.png          an approved + sent reply with tool trace and hash-chained audit trail

    pip install playwright && playwright install chromium   # or set CHROME_PATH to a local Chrome
    python scripts/screenshot.py
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
PORT = os.getenv("SHOT_PORT", "8899")
BASE = f"http://127.0.0.1:{PORT}"


def call(path, body=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json"}, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read() or b"{}")


def main():
    data = ROOT / "data" / "screenshots"
    shutil.rmtree(data, ignore_errors=True)
    env = os.environ | {"DATA_DIR": str(data), "LOG_JSON": "false", "PUBLIC_URL": BASE, "OPENAI_API_KEY": "",
                        "APPROVER_USER": "taha", "APPROVER_PASSWORD": ""}
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "--factory", "agent_approval.web:create_app", "--port", PORT,
                            "--log-level", "warning"], cwd=ROOT, env=env)
    try:
        for _ in range(60):
            try:
                call("/health")
                break
            except OSError:
                time.sleep(0.25)
        ids = {}
        for m in json.loads((ROOT / "samples" / "inbound.json").read_text()):
            ids[m["subject"]] = call("/inbound", m)["run_id"]
        leo = ids["Where is my Desk Hub?"]
        urllib.request.urlopen(urllib.request.Request(f"{BASE}/approvals/{leo}/approve", data=b"reply=", method="POST"))
        with sync_playwright() as p:
            launch = {"headless": True}
            if os.getenv("CHROME_PATH"):
                launch["executable_path"] = os.environ["CHROME_PATH"]
            b = p.chromium.launch(**launch)
            page = b.new_page(viewport={"width": 1360, "height": 900}, device_scale_factor=1)
            page.goto(f"{BASE}/approvals")
            page.screenshot(path=str(DOCS / "approval-queue.png"), full_page=True)
            page.goto(f"{BASE}/approvals/{ids['urgent']}")
            page.screenshot(path=str(DOCS / "approval-blocked.png"), full_page=True)
            page.goto(f"{BASE}/approvals/{leo}")
            page.screenshot(path=str(DOCS / "approval-sent.png"), full_page=True)
            b.close()
        print("saved", *(DOCS / f for f in ("approval-queue.png", "approval-blocked.png", "approval-sent.png")))
    finally:
        srv.terminate()


if __name__ == "__main__":
    main()
