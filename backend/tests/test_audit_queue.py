"""
test_audit_queue.py - checks scenario 4 (shelf audit), scenario 5 (waiting area overloaded) and the customer
notices (scenarios 2 and 5) OFFLINE with fake data. It never touches the database.

Run it from the project root:   python -m backend.tests.test_audit_queue
"""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import backend.agents.cx_agent as cx
import backend.agents.inventory_agent as inv
import backend.agents.orchestrator as orch
import backend.agents.scheduling_agent as sched
import backend.proposals as proposals

os.environ["OLLAMA_MODEL"] = ""   # no AI call in these tests: the template sentence is used

DAY = "2030-01-01"   # a Tuesday


def check(label, condition):
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    assert condition, label


# ----------------------------------------------------------------------------
# A fake database shared by all the flow checks
# ----------------------------------------------------------------------------

logs = []
fake_db = MagicMock()


def reset_db():
    logs.clear()
    fake_db.reset_mock()
    fake_db.table.return_value.insert.return_value.execute.return_value.data = [{"id": "row-1"}]


def record_log(event_id, agents, reasoning, outcome):
    logs.append({"agents": agents, "reasoning": reasoning, "outcome": outcome})


def inserted(tool):
    return [c.args[0] for c in fake_db.table.return_value.insert.call_args_list if c.args[0].get("tool_called") == tool]


def tables_touched():
    return [c.args[0] for c in fake_db.table.call_args_list]


proposals.supabase = fake_db
proposals.add_decision_log = record_log
inv.add_decision_log = record_log
sched.add_decision_log = record_log
cx.add_decision_log = record_log
orch.add_decision_log = record_log
orch.supabase = fake_db
sched.supabase = fake_db
reset_db()


# ----------------------------------------------------------------------------
# Scenario 4: shelf audit
# ----------------------------------------------------------------------------

def audit():
    print("== 1. the audit rule: more than 1 apart is suspicious ==")
    for observed, db_qty, suspicious in [(0, 8, True), (6, 8, True), (7, 8, False), (8, 8, False), (9, 8, False), (10, 8, True), (2, 2, False)]:
        check(f"camera {observed} vs database {db_qty} -> {'FLAG' if suspicious else 'fine'} (difference {observed - db_qty:+d})",
              inv.audit_difference(observed, db_qty)[1] == suspicious)

    print("\n== 2. what the audit writes ==")
    stocks = {"SKN-001": {"product_id": "p1", "sku": "SKN-001", "name": "Gentle Foam Cleanser", "current_qty": 8},
              "SER-001": {"product_id": "p2", "sku": "SER-001", "name": "Shine Serum", "current_qty": 10},
              "SER-002": {"product_id": "p3", "sku": "SER-002", "name": "Anti-Frizz Serum", "current_qty": 10}}
    inv.get_stock = lambda sku=None, product_id=None: stocks.get(sku)
    pending = set()
    inv.pending_input_values = lambda tool, key: set(pending)

    def event(observations):
        return {"id": "ev-1", "type": "shelf_observation", "payload": {"image_path": "shelf.jpg", "observations": observations}}

    reset_db()
    results = inv.audit_shelf(event([{"sku": "SKN-001", "observed_count": 0, "empty_slots": 3},
                                     {"sku": "SER-001", "observed_count": 9},       # 1 apart: fine
                                     {"sku": "SER-002", "observed_count": 10},      # equal: fine
                                     {"sku": "NOPE", "observed_count": 4}]))       # unknown product
    print("   outcomes:", [(r["label"], r["outcome"]) for r in results])
    check("ONE audit flag: only SKN-001 (0 vs 8) differs by more than 1", [r["label"] for r in results] == ["audit SKN-001"])
    flag = inserted("raise_audit_flag")
    check("raise_audit_flag: inventory_agent, MEDIUM risk, awaiting_approval", len(flag) == 1 and flag[0]["agent_name"] == "inventory_agent"
          and flag[0]["risk_tier"] == "medium" and flag[0]["status"] == "awaiting_approval")
    check("the input has the observed count, the database count and the difference",
          flag[0]["input"]["observed_count"] == 0 and flag[0]["input"]["db_qty"] == 8 and flag[0]["input"]["difference"] == -8)
    check("the result says the stock was NOT changed", flag[0]["result"]["stock_changed"] is False)
    approvals = [c.args[0] for c in fake_db.table.return_value.insert.call_args_list if "agent_action_id" in c.args[0]]
    check("one 'pending' approval", len(approvals) == 1 and approvals[0]["status"] == "pending")
    check("one decision_log row with the real numbers", len(logs) == 1 and logs[0]["outcome"] == "audit flag proposed, waiting for approval"
          and "counted 0" in logs[0]["reasoning"] and "database says 8" in logs[0]["reasoning"])
    check("the stock is NEVER changed: no stock_levels or stock_transactions table was touched",
          not any(t in ("stock_levels", "stock_transactions") for t in tables_touched()))

    print("\n== 3. duplicates, and the whole Inventory Agent on a shelf event ==")
    pending.add("SKN-001"); reset_db()
    results = inv.audit_shelf(event([{"sku": "SKN-001", "observed_count": 0}]))
    check("a flag already waiting for this product is not raised again", inserted("raise_audit_flag") == []
          and results[0]["outcome"] == "audit flag already proposed" and logs[0]["outcome"] == "audit flag already proposed")
    pending.clear(); reset_db()
    inv.products_to_check = lambda ev: ([], [])           # pretend no product is low for the reorder rules
    inv.set_event_status = lambda event_id, status: None
    summary = inv.handle_event(event([{"sku": "SKN-001", "observed_count": 0}]))
    check("handle_event of a shelf_observation includes the audit flag", summary["status"] == "handled"
          and any(r["outcome"] == "audit flag proposed, waiting for approval" for r in summary["results"]))
    reset_db()
    summary = inv.handle_event({"id": "ev-2", "type": "stock_low", "payload": {"sku": "SKN-001"}})
    check("a stock_low event does NOT run the audit", inserted("raise_audit_flag") == [])


# ----------------------------------------------------------------------------
# Scenario 5: waiting area overloaded
# ----------------------------------------------------------------------------

def stylist(sid, name, end="18:00:00"):
    return {"id": sid, "name": name, "shift_start": "09:00:00", "shift_end": end}


def appt(appt_id, staff_id, start, end, customer=None, service="Men's Cut", status="booked", day=DAY):
    return {"id": appt_id, "staff_id": staff_id, "customer_id": customer, "status": status,
            "start_time": f"{day}T{start}:00+00:00", "end_time": f"{day}T{end}:00+00:00", "services": {"name": service}}


def queue():
    print("\n== 4. how far to shift (15 or 30 minutes) ==")
    for waiting, capacity, avg, expected in [(5, 4, 10, 15), (6, 4, 10, 15), (7, 4, 10, 30), (8, 4, 25, 30), (5, 4, 30, 30), (5, None, 45, 30), (6, None, None, 15), (7, None, None, 30)]:
        check(f"waiting {waiting}, capacity {capacity}, average wait {avg} -> {expected} min", sched.choose_shift_minutes(waiting, capacity, avg) == expected)

    print("\n== 5. the shift rule: never overlap the stylist, never past the shift ==")
    ben = stylist("s-ben", "Ben")
    first = appt("a1", "s-ben", "10:00", "10:30")

    def plan(day_appts, minutes=30, staff=ben, appointment=first):
        return sched.plan_shift(appointment, minutes, staff, day_appts)

    result, why = plan([first])
    check("free stylist: moves 30 minutes later (10:30-11:00)", result["minutes"] == 30 and result["new_start"].strftime("%H:%M") == "10:30")
    check("the appointment itself is not counted as an overlap", result is not None)
    result, why = plan([first, appt("a2", "s-ben", "10:45", "11:15")])
    print("   ", "30 min blocked at 10:45 ->", result["minutes"], "min instead")
    check("30 min would overlap the 10:45 appointment, so it falls back to 15 min", result["minutes"] == 15 and result["new_end"].strftime("%H:%M") == "10:45")
    result, why = plan([first, appt("a2", "s-ben", "10:30", "11:00")])
    print("    both blocked ->", why)
    check("the next appointment starts right after: neither 30 nor 15 fits -> NO shift, with reasons",
          result is None and "30 min later would overlap" in why and "15 min later would overlap" in why)
    for status in ("booked", "held", "in_progress"):
        check(f"a '{status}' appointment blocks the shift", plan([first, appt("a2", "s-ben", "10:30", "11:00", status=status)])[0] is None)
    check("a CANCELLED appointment does not block", plan([first, appt("a2", "s-ben", "10:30", "11:00", status="cancelled")])[0]["minutes"] == 30)
    check("another stylist's appointment does not block", plan([first, appt("a2", "s-other", "10:30", "11:00")])[0]["minutes"] == 30)
    late = appt("a9", "s-ben", "17:15", "17:45")
    result, why = plan([late], appointment=late)
    check("17:15-17:45 + 30 would end at 18:15, after the 18:00 shift: falls back to 15 (ends 18:00)", result["minutes"] == 15)
    result, why = plan([late], minutes=15, appointment=late)
    check("exactly at shift end (18:00) is allowed", result is not None and result["new_end"].strftime("%H:%M") == "18:00")
    later = appt("a9", "s-ben", "17:30", "18:00")
    result, why = plan([later], appointment=later)
    check("an appointment already ending at 18:00 cannot move: 'after the shift'", result is None and "after the shift" in why)
    check("15-minute preference does not try other sizes", len(plan([first, appt("a2", "s-ben", "10:30", "11:00")], minutes=15)[1].split(";")) == 1)

    print("\n== 6. the Scheduling Agent on a queue_overload event ==")
    staff = [stylist("s-aria", "Aria"), stylist("s-ben", "Ben"), stylist("s-dev", "Dev")]
    booked = [appt("m1", "s-aria", "10:00", "10:30", customer="cust-1"),
              appt("m2", "s-ben", "10:30", "11:00", customer="cust-2"),
              appt("m3", "s-dev", "11:00", "11:30", customer=None)]        # a walk-in
    asked = {}
    sched.get_staff = lambda staff_id=None: staff
    sched.get_day_appointments = lambda start, end: list(booked)
    sched.get_next_booked_appointments = lambda from_iso, limit: asked.update(from_iso=from_iso, limit=limit) or booked[:limit]
    sched.pending_input_values = lambda tool, key: set()
    event = {"id": "ev-q", "type": "queue_overload", "created_at": f"{DAY}T09:00:00+00:00",
             "payload": {"waiting_count": 8, "capacity": 4, "avg_wait_minutes": 25, "time": f"{DAY}T09:00:00+00:00"}}

    reset_db(); context = {}
    summary = sched.handle_event(event, context)
    print("   outcomes:", [(r["label"], r["outcome"]) for r in summary["results"]])
    check("it asked for the next MAX_SHIFTS (3) booked appointments after the event time", asked["limit"] == 3 and asked["from_iso"].startswith(f"{DAY}T09:00"))
    shifts = inserted("shift_appointment")
    check("3 shift proposals: scheduling_agent, MEDIUM risk, awaiting_approval",
          len(shifts) == 3 and all(a["agent_name"] == "scheduling_agent" and a["risk_tier"] == "medium" and a["status"] == "awaiting_approval" for a in shifts))
    check("waiting 8 vs capacity 4 -> every shift is 30 minutes later", all(a["input"]["minutes"] == 30 for a in shifts)
          and shifts[0]["input"]["new_start"].startswith(f"{DAY}T10:30") and shifts[0]["input"]["old_start"].startswith(f"{DAY}T10:00"))
    check("3 pending approvals and 3 decision_log rows ('shift proposed, waiting for approval')",
          len([c for c in fake_db.table.return_value.insert.call_args_list if "agent_action_id" in c.args[0]]) == 3
          and [l["outcome"] for l in logs] == ["shift proposed, waiting for approval"] * 3
          and logs[0]["agents"] == ["scheduling_agent", "cx_agent"])
    check("NO appointment was changed (no database call on 'appointments')", "appointments" not in tables_touched())
    check("the proposals are handed to the CX Agent through the context", [s["appointment_id"] for s in context["shifts"]] == ["m1", "m2", "m3"])
    check("the agent does not set the event status itself (the Orchestrator does)", summary["status"] == "handled" and "events" not in tables_touched())

    print("\n== 7. more cases ==")
    sched.get_next_booked_appointments = lambda from_iso, limit: booked[:limit]
    reset_db(); context = {}
    sched.pending_input_values = lambda tool, key: {"m2"}
    summary = sched.handle_event(event, context)
    check("an appointment that already has a shift waiting gets no second proposal", len(inserted("shift_appointment")) == 2
          and any(r["outcome"] == "shift already proposed" for r in summary["results"]))
    sched.pending_input_values = lambda tool, key: set()

    reset_db(); context = {}
    sched.get_day_appointments = lambda start, end: booked + [appt("m4", "s-aria", "10:30", "11:00")]   # Aria is busy right after m1
    summary = sched.handle_event(event, context)
    check("m1 cannot move (Aria is busy right after): 'no safe shift found, manager must decide', the other two still move",
          [r["outcome"] for r in summary["results"]] == ["no safe shift found, manager must decide"] + ["shift proposed, waiting for approval"] * 2
          and len(inserted("shift_appointment")) == 2 and any(l["outcome"] == "no safe shift found, manager must decide" for l in logs))
    sched.get_day_appointments = lambda start, end: list(booked)

    reset_db(); context = {}
    sched.get_next_booked_appointments = lambda from_iso, limit: []
    summary = sched.handle_event(event, context)
    check("no booked appointments -> logged, nothing proposed", logs[0]["outcome"] == "no appointments to shift" and inserted("shift_appointment") == [])
    sched.get_next_booked_appointments = lambda from_iso, limit: booked[:limit]

    for bad in ({"waiting_count": 0}, {}, {"waiting_count": "many"}):
        try:
            sched.handle_event(dict(event, payload=bad), {}); raised = False
        except ValueError as error:
            raised = "waiting_count" in str(error)
        check(f"payload {bad} is refused clearly", raised)
    reset_db()
    sched.handle_event(dict(event, payload={"waiting_count": 5, "capacity": 4}), {})
    check("without a 'time' the event's created_at is used, and 5 vs 4 -> 15 minutes",
          asked["from_iso"].startswith(f"{DAY}T09:00") and inserted("shift_appointment")[0]["input"]["minutes"] == 15)


# ----------------------------------------------------------------------------
# Customer notices (scenarios 2 and 5)
# ----------------------------------------------------------------------------

def notices():
    print("\n== 8. the CX Agent drafts a notice for each affected customer (walk-ins get none) ==")
    reset_db()
    staff = [stylist("s-aria", "Aria"), stylist("s-ben", "Ben"), stylist("s-dev", "Dev")]
    booked = [appt("m1", "s-aria", "10:00", "10:30", customer="cust-1"), appt("m2", "s-ben", "10:30", "11:00", customer="cust-2"),
              appt("m3", "s-dev", "11:00", "11:30", customer=None)]
    sched.get_staff = lambda staff_id=None: staff
    sched.get_day_appointments = lambda start, end: list(booked)
    sched.get_next_booked_appointments = lambda from_iso, limit: booked[:limit]
    sched.pending_input_values = lambda tool, key: set()
    cx.pending_input_values = lambda tool, key: set()
    event = {"id": "ev-q", "type": "queue_overload", "created_at": f"{DAY}T09:00:00+00:00", "payload": {"waiting_count": 8, "capacity": 4}}
    context = {}
    sched.handle_event(event, context)
    summary = cx.handle_event(event, context)
    print("   outcomes:", [(r["label"], r["outcome"]) for r in summary["results"]])
    drafts = inserted("draft_customer_notice")
    check("2 notices (customers 1 and 2): cx_agent, MEDIUM risk, awaiting_approval, NOT sent",
          len(drafts) == 2 and [d["input"]["customer_id"] for d in drafts] == ["cust-1", "cust-2"]
          and all(d["agent_name"] == "cx_agent" and d["risk_tier"] == "medium" and d["status"] == "awaiting_approval"
                  and d["result"]["sent"] is False and d["result"]["draft_text"] for d in drafts))
    check("the delay notice names the service and the new time", "Men's Cut" in drafts[0]["result"]["draft_text"]
          and "10:30 UTC" in drafts[0]["result"]["draft_text"] and drafts[0]["input"]["kind"] == "delay")
    check("the walk-in gets no notice, and that is logged once", any(l["outcome"] == "no notice needed for walk-ins" for l in logs))
    notice_logs = [l for l in logs if l["outcome"] == "customer notice drafted, waiting for approval"]
    check("a decision_log row per notice (cx_agent + scheduling_agent)", len(notice_logs) == 2 and notice_logs[0]["agents"] == ["cx_agent", "scheduling_agent"]
          and "Nothing was sent to the customer" in notice_logs[0]["reasoning"])
    check("nothing is sent: no 'send' function exists in the CX Agent", not hasattr(cx, "send_notice") and not hasattr(cx, "send_reply"))

    print("\n== 9. scenario 2: a notice when a stylist is absent and the appointment is proposed to move ==")
    reset_db()
    context = {"reassignments": [
        {"appointment_id": "r1", "service": "Men's Cut", "start_time": f"{DAY}T10:00:00+00:00", "old_staff": "Aria", "new_staff": "Ben", "customer_id": "cust-1", "agent_action_id": "x"},
        {"appointment_id": "r2", "service": "Men's Cut", "start_time": f"{DAY}T12:00:00+00:00", "old_staff": "Aria", "new_staff": "Dev", "customer_id": None, "agent_action_id": "y"}]}
    summary = cx.handle_event({"id": "ev-a", "type": "staff_absent", "payload": {}}, context)
    drafts = inserted("draft_customer_notice")
    check("one notice (the customer), none for the walk-in", len(drafts) == 1 and drafts[0]["input"]["kind"] == "reassignment")
    check("the notice names the new stylist and says the time stays", "Ben" in drafts[0]["result"]["draft_text"] and "10:00 UTC" in drafts[0]["result"]["draft_text"])
    reset_db()
    cx.pending_input_values = lambda tool, key: {"r1"}
    cx.handle_event({"id": "ev-a", "type": "staff_absent", "payload": {}}, context)
    check("a notice that is already waiting for this appointment is not drafted again", inserted("draft_customer_notice") == [])
    cx.pending_input_values = lambda tool, key: set()


# ----------------------------------------------------------------------------
# The Orchestrator ties them together
# ----------------------------------------------------------------------------

def routing():
    print("\n== 10. the Orchestrator runs Scheduling then CX for queue_overload ==")
    reset_db()
    booked = [appt("m1", "s-aria", "10:00", "10:30", customer="cust-1")]
    sched.get_staff = lambda staff_id=None: [stylist("s-aria", "Aria")]
    sched.get_day_appointments = lambda start, end: list(booked)
    sched.get_next_booked_appointments = lambda from_iso, limit: booked
    order = []
    orch.AGENTS = {"scheduling_agent": lambda e, context: order.append("scheduling") or sched.handle_event(e, context),
                   "cx_agent": lambda e, context: order.append("cx") or cx.handle_event(e, context)}
    event = {"id": "ev-q", "type": "queue_overload", "created_at": f"{DAY}T09:00:00+00:00", "payload": {"waiting_count": 7, "capacity": 4}}
    summary = orch.handle_event(event)
    check("order: scheduling, then cx", order == ["scheduling", "cx"] and summary["routed_to"] == "scheduling_agent + cx_agent")
    check("a shift AND a notice were proposed, and the event is handled",
          len(inserted("shift_appointment")) == 1 and len(inserted("draft_customer_notice")) == 1 and summary["status"] == "handled")
    check("the worker would pick queue_overload up", orch.is_routed(event) is True)

    print("\n== 11. a shelf_observation goes to the Inventory Agent only (it does the audit itself) ==")
    check("ROUTES['shelf_observation'] == ['inventory_agent']", orch.ROUTES["shelf_observation"] == ["inventory_agent"])

    print("\n== 12. a crash in the CX Agent after the shifts: failed, logged, worker carries on ==")
    reset_db()
    orch.AGENTS["cx_agent"] = lambda e, context: (_ for _ in ()).throw(RuntimeError("cx exploded"))
    summary = orch.handle_event(event)
    check("event failed and the reason logged", summary["status"] == "failed" and logs[-1]["outcome"] == "event failed" and "cx exploded" in logs[-1]["reasoning"])


if __name__ == "__main__":
    audit()
    queue()
    notices()
    routing()
    print("\nALL OFFLINE AUDIT/QUEUE CHECKS PASSED (no database rows written)")
