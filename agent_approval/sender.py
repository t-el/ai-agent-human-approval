"""Send the approved reply. Exactly once: the run id is the idempotency key."""

from __future__ import annotations

import json
import smtplib
import time
from email.message import EmailMessage
from pathlib import Path

from .retry import TransientError


class MockSender:
    """Writes to data/outbox.jsonl. `fail_times` simulates an SMTP outage."""

    def __init__(self, outbox: Path, fail_times: int = 0):
        self.outbox, self.fail_times = outbox, fail_times

    def send(self, to: str, subject: str, body: str, in_reply_to: str, key: str) -> str:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise TransientError("SMTP 451 temporary failure")
        mid = f"<{key}.{int(time.time())}@mock.outbox>"
        with self.outbox.open("a") as f:
            f.write(json.dumps({"to": to, "subject": subject, "body": body, "in_reply_to": in_reply_to,
                                "message_id": mid}) + "\n")
        return mid


class SmtpSender:
    def __init__(self, host: str, port: int, user: str, password: str, from_addr: str):
        self.host, self.port, self.user, self.password, self.from_addr = host, port, user, password, from_addr

    def send(self, to: str, subject: str, body: str, in_reply_to: str, key: str) -> str:
        msg = EmailMessage()
        msg["From"], msg["To"] = self.from_addr, to
        msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        msg["In-Reply-To"] = msg["References"] = in_reply_to
        msg["Message-ID"] = f"<{key}@{self.from_addr.split('@')[-1]}>"
        msg.set_content(body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=20) as s:
                s.starttls()
                s.login(self.user, self.password)
                s.send_message(msg)
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, TimeoutError) as exc:
            raise TransientError(str(exc)) from exc
        except smtplib.SMTPResponseException as exc:
            if 400 <= exc.smtp_code < 500:
                raise TransientError(f"SMTP {exc.smtp_code}") from exc
            raise
        return msg["Message-ID"]
