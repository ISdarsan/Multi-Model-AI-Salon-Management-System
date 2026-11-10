"""
test_audit_queue_flow.py - tests scenario 4 (shelf audit) and scenario 5 (waiting area overloaded) end to end
on your REAL database, with temporary test data.

What it does:
  A. Sends a shelf_observation saying the shelf has 0 cleansers (SKN-001) while the database says some exist.
     Checks an audit flag is proposed, the stock is NOT changed, and approving it only marks the flag resolved.
  B. Creates 3 temporary booked appointments far in the future (2 with customers, 1 walk-in), then sends a
     queue_overload event. Checks 3 shifts and 2 customer notices are proposed, nothing changes until approval,
     approves one shift (the appointment moves) and rejects another (it stays).
  C. Cleans up everything except the append-only decision_log rows.

IMPORTANT: stop your worker (python -m backend.worker) while this runs, or it may grab the events first.

Run it from the project root:   python -m backend.tests.test_audit_queue_flow
"""

import time
from datetime import timedelta

from backend.agents import orchestrator
from backend.agents.scheduling_agent import hhmm, parse_dt
from backend.approvals.approval_actions import approve, reject
from backend.db import supabase
from backend.scenarios.helpers import TestData, day_at, demo_customer_ids, ids_by_name
from backend.tools.inventory_tools import get_stock


def heading(text):
    print("\n" + "=" * 72 + "\n" + text + "\n" + "=" * 72)


def actions_of(event_id):
    actions = supabase.table("agent_actions").select("*").eq("event_id", event_id).order("created_at").execute().data
    approvals = {a["agent_action_id"]: a for a in supabase.table("approvals").select("*")
                 .in_("agent_action_id", [a["id"] for a in actions]).execute().data} if actions else {}
    return actions, approvals


def appointment(appointment_id):
    return supabase.table("appointments").select("*").eq("id", appointment_id).execute().data[0]


def main():
    data = TestData("audit_queue_test")
    manager = supabase.table("profiles").select("id").eq("role", "manager").limit(1).execute().data[0]["id"]
    try:
        # ------------------------------------------------------------------ A. shelf audit
        heading("A. SCENARIO 4: the shelf photo says empty, the database says stock exists")
        before = get_stock(sku="SKN-001")
        print(f"Database stock for SKN-001: {before['current_qty']}")
        event = data.new_event("shelf_observation", {
            "image_path": "test/shelf.jpg", "model": "yolo11n", "inference_ms": 150,
            "observations": [{"sku": "SKN-001", "observed_count": 0, "empty_slots": 3, "confidence": 0.9}]})
        started = time.time()
        summary = orchestrator.handle_event(event)
        print(f"Orchestrator finished in {time.time() - started:.1f} s: status={summary['status']}, routed to {summary['routed_to']}")
        for r in summary["results"]:
            print(f"   {r.get('sku') or r.get('label')}: {r['outcome']}")
        assert summary["status"] == "handled"

        actions, approvals = actions_of(event["id"])
        flags = [a for a in actions if a["tool_called"] == "raise_audit_flag"]
        assert len(flags) == 1, "expected exactly one audit flag"
        flag = flags[0]
        print(f"\nagent_action: {flag['agent_name']} {flag['tool_called']} risk={flag['risk_tier']} status={flag['status']} "
              f"approval={approvals[flag['id']]['status']}")
        print(f"   input: observed={flag['input']['observed_count']}, database={flag['input']['db_qty']}, difference={flag['input']['difference']}")
        assert flag["risk_tier"] == "medium" and flag["status"] == "awaiting_approval" and flag["result"]["stock_changed"] is False
        assert not [a for a in actions if a["tool_called"] == "create_draft_po"], "no purchase order should be created for a mismatch"
        assert get_stock(sku="SKN-001")["current_qty"] == before["current_qty"], "the stock changed before approval!"
        print("CHECK: audit flag waiting for approval, no purchase order, stock unchanged.")

        changes = approve(approvals[flag["id"]]["id"], manager)
        print(f"approve(): approval={changes['approval']['status']}, action={changes['agent_action']['status']}, log={changes['decision_log']['outcome']!r}")
        assert changes["agent_action"]["status"] == "executed"
        assert get_stock(sku="SKN-001")["current_qty"] == before["current_qty"], "approving the flag must NOT change the stock"
        print("CHECK: approving only resolved the flag. The stock is still", before["current_qty"])

        # ------------------------------------------------------------------ B. queue overload
        heading("B. SCENARIO 5: the waiting area is overloaded")
        day = 10
        while day_at(day, 10).isoweekday() == 7:      # skip Sunday (the salon is closed)
            day += 1
        customers = demo_customer_ids()
        service = ids_by_name("services", "Men's Cut")
        slots = [("Aria Demo", 10, 0, customers[0]), ("Ben Sample", 10, 30, customers[1]), ("Dev Mock", 11, 0, None)]
        temp = []
        for name, hour, minute, customer in slots:
            temp.append(data.temp_appointment(ids_by_name("staff", name), service, day_at(day, hour, minute), 30, customer))
        print(f"Created 3 TEMPORARY Men's Cut bookings on {day_at(day, 10):%A %Y-%m-%d}: 10:00 Aria (customer), "
              "10:30 Ben (customer), 11:00 Dev (walk-in)")
        nxt = supabase.table("appointments").select("id").eq("status", "booked").gte("start_time", day_at(day, 9).isoformat()) \
            .order("start_time").order("id").limit(3).execute().data
        assert [r["id"] for r in nxt] == [t["id"] for t in temp], "other appointments exist that day, so the test data is not the next 3"

        event = data.new_event("queue_overload", {"waiting_count": 8, "capacity": 4, "avg_wait_minutes": 25,
                                                  "time": day_at(day, 9).isoformat()})
        started = time.time()
        summary = orchestrator.handle_event(event)
        print(f"\nOrchestrator finished in {time.time() - started:.1f} s: status={summary['status']}, routed to {summary['routed_to']}")
        for r in summary["results"]:
            print(f"   {r['label']}: {r['outcome']}")
        assert summary["status"] == "handled"

        actions, approvals = actions_of(event["id"])
        shifts = [a for a in actions if a["tool_called"] == "shift_appointment"]
        notices = [a for a in actions if a["tool_called"] == "draft_customer_notice"]
        print(f"\n{len(shifts)} shift proposals (scheduling_agent) and {len(notices)} notices (cx_agent):")
        for a in shifts:
            i = a["input"]
            print(f"   shift : {i['staff_name']:<11} {hhmm(parse_dt(i['old_start']))} -> {hhmm(parse_dt(i['new_start']))} "
                  f"(+{i['minutes']} min) risk={a['risk_tier']} approval={approvals[a['id']]['status']}")
        for a in notices:
            print(f"   notice: customer {a['input']['customer_id'][:8]}... risk={a['risk_tier']} sent={a['result']['sent']}")
            print(f"           draft: {a['result']['draft_text']}")
        assert len(shifts) == 3 and all(a["risk_tier"] == "medium" and a["status"] == "awaiting_approval" and a["input"]["minutes"] == 30 for a in shifts)
        assert len(notices) == 2 and all(a["risk_tier"] == "medium" and a["result"]["sent"] is False for a in notices)
        assert {a["input"]["customer_id"] for a in notices} == {customers[0], customers[1]}, "the walk-in must get no notice"
        for t in temp:
            assert appointment(t["id"])["start_time"] == t["start_time"], "an appointment changed before approval!"
        print("CHECK: nothing changed yet. Walk-in got no notice. Nothing was sent.")

        for log in supabase.table("decision_log").select("*").eq("event_id", event["id"]).order("created_at").execute().data:
            print(f"   decision_log: {log['agents_involved']} -> {log['outcome']}")

        heading("The manager approves Aria's shift, rejects Ben's shift, approves one notice")
        aria = next(a for a in shifts if a["input"]["staff_name"] == "Aria Demo")
        ben = next(a for a in shifts if a["input"]["staff_name"] == "Ben Sample")
        changes = approve(approvals[aria["id"]]["id"], manager)
        moved = appointment(aria["input"]["appointment_id"])
        print(f"approve(shift Aria): appointment now {hhmm(parse_dt(moved['start_time']))}-{hhmm(parse_dt(moved['end_time']))} "
              f"(was 10:00-10:30), log={changes['decision_log']['outcome']!r}")
        assert parse_dt(moved["start_time"]) == parse_dt(aria["input"]["new_start"])
        changes = reject(approvals[ben["id"]]["id"], manager, "Ben's customer is already here")
        kept = appointment(ben["input"]["appointment_id"])
        print(f"reject(shift Ben) : appointment still {hhmm(parse_dt(kept['start_time']))}-{hhmm(parse_dt(kept['end_time']))}, log={changes['decision_log']['outcome']!r}")
        assert parse_dt(kept["start_time"]) == parse_dt(ben["input"]["old_start"])
        changes = approve(approvals[notices[0]["id"]]["id"], manager)
        print(f"approve(notice)   : action={changes['agent_action']['status']}, sent={changes['agent_action']['result']['sent']}, log={changes['decision_log']['outcome']!r}")
        assert changes["agent_action"]["result"]["sent"] is False
        print("\nALL CHECKS PASSED")
    finally:
        heading("CLEANUP")
        for what, count in data.cleanup().items():
            print(f"  {what}: {count}")
        print("  The decision_log rows stay (append-only). Your other test rows were not touched.")


if __name__ == "__main__":
    main()
