# Handoff: AI Support Agent with Human Approval

## What it does
Drafts replies to inbound customer emails using the CRM, the orders sheet and the knowledge base. **It never sends anything on its own.** Every reply is approved by a person in Slack or on the web page, and every step is written to a tamper-evident audit log.

## Where things live

| Thing | Location |
|---|---|
| Config | `.env` (documented in `.env.example`) |
| Database | `data/agent.db`. Tables: `runs`, `audit` (hash-chained), `dead_letters`, `sent` (exactly-once ledger) |
| Mock Slack / mock outbox | `data/slack.jsonl`, `data/outbox.jsonl` |
| Approval UI | `https://<host>/approvals` (Basic auth with `APPROVER_USER` / `APPROVER_PASSWORD`) |
| Slack Interactivity URL | `https://<host>/slack/interactions` |
| Knowledge base | `kb/*.md`. Each `## ` heading is one searchable, citable section |
| Prompt | `SYSTEM_PROMPT` in `agent_approval/agent.py` |
| Evals | `evals/cases.json` |

## Run statuses

| Status | Meaning | Who acts |
|---|---|---|
| `pending_approval` | Draft ready | Approver: approve, edit or reject |
| `needs_human` | Legal language, AI failure or a loop. No AI reply exists | Support lead replies manually, then closes it |
| `sent` | Approved and delivered once | Nobody |
| `rejected` | Someone said no | Nobody (the customer still needs a manual reply) |
| `ignored` | Newsletter / spam | Nobody |
| `send_failed` | Approved, but the email provider kept failing | Fix SMTP, then `python -m agent_approval.cli replay <id>` |

## Guardrail codes approvers will see

| Code | Severity | What to do |
|---|---|---|
| `REFUND_PROMISE`, `UNVERIFIED_AMOUNT`, `FAKE_CITATION`, `PROMPT_LEAK`, `PII_ECHO` | block | Rewrite the reply in the text box. Your edit is re-checked |
| `PROMPT_INJECTION` | warn | The email tried to instruct the AI. Read the draft carefully |
| `MANAGER_APPROVAL` | warn | Refund above `MAX_REFUND_WITHOUT_MANAGER`. Get a manager's OK first |
| `TOOL_ERROR`, `NO_SOURCE`, `LOW_CONFIDENCE` | warn | Check the facts yourself before approving |

## Changing the agent safely
1. Edit the prompt, a tool or the KB.
2. Add an eval case for the behaviour you want in `evals/cases.json`.
3. Run `python -m agent_approval.cli eval` with the real `OPENAI_API_KEY`. Ship only at 10/10 (or better).

## Failure playbook

| Symptom | Action |
|---|---|
| Many `needs_human` with `llm HTTP 429/503` | Provider outage or quota. Check billing, then `replay` each dead letter |
| `slack_post_failed` in audit | Bot token revoked or channel renamed. Drafts are still in the web queue |
| `send_failed` | SMTP credentials or provider outage. Fix, then `replay` (it never double-sends) |
| `verify-audit` reports a broken chain | Someone edited the database. Restore `data/agent.db` from backup and investigate |

## Security
- Tools are read-only. To add a write action (e.g. "issue refund"), put it **after** approval in `service.py`, never in the tool list.
- Customer emails go to the LLM provider. Use an approved provider and region, or a local model via `OPENAI_BASE_URL`.
- `.env` and `data/` are git-ignored. Back up `data/agent.db`, since it holds the audit trail.
