"""
test_scheduling_agent.py - tests the Scheduling Agent end to end on your REAL database.

What it does:
  1. Picks a future day where Aria Demo has at least 2 bookings that other stylists can cover.
  2. Creates a staff_absent event for Aria on that day and runs the agent.
  3. Prints what the agent wrote, and shows that NO appointment has changed yet.
  4. APPROVES the first proposal (the appointment moves) and REJECTS the second (nothing moves).
  5. Prints the appointments before and after.
  6. Cleans up: puts the appointments back, deletes the actions and approvals it created.

Run it from the project root:   python -m backend.tests.test_scheduling_agent

About cleanup: decision_log is append-only, and an event with log rows cannot be deleted. So the
test event (source = 'scheduling_test') and its decision_log rows stay. Everything else is removed.
"""

from datetime import datetime, timedelta, timezone

from backend.agents.scheduling_agent import choose_replacement, hhmm, parse_dt, run
from backend.approvals.approval_actions import approve, reject
from backend.db import supabase
from backend.tools.scheduling_tools import get_appointments, get_day_appointments, get_staff, required_skill

TEST_SOURCE = "scheduling_test"
ABSENT_NAME = "Aria Demo"

# Everything we may need to undo: appointment id -> the stylist it had at the start.
original_staff = {}
created = {"actions": [], "approvals": []}


def heading(text):
    print("\n" + "=" * 72 + "\n" + text + "\n" + "=" * 72)


def pick_day(absent, staff_list):
    """Read-only dry run: find the first future day where at least 2 of Aria's bookings have a replacement."""
    today = datetime.now(timezone.utc).date()
    for offset in range(1, 8):
        day = today + timedelta(days=offset)
        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        appointments = get_appointments(absent["id"], start.isoformat(), (end - timedelta(seconds=1)).isoformat())
        day_appts, covered = get_day_appointments(start.isoformat(), end.isoformat()), 0
        for appointment in appointments:
            skill = required_skill(appointment["services"]["name"])
            chosen, _ = choose_replacement(appointment, skill, staff_list, day_appts, absent["id"]) if skill else (None, [])
            if chosen:
                covered += 1
                day_appts.append({"staff_id": chosen["staff"]["id"], "start_time": appointment["start_time"],
                                  "end_time": appointment["end_time"], "status": "booked"})
        if covered >= 2:
            return start, end
    raise SystemExit("Could not find a day with 2 reassignable bookings for " + ABSENT_NAME)


def appointment_rows(start, end, names):
    """Aria's appointments that day plus where each one is now, as printable lines."""
    rows = (supabase.table("appointments").select("id, start_time, end_time, status, staff_id, services(name)")
            .gte("start_time", start.isoformat()).lt("start_time", end.isoformat())
            .in_("id", list(original_staff)).order("start_time").execute().data)
    return {r["id"]: r for r in rows}


def show_table(title, rows, names):
    print(f"\n{title}")
    for r in rows.values():
        print(f"   {hhmm(parse_dt(r['start_time']))}-{hhmm(parse_dt(r['end_time']))}  "
              f"{r['services']['name']:<30} stylist: {names[r['staff_id']]:<11} status: {r['status']}")


def main():
    staff_list = get_staff()
    names = {s["id"]: s["name"] for s in staff_list}
    absent = next(s for s in staff_list if s["name"] == ABSENT_NAME)
    day_start, day_end = pick_day(absent, staff_list)
    print(f"Absent stylist: {absent['name']}   day: {day_start:%A %Y-%m-%d} (UTC)")

    # Remember how Aria's appointments look NOW, so we can compare and restore them.
    for a in get_appointments(absent["id"], day_start.isoformat(), (day_end - timedelta(seconds=1)).isoformat()):
        original_staff[a["id"]] = a["staff_id"]

    try:
        heading("STEP 1: staff_absent event -> the agent proposes reassignments")
        before = appointment_rows(day_start, day_end, names)
        show_table("Appointments BEFORE:", before, names)

        event = supabase.table("events").insert({
            "type": "staff_absent", "source": TEST_SOURCE, "status": "new",
            "payload": {"staff_id": absent["id"], "from": day_start.isoformat(),
                        "to": (day_end - timedelta(seconds=1)).isoformat(), "reason": "sick"},
        }).execute().data[0]
        print(f"\nCreated event {event['id']} (type=staff_absent, status=new)")

        summaries = run(event_ids=[event["id"]])
        assert len(summaries) == 1, "the agent should have handled exactly one event"
        summary = summaries[0]
        print(f"Agent finished: event status = {summary['status']}")
        for r in summary["results"]:
            print(f"   {r['label']}: {r['outcome']}")
            print(f"      {r['detail']}")
        assert summary["status"] == "handled"

        actions = (supabase.table("agent_actions").select("*").eq("event_id", event["id"])
                   .order("created_at").execute().data)
        created["actions"] = [a["id"] for a in actions]
        approvals = {a["agent_action_id"]: a for a in supabase.table("approvals").select("*")
                     .in_("agent_action_id", created["actions"]).execute().data}
        created["approvals"] = [a["id"] for a in approvals.values()]
        assert len(actions) >= 2, "expected at least 2 proposals"

        print("\nRows the agent wrote:")
        for a in actions:
            i = a["input"]
            print(f"  agent_actions: tool={a['tool_called']}, risk={a['risk_tier']}, status={a['status']}")
            print(f"     {i['service']} at {hhmm(parse_dt(i['start_time']))}: {i['old_staff_name']} -> {i['new_staff_name']}")
            ap = approvals[a["id"]]
            print(f"  approvals    : status={ap['status']}, decided_by={ap['decided_by']}")
        for log in supabase.table("decision_log").select("*").eq("event_id", event["id"]).order("created_at").execute().data:
            print(f"  decision_log : outcome={log['outcome']!r}\n     reasoning: {log['reasoning']}")

        after_agent = appointment_rows(day_start, day_end, names)
        assert {k: v["staff_id"] for k, v in after_agent.items()} == original_staff, "the agent changed an appointment!"
        print("\nCHECK: no appointment changed yet (the agent only proposes).")

        manager = supabase.table("profiles").select("id").eq("role", "manager").limit(1).execute().data[0]["id"]

        heading("STEP 2: APPROVE the first proposal, REJECT the second")
        first, second = actions[0], actions[1]
        changes = approve(approvals[first["id"]]["id"], manager)
        print(f"approve() on '{first['input']['service']} {hhmm(parse_dt(first['input']['start_time']))}':")
        print(f"   approvals    : status={changes['approval']['status']}, decided_at={changes['approval']['decided_at']}")
        print(f"   appointment  : staff is now {names[changes['appointment']['staff_id']]}")
        print(f"   agent_actions: status={changes['agent_action']['status']}, result={changes['agent_action']['result']}")
        print(f"   decision_log : outcome={changes['decision_log']['outcome']!r}")
        assert changes["appointment"]["staff_id"] == first["input"]["new_staff_id"]
        assert changes["agent_action"]["status"] == "executed"

        changes = reject(approvals[second["id"]]["id"], manager, "Customer asked to keep their usual stylist")
        print(f"\nreject() on '{second['input']['service']} {hhmm(parse_dt(second['input']['start_time']))}':")
        print(f"   approvals    : status={changes['approval']['status']}, reason={changes['approval']['reason']!r}")
        print(f"   appointment  : staff is still {names[changes['appointment']['staff_id']]} (not changed)")
        print(f"   agent_actions: status={changes['agent_action']['status']}")
        print(f"   decision_log : outcome={changes['decision_log']['outcome']!r}")
        assert changes["appointment"]["staff_id"] == original_staff[second["input"]["appointment_id"]]
        assert changes["agent_action"]["status"] == "rejected"

        show_table("Appointments AFTER approve + reject:", appointment_rows(day_start, day_end, names), names)
        print("\nALL CHECKS PASSED")

    finally:
        heading("CLEANUP")
        # Put every appointment back the way it was.
        restored = 0
        for appointment_id, staff_id in original_staff.items():
            current = supabase.table("appointments").select("staff_id").eq("id", appointment_id).execute().data[0]
            if current["staff_id"] != staff_id:
                supabase.table("appointments").update({"staff_id": staff_id}).eq("id", appointment_id).execute()
                restored += 1
        print(f"  restored {restored} appointment(s) to their original stylist")
        # Children first (approvals -> agent_actions).
        for table, ids in (("approvals", created["approvals"]), ("agent_actions", created["actions"])):
            for row_id in ids:
                supabase.table(table).delete().eq("id", row_id).execute()
            print(f"  deleted {len(ids)} row(s) from {table}")
        print("  KEPT the test event (source = 'scheduling_test') and its decision_log rows: "
              "decision_log is append-only.")
        print("  Your hello_agent, inventory_test and worker_test rows were not touched.")
        show_table("Appointments after cleanup:", appointment_rows(day_start, day_end, names), names)


if __name__ == "__main__":
    main()
