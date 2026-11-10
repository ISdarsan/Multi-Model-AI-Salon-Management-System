"""
test_cx_quality.py - checks the CX Agent and the Quality Agent OFFLINE with fake data.
It never touches the database. (Scenario 3: a customer complaint.)

Run it from the project root:   python -m backend.tests.test_cx_quality
"""

import os
from unittest.mock import MagicMock

import backend.agents.cx_agent as cx
import backend.agents.orchestrator as orch
import backend.agents.quality_agent as quality
import backend.proposals as proposals

os.environ["OLLAMA_MODEL"] = ""   # no AI call in these tests: the template sentence is used

# Real wording from the seed data, so the rules are checked on realistic text.
POLICIES = [
    {"title": "Cancellation and Rescheduling", "body": "Clients may cancel or reschedule free of charge up to 24 hours before the appointment."},
    {"title": "Delay Compensation", "body": "If a client waits more than 15 minutes beyond their booked time, they are offered a complimentary drink and an apology. For waits over 30 minutes, offer 10% off the current service."},
    {"title": "Escalation Rules", "body": "Complaints with a rating of 1 or 2 open a ticket. Complaints about colour results are checked against the service log before a reply is drafted."},
    {"title": "Patch Test and Allergy Policy", "body": "A patch test is required at least 48 hours before the first colour service for any new client. If a reaction is reported after a service, escalate to the manager."},
    {"title": "Refund Policy", "body": "A refund or re-do is offered when a service result clearly differs from what was agreed at consultation. Refunds up to 50% of the service price may be suggested by staff."},
]
COLOUR_COMPLAINT = "Colour came out patchy and far too orange. Very disappointed and I would like a refund."
WAIT_COMPLAINT = "Waited nearly an hour and was never offered a drink or an apology."


def check(label, condition):
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    assert condition, label


def rules():
    print("== 1. classification (keyword rules) ==")
    for text, expected in [
        (COLOUR_COMPLAINT, "service quality"),
        ("Waited 35 minutes in the waiting area with no update. Not good.", "wait time"),
        ("Nice result but a bit pricey for a trim.", "price"),   # 'price' and 'pricey' tie with 'result' and 'trim'; price is listed first
        ("It is far too expensive and the cost is not fair, I was overcharged", "price"),
        ("The shampoo gave me an itchy scalp and a reaction", "product"),
        ("The stylist was rude and ignored me", "staff behaviour"),
        ("Appointment was cancelled on me with no notice. Terrible.", "staff behaviour"),
        ("Basin was still wet and hair clippings on the chair when I sat down.", "service quality"),
        ("zzz qqq", "other"),
    ]:
        check(f"'{text[:48]}...' -> {expected}", cx.classify_complaint(text) == expected)
    check("sentiment: 1,2 negative / 3 neutral / 4,5 positive",
          [cx.sentiment_from_rating(r) for r in (1, 2, 3, 4, 5)] == ["negative", "negative", "neutral", "positive", "positive"])
    check("ticket priority: rating 1 high, 2 medium, 3+ low",
          [cx.priority_from_rating(r) for r in (1, 2, 3, 5)] == ["high", "medium", "low", "low"])

    print("\n== 2. policy matching (simple keywords) ==")
    names = [p["title"] for p in cx.match_policies(COLOUR_COMPLAINT, "service quality", POLICIES)]
    print("   colour complaint ->", names)
    check("colour complaint: Refund Policy is the best match", names[0] == "Refund Policy" and len(names) <= 2)
    names = [p["title"] for p in cx.match_policies(WAIT_COMPLAINT, "wait time", POLICIES)]
    print("   wait complaint   ->", names)
    check("wait complaint: Delay Compensation is the best match", names[0] == "Delay Compensation")
    check("nothing matches nothing", cx.match_policies("zzz", "other", [{"title": "Colour", "body": "hair dye"}]) == [])

    print("\n== 3. the Quality Agent's keyword rules on real service-log notes ==")
    for note, flagged in [
        ("Colour left on about 10 minutes over target.", True),
        ("Slight unevenness at the back, corrected before leaving.", True),
        ("Tools sterilised; minor delay finding clips.", True),
        ("Finished on time, no issues.", False),
        ("Client happy with result.", False),
        ("Workstation clean, tools sterilised.", False),
        ("Good result, client rebooked.", False),
        (None, False),
    ]:
        check(f"'{note}' -> {'problem' if flagged else 'fine'}", bool(quality.find_problems([note])) == flagged)
    check("'not sterilised' IS a problem", bool(quality.find_problems(["Tools were not sterilised"])))

    print("\n== 4. the refund rule (never above the policy limit) ==")
    r = cx.decide_refund(1, False, "service quality", 120.0)
    check("rating 1 -> 50% refund of 120 = 60.00 (Refund Policy)", r["amount"] == 60.0 and r["percent"] == 50 and r["basis"] == "Refund Policy")
    r = cx.decide_refund(2, True, "service quality", 80)
    check("rating 2 + Quality flagged -> 50% of 80 = 40.00", r["amount"] == 40.0 and "Quality Agent" in r["reasons"][0])
    check("rating 2 and no problem found -> NO refund", cx.decide_refund(2, False, "service quality", 80) is None)
    r = cx.decide_refund(1, False, "wait time", 50)
    check("wait time -> only 10% compensation (Delay Compensation): 5.00", r["amount"] == 5.0 and r["basis"] == "Delay Compensation")
    check("price unknown (no appointment) -> no amount can be suggested", cx.decide_refund(1, True, "service quality", None) is None)
    check("the amount never exceeds the policy limit",
          all(cx.decide_refund(1, True, c, p)["percent"] <= cx.REFUND_LIMIT_PERCENT for c in ("wait time", "price", "other") for p in (10, 99.99)))


# ----------------------------------------------------------------------------
# Fakes for the full-flow checks
# ----------------------------------------------------------------------------

state = {}
logs = []
fake_db = MagicMock()
fake_db.table.return_value.insert.return_value.execute.return_value.data = [{"id": "row-1"}]


def setup(rating=1, text=COLOUR_COMPLAINT, appointment=True, service_log=None, existing_ticket=None, price=120.0):
    """Prepare the fake database for one complaint."""
    logs.clear(); fake_db.reset_mock()
    fake_db.table.return_value.insert.return_value.execute.return_value.data = [{"id": "row-1"}]
    state.update(opened=[], classified=[])
    feedback = {"id": "fb-1", "rating": rating, "text": text, "appointment_id": "ap-1" if appointment else None, "customer_id": None}
    cx.get_feedback = lambda fid: feedback if fid == "fb-1" else None
    cx.get_policies = lambda: POLICIES
    cx.get_ticket_for_feedback = lambda fid: existing_ticket
    cx.open_ticket = lambda fid, priority: state["opened"].append(priority) or {"id": "tk-1", "status": "open", "priority": priority}
    cx.set_classification = lambda fid, category, sentiment: state["classified"].append((category, sentiment))
    cx.get_appointment_details = lambda aid: {"services": {"name": "Full Colour", "price": price}, "staff": {"name": "Cleo Test"}}
    cx.add_decision_log = lambda eid, agents, reasoning, outcome: logs.append({"agents": agents, "reasoning": reasoning, "outcome": outcome})
    proposals.supabase = fake_db
    proposals.add_decision_log = cx.add_decision_log
    quality.record_executed = proposals.record_executed
    quality.get_service_log = lambda aid: service_log if service_log is not None else []
    return {"id": "ev-1", "type": "complaint_received", "payload": {"feedback_id": "fb-1", "appointment_id": "ap-1" if appointment else None, "rating": rating}}


def inserted(tool):
    return [c.args[0] for c in fake_db.table.return_value.insert.call_args_list if c.args[0].get("tool_called") == tool]


BAD_LOG = [{"readiness_notes": "Colour bowl and brushes prepared.", "quality_notes": "Colour left on about 10 minutes over target."}]
GOOD_LOG = [{"readiness_notes": "Workstation clean.", "quality_notes": "Finished on time, no issues."}]


def flows():
    print("\n== 5. the Quality Agent alone ==")
    event = setup(service_log=BAD_LOG)
    context = {}
    summary = quality.handle_event(event, context)
    check("a problem in the log is flagged (low risk, executed, nothing to approve)",
          context["quality"]["flagged"] and len(inserted("flag_quality_issue")) == 1
          and inserted("flag_quality_issue")[0]["risk_tier"] == "low" and inserted("flag_quality_issue")[0]["status"] == "executed")
    check("the Quality Agent makes no proposal and writes no decision_log row", fake_db.table.return_value.insert.call_count == 1 and logs == [])
    setup(service_log=GOOD_LOG); context = {}
    quality.handle_event(event, context)
    check("a clean log is not flagged and nothing is written", not context["quality"]["flagged"] and fake_db.table.return_value.insert.call_count == 0)
    event2 = setup(appointment=False); context = {}
    quality.handle_event(event2, context)
    check("a complaint with no appointment: nothing to check, no flag", not context["quality"]["checked"] and not context["quality"]["flagged"])

    print("\n== 6. the CX Agent: a justified complaint (rating 1, problem in the log) ==")
    event = setup(rating=1, service_log=BAD_LOG); context = {}
    quality.handle_event(event, context)
    summary = cx.handle_event(event, context)
    print("   outcomes:", [r["outcome"] for r in summary["results"]])
    check("complaint classified and saved on the feedback row (service quality, negative)", state["classified"] == [("service quality", "negative")])
    check("a HIGH-priority ticket was opened (rating 1)", state["opened"] == ["high"])
    check("classify_feedback and open_ticket were logged as low-risk executed actions",
          len(inserted("classify_feedback")) == 1 and len(inserted("open_ticket")) == 1
          and all(a["risk_tier"] == "low" and a["status"] == "executed" for a in inserted("classify_feedback") + inserted("open_ticket")))
    reply = inserted("draft_reply")
    check("draft_reply: MEDIUM risk, awaiting_approval, NOT sent", len(reply) == 1 and reply[0]["risk_tier"] == "medium"
          and reply[0]["status"] == "awaiting_approval" and reply[0]["result"]["sent"] is False and reply[0]["result"]["draft_text"])
    refund = inserted("suggest_refund")
    check("suggest_refund: HIGH risk, awaiting_approval, 60.00 = 50% of 120", len(refund) == 1 and refund[0]["risk_tier"] == "high"
          and refund[0]["status"] == "awaiting_approval" and refund[0]["result"]["suggested_amount"] == 60.0 and refund[0]["result"]["applied"] is False)
    approvals = [c.args[0] for c in fake_db.table.return_value.insert.call_args_list if "agent_action_id" in c.args[0]]
    check("one 'pending' approval per proposal (reply + refund)", len(approvals) == 2 and all(a["status"] == "pending" for a in approvals))
    check("ONE decision_log row, agents_involved ['cx_agent','quality_agent']", len(logs) == 1 and logs[0]["agents"] == ["cx_agent", "quality_agent"]
          and logs[0]["outcome"] == cx.COMPLAINT_OUTCOME)
    print("   reasoning:", logs[0]["reasoning"][:260], "...")
    reasoning = logs[0]["reasoning"]
    check("the reasoning has the real details (rating, category, service, stylist, log note, policy, amount)",
          all(x in reasoning for x in ["rating 1", "service quality", "Full Colour", "Cleo Test", "over target", "Refund Policy", "60.00"]))
    check("it says nothing was sent to the customer", "Nothing was sent to the customer" in reasoning)

    print("\n== 7. no refund when the complaint is not justified ==")
    event = setup(rating=2, service_log=GOOD_LOG); context = {}
    quality.handle_event(event, context); cx.handle_event(event, context)
    check("rating 2 and a clean log -> reply drafted but NO refund", len(inserted("draft_reply")) == 1 and inserted("suggest_refund") == []
          and "does not look justified" in logs[0]["reasoning"])
    check("rating 2 -> medium-priority ticket", state["opened"] == ["medium"])
    event = setup(rating=2, service_log=BAD_LOG); context = {}
    quality.handle_event(event, context); cx.handle_event(event, context)
    check("rating 2 BUT the log shows a problem -> refund suggested", len(inserted("suggest_refund")) == 1)
    event = setup(rating=1, appointment=False); context = {}
    quality.handle_event(event, context); cx.handle_event(event, context)
    check("no appointment (price unknown) -> no refund, and the log says why", inserted("suggest_refund") == [] and "price is unknown" in logs[0]["reasoning"])
    event = setup(rating=1, text=WAIT_COMPLAINT, service_log=GOOD_LOG, price=50.0); context = {}
    quality.handle_event(event, context); cx.handle_event(event, context)
    check("wait-time complaint -> compensation of only 10% (5.00)", inserted("suggest_refund")[0]["result"]["suggested_amount"] == 5.0)

    print("\n== 8. other behaviour ==")
    event = setup(existing_ticket={"id": "tk-9", "status": "open", "priority": "high"}); context = {}
    cx.handle_event(event, context)
    check("an existing ticket is reused, not duplicated", state["opened"] == [] and inserted("open_ticket") == [] and "reused" in logs[0]["reasoning"])
    event = setup(); event["payload"]["feedback_id"] = "nope"
    try:
        cx.handle_event(event, {}); raised = False
    except ValueError as error:
        raised = "feedback not found" in str(error)
    check("a complaint whose feedback does not exist fails clearly", raised)
    check("the CX Agent never sends anything: no 'send' tool exists, and drafts are marked sent = False",
          not hasattr(cx, "send_reply") and all(c.args[0].get("result", {}).get("sent", False) is False
          for c in fake_db.table.return_value.insert.call_args_list if c.args[0].get("tool_called") == "draft_reply"))


def routing():
    print("\n== 9. the Orchestrator runs the Quality Agent THEN the CX Agent, sharing what was found ==")
    event = setup(rating=1, service_log=BAD_LOG)
    orch.supabase = fake_db
    orch.add_decision_log = cx.add_decision_log
    order = []
    real_quality, real_cx = quality.handle_event, cx.handle_event
    orch.AGENTS = {"quality_agent": lambda e, context: order.append("quality") or real_quality(e, context),
                   "cx_agent": lambda e, context: order.append("cx") or real_cx(e, context)}
    summary = orch.handle_event(event)
    check("order: quality, then cx", order == ["quality", "cx"] and summary["routed_to"] == "quality_agent + cx_agent")
    check("the CX Agent used the Quality Agent's finding: a refund was proposed", len(inserted("suggest_refund")) == 1)
    check("event handled, results from both agents", summary["status"] == "handled" and len(summary["results"]) >= 4)
    check("the worker would pick this type up (both agents are registered)", orch.is_routed(event) is True)

    def crashing(e, context): raise RuntimeError("cx exploded")
    orch.AGENTS = {"quality_agent": real_quality, "cx_agent": crashing}
    setup(rating=1, service_log=BAD_LOG); orch.add_decision_log = cx.add_decision_log
    summary = orch.handle_event(event)
    check("a crash in the CX Agent -> event failed and the reason logged, the worker would carry on",
          summary["status"] == "failed" and logs[-1]["outcome"] == "event failed" and "cx exploded" in logs[-1]["reasoning"]
          and logs[-1]["agents"] == ["orchestrator", "cx_agent"])


if __name__ == "__main__":
    rules()
    flows()
    routing()
    print("\nALL OFFLINE CX/QUALITY CHECKS PASSED (no database rows written)")
