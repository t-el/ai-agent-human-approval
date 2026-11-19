"""Guardrails around the model: what goes in, and what a human is allowed to approve.

Severity:
  info   shown on the approval page
  warn   shown prominently; approver should double-check
  block  the draft cannot be approved as written. The approver must edit it (edits are re-checked) or reject
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}\b")
INJECTION_RE = re.compile(r"ignore (all |any )?(previous|prior|above) instructions|you are now|admin mode|"
                          r"developer mode|system prompt|reveal your (prompt|instructions)", re.I)
LEGAL_RE = re.compile(r"\b(lawyer|attorney|legal action|lawsuit|sue you|formal notice|court)\b", re.I)
REFUND_PROMISE_RE = re.compile(r"\b(i|we)('ve| have)? (issued|processed|refunded|sent|approved)\b[^.]*\brefund|"
                               r"\brefund (has been|was|is) (issued|processed|approved|sent)", re.I)
MONEY_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{2})?)")
LEAK_RE = re.compile(r"system prompt|my instructions are|as an ai language model", re.I)


@dataclass
class Finding:
    severity: str   # info | warn | block
    code: str
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


def _luhn(num: str) -> bool:
    digits = [int(c) for c in num if c.isdigit()][::-1]
    total = sum(d if i % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2) for i, d in enumerate(digits))
    return len(digits) >= 13 and total % 10 == 0


def scrub_pii(text: str) -> tuple[str, list[Finding]]:
    """Remove card numbers and IBANs before text reaches the LLM, the logs or the audit trail."""
    findings = []

    def card(m):
        if _luhn(m.group(0)):
            findings.append(Finding("info", "PII_REDACTED", "A card number was removed before the AI saw the message"))
            raw = m.group(0)
            return "[CARD REDACTED]" + raw[len(raw.rstrip(" -")):]
        return m.group(0)

    text = CARD_RE.sub(card, text)
    if IBAN_RE.search(text):
        text = IBAN_RE.sub("[IBAN REDACTED]", text)
        findings.append(Finding("info", "PII_REDACTED", "An IBAN was removed before the AI saw the message"))
    return text, findings


def check_input(subject: str, body: str) -> list[Finding]:
    out = []
    if INJECTION_RE.search(body) or INJECTION_RE.search(subject):
        out.append(Finding("warn", "PROMPT_INJECTION", "The message tries to give the AI instructions. "
                                                       "Treat the draft with suspicion."))
    if LEGAL_RE.search(body):
        out.append(Finding("block", "LEGAL", "Legal language. Route to a manager; no AI reply."))
    return out


def check_output(reply: str, *, citations: list[str], retrieved_ids: list[str], context_text: str,
                 confidence: float, intent: str, max_len: int = 1200) -> list[Finding]:
    out: list[Finding] = []
    if not reply.strip():
        return [Finding("block", "EMPTY", "The draft is empty")]
    if REFUND_PROMISE_RE.search(reply):
        out.append(Finding("block", "REFUND_PROMISE", "The draft says a refund was issued. Only billing can do that"))
    if LEAK_RE.search(reply):
        out.append(Finding("block", "PROMPT_LEAK", "The draft talks about the AI's own instructions"))
    if (m := CARD_RE.search(reply)) and _luhn(m.group(0)) or "REDACTED]" in reply:
        out.append(Finding("block", "PII_ECHO", "The draft contains payment details"))
    # money in the context = "$x" in KB text, or amount-like fields from the CRM / orders sheet
    found = MONEY_RE.findall(context_text) + re.findall(
        r'"(?:amount|mrr|price|total|gross)":\s*"?(\d[\d,]*(?:\.\d+)?)', context_text)
    known = {float(n.replace(",", "")) for n in found}
    for amt in MONEY_RE.findall(reply):
        if float(amt.replace(",", "")) not in known:
            out.append(Finding("block", "UNVERIFIED_AMOUNT", f"${amt} does not appear in the CRM, orders sheet or "
                                                             f"knowledge base"))
    bad_cites = [c for c in citations if c not in retrieved_ids]
    if bad_cites:
        out.append(Finding("block", "FAKE_CITATION", f"Cited sources that were never retrieved: {bad_cites}"))
    if intent in {"how_to", "pricing", "refund", "order_status"} and not citations:
        out.append(Finding("warn", "NO_SOURCE", "No knowledge-base source backs this answer"))
    if confidence < 0.6:
        out.append(Finding("warn", "LOW_CONFIDENCE", f"Model confidence {confidence:.2f}"))
    if len(reply) > max_len:
        out.append(Finding("warn", "TOO_LONG", f"{len(reply)} characters"))
    return out


def blocking(findings: list[Finding] | list[dict]) -> list:
    return [f for f in findings if (f.severity if isinstance(f, Finding) else f["severity"]) == "block"]
