"""Tools the agent may call. Each tool: an OpenAI function schema + a Python callable returning a dict.

Tools are read-only on purpose: the agent can look things up, but it cannot refund, delete or send.
Anything with side effects happens only after a human approves.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from .crm import CrmTool
from .kb import KnowledgeBase
from .sheets import SheetsTool

log = logging.getLogger("agent.tools")


@dataclass
class ToolResult:
    name: str
    args: dict
    result: dict
    ms: int
    ok: bool = True


@dataclass
class ToolRegistry:
    tools: dict[str, tuple[dict, Callable[..., dict]]] = field(default_factory=dict)

    def register(self, schema: dict, fn: Callable[..., dict]) -> None:
        self.tools[schema["name"]] = (schema, fn)

    def schemas(self) -> list[dict]:
        return [{"type": "function", "function": s} for s, _ in self.tools.values()]

    def call(self, name: str, raw_args: str | dict) -> ToolResult:
        t0 = time.monotonic()
        if name not in self.tools:                       # the model invented a tool: refuse, don't crash
            return ToolResult(name, {}, {"error": f"unknown tool {name!r}"}, 0, ok=False)
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except json.JSONDecodeError:
            return ToolResult(name, {}, {"error": "arguments were not valid JSON"}, 0, ok=False)
        try:
            result = self.tools[name][1](**args)
            ok = "error" not in result
        except TypeError as exc:
            result, ok = {"error": f"bad arguments: {exc}"}, False
        except Exception as exc:                          # a tool outage must not kill the run
            log.warning("tool failed", extra={"tool": name, "error": str(exc)})
            result, ok = {"error": f"{name} unavailable: {exc}"}, False
        return ToolResult(name, args, result, int((time.monotonic() - t0) * 1000), ok)


def build_registry(crm: CrmTool, sheets: SheetsTool, kb: KnowledgeBase) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register({"name": "crm_lookup", "description": "Look up a customer in the CRM by email: plan, MRR, "
                  "account owner, health, open tickets, notes.",
                  "parameters": {"type": "object", "properties": {"email": {"type": "string"}},
                                 "required": ["email"]}}, crm.lookup)
    reg.register({"name": "sheets_lookup_order", "description": "Find an order in the Orders Google Sheet by "
                  "order id (e.g. ORD-10421): product, amount, status, shipping date and tracking.",
                  "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}},
                                 "required": ["order_id"]}}, sheets.lookup_order)
    reg.register({"name": "kb_search", "description": "Search the help-center knowledge base. Returns the most "
                  "relevant sections with their ids. Cite the ids you use.",
                  "parameters": {"type": "object", "properties": {"query": {"type": "string"},
                                                                  "top_k": {"type": "integer", "default": 2}},
                                 "required": ["query"]}}, kb.search)
    return reg
