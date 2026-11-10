"""
test_orchestrator_agent.py - tests the Orchestrator end to end on your REAL database, with COL-6N.

What it does:
  1. Looks at the real bookings in the next 3 days that use COL-6N. If none of them would clash
     with the low stock, it creates TEMPORARY appointments (and removes them at the end).
  2. Sends a stock_low event for COL-6N through the Orchestrator.
  3. Prints the draft purchase order (Inventory Agent), the hold proposals (Orchestrator) and the log rows.
  4. APPROVES the first hold and shows the appointment becoming 'held'.
  5. Cleans up: puts every appointment back, deletes the orders, actions, approvals and temporary rows.

Run it from the project root:   python -m backend.tests.test_orchestrator_agent

About cleanup: decision_log is append-only, so the test event (source = 'orchestrator_test') and its
decision_log rows stay. Everything else is removed.
"""

from datetime import datetime, timedelta, timezone

from backend.agents import orchestrator
from backend.agents.scheduling_agent import hhmm, parse_dt
from backend.approvals.approval_actions import approve
from backend.db import supabase
from backend.tools.inventory_tools import get_open_purchase_order, get_stock
from backend.tools.orchestrator_tools import get_pending_hold_appointment_ids, get_upcoming_bookings

TEST_SOURCE = "orchestrator_test"
SKU = "COL-6N"

original_status = {}                       # appointment id -> the status it had at the start
temp_appointments = []                     # ids of appointments this test created
created = {"event": None}


def heading(text):
    print("\n" + "=" * 72 + "\n" + text + "\n" + "=" * 72)


def real_bookings(product_id):
    """The real 'booked' appointments inside the lookahead window that use this product."""
    now = datetime.now(timezone.utc)
    limit = now + timedelta(days=orchestrator.LOOKAHEAD_DAYS)
    return [b for b in get_upcoming_bookings(product_id, now.isoformat()) if parse_dt(b["start_time"]) < limit]


def create_temporary_appointments(count, product_id):
    """Create `count` temporary Full Colour bookings tomorrow evening (after the salon's shifts)."""
    staff = supabase.table("staff").select("id").eq("name", "Cleo Test").execute().data[0]["id"]
    service = supabase.table("services").select("id").eq("name", "Full Colour").execute().data[0]["id"]
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=19, minute=0, second=0, microsecond=0)
    for n in range(count):
        start = tomorrow + timedelta(hours=2.5 * n)
        row = supabase.table("appointments").insert({
            "staff_id": staff, "service_id": service, "status": "booked",
            "start_time": start.isoformat(), "end_time": (start + timedelta(hours=2)).isoformat(),
        }).execute().data[0]
        temp_appointments.append(row["id"])
        print(f"  created TEMPORARY Full Colour booking {row['id'][:8]} at {start:%a %Y-%m-%d %H:%M}")


def status_of(appointment_id):
    return supabase.table("appointments").select("status").eq("id", appointment_id).execute().data[0]["status"]


def main():
    stock = get_stock(sku=SKU)
    assert not get_open_purchase_order(stock["product_id"]), f"{SKU} already has an open purchase order. Approve or reject it first."
    assert not get_pending_hold_appointment_ids(), "there are already pending hold proposals. Approve or reject them first."
    print(f"{SKU}: stock now {stock['current_qty']}, reorder threshold {stock['reorder_threshold']}")

    try:
        heading("STEP 0: which real bookings use COL-6N in the next 3 days?")
        bookings = real_bookings(stock["product_id"])
        for b in bookings:
            original_status[b["id"]] = b["status"]
            print(f"  {parse_dt(b['start_time']):%a %Y-%m-%d} {hhmm(parse_dt(b['start_time']))}  {b['services']['name']:<14} needs {b['needs']}  status {b['status']}")
        if not bookings:
            print("  (none)")
        clashes = orchestrator.find_clashes(stock["current_qty"], bookings)
        print(f"  expected clashes with the real data: {len(clashes)}")
        if not clashes:
            # Real bookings do not use up the stock, so add temporary ones until one clashes.
            print("  No real clash, so creating temporary bookings (they are removed at the end):")
            create_temporary_appointments(2, stock["product_id"])
            bookings = real_bookings(stock["product_id"])
            clashes = orchestrator.find_clashes(stock["current_qty"], bookings)
            for b in bookings:
                original_status.setdefault(b["id"], b["status"])
            print(f"  expected clashes now: {len(clashes)}")
        assert clashes, "no clash could be created for the test"
        expected_ids = [c["booking"]["id"] for c in clashes]

        heading("STEP 1: stock_low event for COL-6N -> Orchestrator -> Inventory Agent -> clash check")
        # Created as 'processing' (already claimed), exactly as the worker would hand it over.
        event = supabase.table("events").insert({
            "type": "stock_low", "source": TEST_SOURCE, "status": "processing",
            "payload": {"product_id": stock["product_id"], "sku": SKU,
                        "current_qty": stock["current_qty"], "reorder_threshold": stock["reorder_threshold"]},
        }).execute().data[0]
        created["event"] = event["id"]
        print(f"Created event {event['id']} (type=stock_low)")

        summary = orchestrator.handle_event(event)
        print(f"\nOrchestrator finished: status={summary['status']}, routed to {summary['routed_to']}")
        for r in summary["results"]:
            print(f"   {r.get('sku') or r.get('label')}: {r['outcome']}")
        assert summary["status"] == "handled"

        actions = (supabase.table("agent_actions").select("*").eq("event_id", event["id"])
                   .order("created_at").execute().data)
        approvals = {a["agent_action_id"]: a for a in supabase.table("approvals").select("*")
                     .in_("agent_action_id", [a["id"] for a in actions]).execute().data}

        print("\nDraft purchase order (Inventory Agent):")
        for a in [a for a in actions if a["tool_called"] == "create_draft_po"]:
            po = supabase.table("purchase_orders").select("*").eq("id", a["result"]["purchase_order_id"]).execute().data[0]
            print(f"  purchase_orders: status={po['status']}, qty={po['qty']}, supplier={po['supplier']}")
            print(f"  agent_actions  : agent={a['agent_name']}, risk={a['risk_tier']}, status={a['status']}")
            print(f"  approvals      : status={approvals[a['id']]['status']}")

        holds = [a for a in actions if a["tool_called"] == "hold_appointment"]
        print(f"\nHold proposals (Orchestrator): {len(holds)}")
        for a in holds:
            i = a["input"]
            print(f"  agent_actions: agent={a['agent_name']}, tool={a['tool_called']}, risk={a['risk_tier']}, status={a['status']}")
            print(f"     {i['service']} at {hhmm(parse_dt(i['start_time']))}: needs {i['units_needed']}, "
                  f"stock left for it {i['stock_before_this_booking']} (stock now {i['stock_now']})")
            print(f"  approvals    : status={approvals[a['id']]['status']}")
        assert [a["input"]["appointment_id"] for a in holds] == expected_ids, "the proposals do not match the expected clashes"

        print("\nDecision log rows for this event:")
        for log in supabase.table("decision_log").select("*").eq("event_id", event["id"]).order("created_at").execute().data:
            print(f"  agents={log['agents_involved']}\n    outcome  : {log['outcome']}\n    reasoning: {log['reasoning']}")

        statuses = {i: status_of(i) for i in expected_ids}
        assert all(s == "booked" for s in statuses.values()), "an appointment changed before approval!"
        print("\nCHECK: every clashing appointment is still 'booked' (the Orchestrator only proposes).")

        heading("STEP 2: APPROVE the first hold")
        manager = supabase.table("profiles").select("id").eq("role", "manager").limit(1).execute().data[0]["id"]
        first = holds[0]
        before = status_of(first["input"]["appointment_id"])
        changes = approve(approvals[first["id"]]["id"], manager)
        after = status_of(first["input"]["appointment_id"])
        print(f"appointment {first['input']['service']} {hhmm(parse_dt(first['input']['start_time']))}: status {before!r} -> {after!r}")
        print(f"  approvals    : status={changes['approval']['status']}, decided_at={changes['approval']['decided_at']}")
        print(f"  agent_actions: status={changes['agent_action']['status']}")
        print(f"  decision_log : outcome={changes['decision_log']['outcome']!r}")
        assert (before, after) == ("booked", "held")
        assert changes["agent_action"]["status"] == "executed"
        others = [status_of(a["input"]["appointment_id"]) for a in holds[1:]]
        print(f"  the other {len(others)} proposal(s) are untouched: appointments still {sorted(set(others)) or '-'}")
        assert all(s == "booked" for s in others)
        print("\nALL CHECKS PASSED")

    finally:
        heading("CLEANUP")
        # 1. Put every appointment back to the status it had.
        restored = 0
        for appointment_id, status in original_status.items():
            if appointment_id in temp_appointments:
                continue
            if status_of(appointment_id) != status:
                supabase.table("appointments").update({"status": status}).eq("id", appointment_id).execute()
                restored += 1
        print(f"  restored {restored} appointment(s) to their original status")

        # 2. Delete the approvals, actions and purchase orders this test created (children first).
        if created["event"]:
            actions = supabase.table("agent_actions").select("id, tool_called, result").eq("event_id", created["event"]).execute().data
            action_ids = [a["id"] for a in actions]
            po_ids = [a["result"]["purchase_order_id"] for a in actions if a["tool_called"] == "create_draft_po"]
            if action_ids:
                supabase.table("approvals").delete().in_("agent_action_id", action_ids).execute()
                supabase.table("agent_actions").delete().in_("id", action_ids).execute()
            for po_id in po_ids:
                supabase.table("purchase_orders").delete().eq("id", po_id).execute()
            print(f"  deleted {len(action_ids)} action(s) with their approvals, and {len(po_ids)} purchase order(s)")

        # 3. Delete the temporary appointments and the booking_created events their creation caused.
        if temp_appointments:
            events = supabase.table("events").select("id, payload").eq("type", "booking_created").execute().data
            mine = [e["id"] for e in events if (e["payload"] or {}).get("appointment_id") in temp_appointments]
            for event_id in mine:
                supabase.table("events").delete().eq("id", event_id).execute()
            for appointment_id in temp_appointments:
                supabase.table("appointments").delete().eq("id", appointment_id).execute()
            print(f"  deleted {len(temp_appointments)} temporary appointment(s) and {len(mine)} booking_created event(s)")

        print("  KEPT the test event (source = 'orchestrator_test') and its decision_log rows: decision_log is append-only.")
        print("  Your hello_agent, inventory_test, worker_test and scheduling_test rows were not touched.")
        left = {i: status_of(i) for i in original_status if i not in temp_appointments}
        print(f"  appointments now: {sorted(set(left.values()))} (all back to their starting status: "
              f"{all(left[i] == original_status[i] for i in left)})")


if __name__ == "__main__":
    main()
