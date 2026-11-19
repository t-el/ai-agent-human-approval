import pytest

from agent_approval.evals import run_evals
from agent_approval.sender import MockSender
from agent_approval.service import ApprovalError, build_service
from agent_approval.slack import SlackNotifier
from conftest import sample


def outbox(settings):
    p = settings.data_dir / "outbox.jsonl"
    return p.read_text().splitlines() if p.exists() else []


def test_draft_waits_for_approval_and_nothing_is_sent(svc, settings):
    run = svc.process(sample(0))
    assert run["status"] == "pending_approval" and outbox(settings) == []
    assert (settings.data_dir / "slack.jsonl").exists()            # approval request posted


def test_approve_sends_exactly_once(svc, settings):
    run = svc.process(sample(1))
    assert svc.approve(run["id"], "taha")["status"] == "sent"
    again = svc.approve(run["id"], "taha")                        # double-click / Slack retry
    assert again.get("already_sent") and len(outbox(settings)) == 1


def test_duplicate_inbound_is_ignored(svc):
    a = svc.process(sample(2))
    b = svc.process(sample(2))
    assert b["duplicate"] and b["id"] == a["id"] and len(svc.store.list_runs()) == 1


def test_blocked_draft_cannot_be_approved_as_is(svc):
    run = svc.process(sample(4))
    with pytest.raises(ApprovalError, match="blocking"):
        svc.approve(run["id"], "taha")


def test_edit_is_rechecked_by_guardrails(svc, settings):
    run = svc.process(sample(4))
    with pytest.raises(ApprovalError, match="REFUND_PROMISE"):
        svc.approve(run["id"], "taha", "Sure, I have issued your refund.")
    ok = svc.approve(run["id"], "taha", "Hi Tom, we can't make account changes by email. Please reply with your "
                                        "order number and we'll look into it.")
    assert ok["status"] == "sent" and "4111" not in outbox(settings)[0]


def test_raw_card_number_never_stored(svc):
    run = svc.process(sample(4))
    assert "4111" not in run["inbound"]["body"]
    assert all("4111" not in str(a["data"]) for a in svc.store.audit_for(run["id"]))


def test_legal_goes_to_human_without_calling_the_llm(svc):
    calls = svc.llm.calls
    run = svc.process(sample(5))
    assert run["status"] == "needs_human" and svc.llm.calls == calls


def test_llm_outage_routes_to_human_then_replay_recovers(settings):
    from agent_approval.llm import MockLLM
    svc = build_service(settings, llm=MockLLM(fail_times=3), sleep=lambda s: None)
    run = svc.process(sample(2))
    assert run["status"] == "needs_human"
    dlq = svc.store.dead_letters()[0]
    assert dlq["kind"] == "agent"
    assert svc.replay(dlq["id"])["status"] == "pending_approval"
    assert svc.store.dead_letters() == []


def test_send_failure_dead_letters_then_replay_sends_once(settings):
    svc = build_service(settings, sender=MockSender(settings.data_dir / "outbox.jsonl", fail_times=5),
                        sleep=lambda s: None)
    run = svc.process(sample(1))
    assert svc.approve(run["id"], "taha")["status"] == "send_failed"
    svc.sender.fail_times = 0
    dlq = svc.store.dead_letters()[0]
    assert svc.replay(dlq["id"])["status"] == "sent"
    assert len(outbox(settings)) == 1


def test_slack_down_does_not_lose_the_draft(settings):
    svc = build_service(settings, slack=SlackNotifier("", "#x", settings.data_dir / "s.jsonl", fail=True),
                        sleep=lambda s: None)
    run = svc.process(sample(0))
    assert run["status"] == "pending_approval"
    assert "slack_post_failed" in [a["event"] for a in svc.store.audit_for(run["id"])]


def test_reject_records_actor(svc):
    run = svc.process(sample(7))
    assert svc.reject(run["id"], "salma")["actor"] == "salma"
    with pytest.raises(ApprovalError):
        svc.approve(run["id"], "salma")


def test_audit_chain_detects_tampering(svc):
    run = svc.process(sample(0))
    svc.approve(run["id"], "taha")
    assert svc.store.verify_audit() == (True, None)
    svc.store.db.execute("UPDATE audit SET actor='someone-else' WHERE event='approved'")
    ok, row = svc.store.verify_audit()
    assert not ok and row is not None


def test_all_eval_cases_pass_with_mock_model(settings):
    results = run_evals(settings)
    failed = [(r.case_id, r.failures) for r in results if not r.passed]
    assert not failed and len(results) == 10
