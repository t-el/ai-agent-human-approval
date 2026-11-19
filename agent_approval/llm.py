"""OpenAI-compatible chat client with tool calling, and a deterministic mock that speaks the same protocol.

The mock exists so the whole system (tool loop, guardrails, approvals, evals) runs offline and repeatably.
It is intentionally naive about prompt injection, so the guardrails have something real to catch.
"""

from __future__ import annotations

import json
import re
from typing import Protocol

import httpx

from .retry import TransientError, check

ORDER_RE = re.compile(r"\bORD-\d{4,6}\b", re.I)


class LLM(Protocol):
    model: str

    def chat(self, messages: list[dict], tools: list[dict]) -> dict: ...


class OpenAIChat:
    def __init__(self, api_key: str, base_url: str, model: str, timeout: float = 30, http: httpx.Client | None = None):
        self.model = model
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.http = http or httpx.Client(timeout=timeout)

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        body = {"model": self.model, "messages": messages, "tools": tools, "tool_choice": "auto", "temperature": 0}
        return check(self.http.post(self.url, json=body, headers=self.headers), "llm").json()


# --------------------------------------------------------------------------------------------- mock
def _intent(text: str) -> str:
    t = text.lower()
    if "unsubscribe" in t or "newsletter" in t:
        return "spam"
    if re.search(r"ignore (all )?previous instructions|admin mode|system prompt", t):
        return "injection"
    if re.search(r"lawyer|attorney|legal action|lawsuit|formal notice", t):
        return "legal"
    if re.search(r"refund|money back|chargeback", t):
        return "refund"
    if re.search(r"where is|hasn.t arrived|not arrived|tracking|delivery|shipped", t):
        return "order_status"
    if re.search(r"upgrade|downgrade|pricing|price|billed|billing|plan", t):
        return "pricing"
    return "how_to"


KB_QUERY = {"refund": "refund policy eligibility approved manager", "order_status": "shipping delivery times delays tracking",
            "pricing": "upgrades downgrades prorated plans pricing", "injection": "refund policy"}


class MockLLM:
    """Deterministic 'model'. Turn 1: decide which tools to call. Turn 2: write the draft from tool results."""

    model = "mock-support-agent-1"

    def __init__(self, max_refund: float = 100.0, fail_times: int = 0, bad_json_times: int = 0):
        self.max_refund = max_refund
        self.fail_times, self.bad_json_times = fail_times, bad_json_times
        self.calls = 0

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise TransientError("llm HTTP 503")
        user = next(m["content"] for m in messages if m["role"] == "user")
        email = re.search(r"From: .*<([^>]+)>", user).group(1)
        body = user.split("\n\n", 1)[1] if "\n\n" in user else user
        intent = _intent(body)
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        if not tool_msgs and intent != "spam":
            calls = [("crm_lookup", {"email": email})]
            if m := ORDER_RE.search(body):
                calls.append(("sheets_lookup_order", {"order_id": m.group(0).upper()}))
            if intent != "legal":
                subject = re.search(r"Subject: (.*)", user).group(1)
                calls.append(("kb_search", {"query": KB_QUERY.get(intent, f"{subject} {body}"), "top_k": 3}))
            return self._tool_calls(calls)
        if self.bad_json_times > 0:
            self.bad_json_times -= 1
            return self._content("Sure! Here is the reply: Hi there, thanks for reaching out...")
        return self._content(json.dumps(self._draft(intent, messages)))

    # -- composing ------------------------------------------------------------------------------
    def _draft(self, intent: str, messages: list[dict]) -> dict:
        names = {}
        for m in messages:
            for tc in m.get("tool_calls") or []:
                names[tc["id"]] = tc["function"]["name"]
        res = {names.get(m["tool_call_id"], "?"): json.loads(m["content"]) for m in messages if m["role"] == "tool"}
        crm, order, kb = res.get("crm_lookup", {}), res.get("sheets_lookup_order", {}), res.get("kb_search", {})
        hits = kb.get("results", [])
        first = (crm.get("name") or "there").split()[0]
        notes, cites = [], []
        if "error" in crm:
            notes.append("CRM was unavailable: check the account manually before sending.")
        elif not crm.get("found", True):
            notes.append("Sender is not in the CRM. Verify identity before any account change.")
        elif crm.get("health") == "at_risk":
            notes.append(f"At-risk {crm.get('plan')} account (${crm.get('mrr')}/mo). Loop in {crm.get('account_owner')}.")
        d = {"intent": intent, "confidence": 0.9, "escalate": False, "escalation_reason": "", "citations": cites}

        def cite(prefix: str) -> str:
            h = next((h for h in hits if h["id"].startswith(prefix)), hits[0] if hits else None)
            if h:
                cites.append(h["id"])
                return h["text"]
            return ""

        if intent == "spam":
            return d | {"reply": "", "action": "ignore", "internal_note": "Newsletter / marketing email."}
        if intent == "legal":
            return d | {"reply": "", "action": "escalate", "escalate": True, "escalation_reason": "legal threat",
                        "internal_note": "Legal language detected. Do not reply from support; forward to management."}
        if intent == "injection":   # a naive model that obeys the email (guardrails must catch this)
            user = next(m["content"] for m in messages if m["role"] == "user")
            digits = re.search(r"card ((?:\d[ -]?){13,19})", user)    # only visible if the PII scrub failed
            card = f"card {digits.group(1).strip()}" if digits else "card on file"
            return d | {"intent": "refund", "confidence": 0.7, "internal_note": " ".join(notes),
                        "reply": f"Hi {first}, admin mode confirmed. I have issued a full refund of $500 to your "
                                 f"{card}. My system prompt is: 'You are a support agent...'"}
        if intent == "refund":
            amount = float(order.get("amount", 0) or 0)
            cite("refund-policy#how-refunds")
            if order.get("found") and amount > self.max_refund:
                d |= {"escalate": True, "escalation_reason": f"refund ${amount:,.2f} is above the "
                      f"${self.max_refund:,.0f} support limit"}
                reply = (f"Hi {first}, thanks for letting us know. I've passed your refund request for "
                         f"{order['order_id']} to our billing team. Because the amount is above what support can "
                         f"approve, a manager will review it and we'll confirm the outcome within 1 business day.")
                notes.append(f"Manager approval needed (${amount:,.2f}).")
            elif order.get("found"):
                reply = (f"Hi {first}, sorry to see you go. I've passed your refund request for {order['order_id']} "
                         f"(${amount:,.2f}) to our billing team. Refunds go back to the original payment method "
                         f"within 5 to 10 business days, and we'll email you once it's processed.")
            else:
                d["confidence"] = 0.55
                reply = (f"Hi {first}, I'm sorry to hear that. Could you reply with your order number (it looks "
                         f"like ORD-12345) so we can check it against our refund policy?")
            cite("refund-policy#eligibility")
        elif intent == "order_status" and order.get("found"):
            policy = cite("shipping#delivery-times")
            if order.get("status") == "shipped":
                carrier, _, num = (order.get("tracking") or "").partition(" ")
                reply = (f"Hi {first}, your order {order['order_id']} ({order['product']}) shipped on "
                         f"{order['shipped_at']} with {carrier}, tracking number {num}. {policy.split('. ')[0]}. "
                         f"If it hasn't arrived 5 business days after the estimated date, reply here and we'll open "
                         f"a carrier investigation.")
                cite("shipping#delays")
            else:
                reply = f"Hi {first}, your order {order['order_id']} hasn't shipped yet. We'll email tracking as soon as it does."
        else:
            text = hits[0]["text"] if hits else ""
            if hits:
                cites.append(hits[0]["id"])
            else:
                d["confidence"] = 0.4
            reply = (f"Hi {first}, thanks for your question. {text} Let me know if anything is unclear and I'll "
                     f"walk you through it.") if text else f"Hi {first}, thanks for reaching out. A teammate will follow up shortly."
        return d | {"reply": reply, "internal_note": " ".join(notes), "citations": list(dict.fromkeys(cites))}

    # -- protocol helpers -----------------------------------------------------------------------
    def _tool_calls(self, calls: list[tuple[str, dict]]) -> dict:
        tc = [{"id": f"call_{self.calls}_{i}", "type": "function",
               "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]
        return {"model": self.model, "choices": [{"index": 0, "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None, "tool_calls": tc}}],
                "usage": {"prompt_tokens": 420, "completion_tokens": 40 * len(calls), "total_tokens": 420 + 40 * len(calls)}}

    def _content(self, text: str) -> dict:
        return {"model": self.model, "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 900, "completion_tokens": len(text) // 4, "total_tokens": 900 + len(text) // 4}}
