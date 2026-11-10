"""
test_complaint_flow.py - tests scenario 3 (a customer complaint) end to end on your REAL database.

What it does:
  1. Creates a TEMPORARY completed Full Colour appointment with a service-log note that mentions a problem,
     and a rating-1 complaint about it. The database trigger turns the complaint into a complaint_received event.
  2. Hands that event to the Orchestrator (Quality Agent, then CX Agent).
  3. Prints the ticket, the proposals (reply = medium risk, refund = HIGH risk) and the decision_log row.
  4. APPROVES the refund as the manager (the ticket becomes resolved) and REJECTS the reply.
  5. Cleans up everything except the append-only decision_log rows.

IMPORTANT: stop your worker (python -m backend.worker) while this runs, or it may grab the event first.

Run it from the project root:   python -m backend.tests.test_complaint_flow
"""

import time

from backend.agents import orchestrator
from backend.approvals.approval_actions import approve, reject
from backend.db import supabase
from backend.scenarios.helpers import TestData, day_at, demo_customer_ids, find_complaint_event, ids_by_name

COMPLAINT = "Colour came out patchy and far too orange. Very disappointed and I would like a refund."


def heading(text):
    print("\n" + "=" * 72 + "\n" + text + "\n" + "=" * 72)


def main():
    data = TestData("complaint_test")
    try:
        heading("STEP 1: a complaint arrives")
        customer = demo_customer_ids()[0]
        appointment = data.temp_appointment(ids_by_name("staff", "Cleo Test"), ids_by_name("services", "Full Colour"),
                                            day_at(-20, 10), 120, customer, "completed")
        supabase.table("service_log").insert({
            "appointment_id": appointment["id"], "readiness_notes": "Colour bowl and brushes prepared.",
            "quality_notes": "Colour left on about 10 minutes over target."}).execute()

        started = time.time()
        feedback = supabase.table("feedback").insert({
            "customer_id": customer, "appointment_id": appointment["id"], "text": COMPLAINT, "rating": 1}).execute().data[0]
        data.feedback_ids.append(feedback["id"])
        event = find_complaint_event(feedback["id"])
        assert event, "the complaint_received event was not found or was taken by a running worker (stop the worker)"
        data.event_ids.append(event["id"])
        print(f"Feedback (rating 1) saved. The database trigger created event {event['id'][:8]} "
              f"(type={event['type']}, source={event['source']}), now claimed.")

        heading("STEP 2: Orchestrator -> Quality Agent -> CX Agent")
        summary = orchestrator.handle_event(event)
        seconds = time.time() - started
        supabase.table("events").update({"source": data.source}).eq("id", event["id"]).execute()   # label it as a test event
        print(f"Finished in {seconds:.1f} s. status={summary['status']}, routed to {summary['routed_to']}")
        for r in summary["results"]:
            print(f"   {r['label']}: {r['outcome']}")
        assert summary["status"] == "handled"

        fb = supabase.table("feedback").select("category, sentiment").eq("id", feedback["id"]).execute().data[0]
        ticket = supabase.table("tickets").select("*").eq("feedback_id", feedback["id"]).execute().data
        actions = supabase.table("agent_actions").select("*").eq("event_id", event["id"]).order("created_at").execute().data
        approvals = {a["agent_action_id"]: a for a in supabase.table("approvals").select("*")
                     .in_("agent_action_id", [a["id"] for a in actions]).execute().data}
        log = supabase.table("decision_log").select("*").eq("event_id", event["id"]).execute().data

        print(f"\nfeedback row : category={fb['category']!r}, sentiment={fb['sentiment']!r}")
        print(f"ticket       : status={ticket[0]['status']}, priority={ticket[0]['priority']}")
        for a in actions:
            ap = approvals.get(a["id"])
            print(f"agent_action : {a['agent_name']:<14} {a['tool_called']:<18} risk={a['risk_tier']:<6} status={a['status']:<18}"
                  f" approval={ap['status'] if ap else '-'}")
        reply = next(a for a in actions if a["tool_called"] == "draft_reply")
        refund = next(a for a in actions if a["tool_called"] == "suggest_refund")
        print(f"\ndraft reply (NOT sent): {reply['result']['draft_text']}")
        print(f"refund suggestion     : {refund['result']['suggested_amount']} ({refund['input']['percent']}% under the {refund['input']['basis']})")
        print(f"decision_log          : agents={log[0]['agents_involved']}, outcome={log[0]['outcome']!r}")
        print(f"   reasoning: {log[0]['reasoning']}")

        assert (fb["category"], fb["sentiment"]) == ("service quality", "negative")
        assert len(ticket) == 1 and ticket[0]["status"] == "open" and ticket[0]["priority"] == "high"
        assert reply["risk_tier"] == "medium" and reply["status"] == "awaiting_approval" and reply["result"]["sent"] is False
        assert refund["risk_tier"] == "high" and refund["status"] == "awaiting_approval" and refund["result"]["suggested_amount"] == 60.0
        assert any(a["tool_called"] == "flag_quality_issue" and a["risk_tier"] == "low" for a in actions)
        assert len(log) == 1 and log[0]["agents_involved"] == ["cx_agent", "quality_agent"]
        print("\nCHECK: ticket open, reply and refund waiting for approval, nothing sent.")

        heading("STEP 3: the manager approves the REFUND and rejects the REPLY")
        manager = supabase.table("profiles").select("id").eq("role", "manager").limit(1).execute().data[0]["id"]
        changes = approve(approvals[refund["id"]]["id"], manager)
        print(f"approve(refund): approval={changes['approval']['status']}, action={changes['agent_action']['status']}, "
              f"ticket={changes['ticket']['status']}, log={changes['decision_log']['outcome']!r}")
        print(f"   ticket notes: {changes['ticket']['resolution_notes']}")
        assert changes["ticket"]["status"] == "resolved" and changes["agent_action"]["status"] == "executed"
        changes = reject(approvals[reply["id"]]["id"], manager, "Tone is too formal, I will call the customer")
        print(f"reject(reply)  : approval={changes['approval']['status']}, action={changes['agent_action']['status']}, "
              f"ticket still {changes['ticket']['status']!r}, log={changes['decision_log']['outcome']!r}")
        assert changes["agent_action"]["status"] == "rejected" and changes["ticket"]["status"] == "resolved"
        print("\nALL CHECKS PASSED")
    finally:
        heading("CLEANUP")
        for what, count in data.cleanup().items():
            print(f"  {what}: {count}")
        print("  The decision_log rows stay (append-only). Your other test rows were not touched.")


if __name__ == "__main__":
    main()
