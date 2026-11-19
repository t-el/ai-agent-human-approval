import json

import pytest

from agent_approval.agent import AgentError, run_agent
from agent_approval.config import ROOT
from agent_approval.llm import MockLLM
from agent_approval.retry import TransientError
from agent_approval.tools import build_registry
from agent_approval.tools.crm import CrmTool
from agent_approval.tools.kb import KnowledgeBase
from agent_approval.tools.sheets import SheetsTool
from conftest import sample


def registry(crm_fail=False):
    return build_registry(CrmTool("mock", ROOT / "samples", fail=crm_fail), SheetsTool("mock", ROOT / "samples"),
                          KnowledgeBase(ROOT / "kb"))


def test_tool_loop_calls_crm_sheet_and_kb_then_drafts():
    events = []
    res = run_agent(sample(0), MockLLM(), registry(), on_event=lambda e, d: events.append(e), sleep=lambda s: None)
    assert [t.name for t in res.tool_results] == ["crm_lookup", "sheets_lookup_order", "kb_search"]
    assert res.draft["intent"] == "refund" and "ORD-10421" in res.draft["reply"]
    assert events.count("llm_call") == 2 and events.count("tool_call") == 3


def test_llm_outage_is_retried():
    llm = MockLLM(fail_times=2)
    res = run_agent(sample(2), llm, registry(), sleep=lambda s: None)
    assert res.draft["intent"] == "how_to" and llm.calls == 4


def test_llm_down_for_good_raises():
    with pytest.raises(TransientError):
        run_agent(sample(2), MockLLM(fail_times=10), registry(), attempts=3, sleep=lambda s: None)


def test_invalid_json_gets_one_repair_attempt():
    events = []
    res = run_agent(sample(2), MockLLM(bad_json_times=1), registry(), on_event=lambda e, d: events.append(e),
                    sleep=lambda s: None)
    assert "output_repair" in events and res.draft["reply"]
    with pytest.raises(AgentError, match="invalid output twice"):
        run_agent(sample(2), MockLLM(bad_json_times=2), registry(), sleep=lambda s: None)


def test_step_limit_stops_runaway_loops():
    class Loopy(MockLLM):
        def chat(self, messages, tools):
            return self._tool_calls([("kb_search", {"query": "x"})])
    with pytest.raises(AgentError):
        run_agent(sample(2), Loopy(), registry(), max_steps=3, sleep=lambda s: None)


def test_crm_outage_becomes_tool_error_not_crash():
    res = run_agent(sample(0), MockLLM(), registry(crm_fail=True), sleep=lambda s: None)
    crm = res.tool_results[0]
    assert not crm.ok and "unavailable" in crm.result["error"]
    assert "CRM was unavailable" in res.draft["internal_note"]


def test_registry_rejects_unknown_tools_and_bad_args():
    reg = registry()
    assert not reg.call("delete_customer", "{}").ok
    assert not reg.call("sheets_lookup_order", '{"order_id": "1; DROP TABLE"}').ok
    assert not reg.call("crm_lookup", "not json").ok


def test_kb_search_ranks_relevant_section_first():
    hits = KnowledgeBase(ROOT / "kb").search("how often do HubSpot contacts sync")["results"]
    assert hits[0]["id"] == "integrations#hubspot"
    assert json.dumps(hits)
