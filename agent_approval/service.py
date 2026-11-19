"""Orchestration: intake -> guardrails -> agent -> guardrails -> approval request -> (human) -> send.

Run statuses:
  received          stored, being processed
  ignored           spam / newsletter, nothing to do
  needs_human       legal, AI failure or loop: a person must write the reply (no AI draft is sent)
  pending_approval  draft ready, waiting for a human in Slack or the web page
  sent              approved and delivered (exactly once)
  rejected          a human said no
  send_failed       approved, but the email provider kept failing: in the dead-letter queue, replayable
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Callable

from .agent import run_agent
from .config import Settings
from .guardrails import Finding, blocking, check_input, check_output, scrub_pii
from .llm import LLM, MockLLM, OpenAIChat
from .retry import TransientError, with_retry
from .sender import MockSender, SmtpSender
from .slack import SlackNotifier, approval_blocks
from .store import Store
from .tools import ToolRegistry, build_registry
from .tools.crm import CrmTool
from .tools.kb import KnowledgeBase
from .tools.sheets import SheetsTool

log = logging.getLogger("agent.service")


class ApprovalError(Exception):
    pass


@dataclass
class Service:
    settings: Settings
    store: Store
    llm: LLM
    tools: ToolRegistry
    slack: SlackNotifier
    sender: object
    sleep: Callable[[float], None] = time.sleep

    # ------------------------------------------------------------------------------- intake
    def process(self, msg: dict, *, run_id: str | None = None) -> dict:
        if run_id is None:
            run_id = self.store.create_run(msg["message_id"], msg)
            if run_id is None:
                existing = self.store.run_by_message(msg["message_id"])
                log.info("duplicate inbound ignored", extra={"message_id": msg["message_id"], "run_id": existing["id"]})
                return existing | {"duplicate": True}
        ev = lambda e, d=None, actor="system": self.store.audit(run_id, e, d, actor)   # noqa: E731
        ev("received", {"from": msg["from_email"], "subject": msg.get("subject"), "channel": msg.get("channel", "email")})

        clean_body, pii = scrub_pii(msg["body"])
        safe_msg = msg | {"body": clean_body}
        findings: list[Finding] = pii + check_input(msg.get("subject", ""), clean_body)
        self.store.update_run(run_id, inbound=safe_msg)       # never store raw card numbers
        if findings:
            ev("input_guardrails", {"findings": [f.to_dict() for f in findings]})

        if any(f.code == "LEGAL" for f in findings):
            return self._needs_human(run_id, findings, "Legal language detected: routed to a manager, no AI draft")

        tool_log: list[dict] = []

        def on_event(event: str, data: dict) -> None:
            if event == "tool_call":
                tool_log.append(data)
            ev(event, data)

        try:
            res = run_agent(safe_msg, self.llm, self.tools, max_steps=self.settings.agent_max_steps,
                            attempts=self.settings.retry_attempts, base_delay=self.settings.retry_base_delay,
                            on_event=on_event, sleep=self.sleep)
        except Exception as exc:   # AgentError, LLM outage, anything: a human takes over
            dlq = self.store.dead_letter(run_id, "agent", str(exc))
            ev("agent_failed", {"error": str(exc), "dlq_id": dlq})
            self.store.update_run(run_id, tools=tool_log)
            return self._needs_human(run_id, findings, f"AI agent failed ({exc}); reply manually or replay #{dlq}")

        d = res.draft
        if d.get("action") == "ignore":
            self.store.update_run(run_id, status="ignored", draft=d, tools=tool_log, findings=[f.to_dict() for f in findings])
            ev("ignored", {"reason": d.get("internal_note")})
            return self.store.get_run(run_id)
        if d.get("action") == "escalate":
            self.store.update_run(run_id, draft=d, tools=tool_log)
            return self._needs_human(run_id, findings, d.get("escalation_reason") or "model escalated")

        retrieved = [r["id"] for t in res.tool_results if t.name == "kb_search" for r in t.result.get("results", [])]
        context = json.dumps([t.result for t in res.tool_results])
        out = check_output(d["reply"], citations=d.get("citations", []), retrieved_ids=retrieved, context_text=context,
                           confidence=d["confidence"], intent=d["intent"])
        if any(not t.ok for t in res.tool_results):
            out.append(Finding("warn", "TOOL_ERROR", "A tool failed: " +
                               ", ".join(t.name for t in res.tool_results if not t.ok)))
        if d.get("escalate"):
            out.append(Finding("warn", "MANAGER_APPROVAL", d.get("escalation_reason") or "escalation requested"))
        findings += out
        ev("draft_created", {"intent": d["intent"], "confidence": d["confidence"], "citations": d.get("citations"),
                             "tokens": res.tokens, "steps": res.steps, "model": res.model})
        ev("output_guardrails", {"findings": [f.to_dict() for f in out]})
        # retrieved ids + context are kept so an approver's edit can be re-checked against the same facts
        self.store.update_run(run_id, status="pending_approval", draft=d | {"retrieved": retrieved, "context": context},
                              tools=tool_log, findings=[f.to_dict() for f in findings])
        self._request_approval(run_id)
        return self.store.get_run(run_id)

    def _needs_human(self, run_id: str, findings: list[Finding], reason: str) -> dict:
        findings = findings + [Finding("block", "NEEDS_HUMAN", reason)]
        self.store.update_run(run_id, status="needs_human", findings=[f.to_dict() for f in findings])
        self.store.audit(run_id, "routed_to_human", {"reason": reason})
        run = self.store.get_run(run_id)
        try:
            self.slack.post(f":rotating_light: Needs a human: {run['inbound'].get('subject')} "
                            f"from {run['inbound']['from_email']}. {reason}. "
                            f"{self.settings.public_url}/approvals/{run_id}")
        except Exception as exc:
            self.store.audit(run_id, "slack_post_failed", {"error": str(exc)})
        return run

    def _request_approval(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        try:
            ts = self.slack.post(f"Reply needs approval: {run['inbound'].get('subject')}",
                                 approval_blocks(run, self.settings.public_url))
            self.store.audit(run_id, "approval_requested", {"slack_ts": ts, "channel": self.settings.slack_channel})
        except Exception as exc:   # the web queue still shows it; nothing is lost
            log.warning("slack post failed, draft still in web queue", extra={"run_id": run_id, "error": str(exc)})
            self.store.audit(run_id, "slack_post_failed", {"error": str(exc)})

    # ------------------------------------------------------------------------------- decisions
    def approve(self, run_id: str, actor: str, edited_reply: str | None = None) -> dict:
        run = self.store.get_run(run_id)
        if not run:
            raise ApprovalError("run not found")
        if run["status"] == "sent":
            return run | {"already_sent": True}          # double-click / Slack retry: no second email
        if run["status"] not in {"pending_approval", "send_failed"}:
            raise ApprovalError(f"cannot approve a run in status {run['status']}")
        d = run["draft"]
        reply = (edited_reply or "").strip() or d["reply"]
        if edited_reply and edited_reply.strip() != d["reply"]:
            new = check_output(reply, citations=d.get("citations", []), retrieved_ids=d.get("retrieved", []),
                               context_text=d.get("context", ""), confidence=1.0, intent=d["intent"])
            if blocking(new):
                self.store.audit(run_id, "edit_rejected_by_guardrails", {"findings": [f.to_dict() for f in new]}, actor)
                raise ApprovalError("edited reply still fails guardrails: " + ", ".join(f.code for f in blocking(new)))
            self.store.audit(run_id, "edited", {"before": d["reply"], "after": reply}, actor)
        elif blocking(run["findings"]):
            raise ApprovalError("this draft has blocking guardrail findings; edit it or reject it")
        self.store.update_run(run_id, final_reply=reply, actor=actor)
        self.store.audit(run_id, "approved", {"edited": reply != d["reply"]}, actor)
        return self._send(run_id)

    def reject(self, run_id: str, actor: str, reason: str = "") -> dict:
        run = self.store.get_run(run_id)
        if not run or run["status"] not in {"pending_approval", "needs_human", "send_failed"}:
            raise ApprovalError("nothing to reject")
        self.store.update_run(run_id, status="rejected", actor=actor)
        self.store.audit(run_id, "rejected", {"reason": reason}, actor)
        return self.store.get_run(run_id)

    def _send(self, run_id: str) -> dict:
        run = self.store.get_run(run_id)
        key = run_id
        if self.store.already_sent(key):
            self.store.update_run(run_id, status="sent")
            return self.store.get_run(run_id)
        msg = run["inbound"]
        try:
            mid = with_retry(lambda: self.sender.send(msg["from_email"], msg.get("subject", ""), run["final_reply"],
                                                      msg["message_id"], key),
                             step="send_email", attempts=self.settings.retry_attempts,
                             base_delay=self.settings.retry_base_delay, sleep=self.sleep)
        except Exception as exc:
            dlq = self.store.dead_letter(run_id, "send", str(exc))
            self.store.update_run(run_id, status="send_failed")
            self.store.audit(run_id, "send_failed", {"error": str(exc), "dlq_id": dlq})
            return self.store.get_run(run_id)
        self.store.mark_sent(key, mid)
        self.store.update_run(run_id, status="sent")
        self.store.audit(run_id, "sent", {"to": msg["from_email"], "provider_message_id": mid})
        log.info("reply sent", extra={"run_id": run_id, "to": msg["from_email"]})
        return self.store.get_run(run_id)

    # ------------------------------------------------------------------------------- recovery
    def replay(self, dlq_id: int) -> dict:
        item = next((d for d in self.store.dead_letters() if d["id"] == dlq_id), None)
        if not item:
            raise ValueError(f"dead letter {dlq_id} not open")
        run = self.store.get_run(item["run_id"])
        if item["kind"] == "send":
            out = self._send(run["id"])
        else:
            self.store.update_run(run["id"], status="received")
            self.store.audit(run["id"], "replayed", {"dlq_id": dlq_id})
            out = self.process(run["inbound"], run_id=run["id"])
        if out["status"] in {"sent", "pending_approval", "ignored"}:
            self.store.resolve_dead_letter(dlq_id)
        return out


def build_service(settings: Settings, *, llm: LLM | None = None, crm: CrmTool | None = None,
                  sender=None, slack: SlackNotifier | None = None, sleep: Callable[[float], None] = time.sleep) -> Service:
    if llm is None:
        llm = MockLLM(settings.max_refund_without_manager) if settings.mock_llm else \
            OpenAIChat(settings.openai_api_key, settings.openai_base_url, settings.openai_model, settings.llm_timeout)
    crm = crm or CrmTool(settings.crm_provider, settings.samples_dir, settings.hubspot_token)
    sheets = SheetsTool(settings.sheets_provider, settings.samples_dir, settings.google_sheets_id,
                        settings.google_sheets_range, settings.google_api_key)
    tools = build_registry(crm, sheets, KnowledgeBase(settings.kb_dir))
    if sender is None:
        sender = MockSender(settings.data_dir / "outbox.jsonl") if settings.sender == "mock" else \
            SmtpSender(settings.smtp_host, settings.smtp_port, settings.smtp_user, settings.smtp_password,
                       settings.from_address)
    slack = slack or SlackNotifier(settings.slack_bot_token, settings.slack_channel, settings.data_dir / "slack.jsonl")
    return Service(settings, Store(settings.db_path), llm, tools, slack, sender, sleep)
