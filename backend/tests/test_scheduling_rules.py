"""
test_scheduling_rules.py - checks the Scheduling Agent's rules OFFLINE with fake data.
It never touches the database.

Run it from the project root:   python -m backend.tests.test_scheduling_rules
"""

import os
from unittest.mock import MagicMock

import backend.agents.scheduling_agent as agent
import backend.tools.scheduling_tools as tools

os.environ["OLLAMA_MODEL"] = ""   # no AI call in these tests: the template sentence is used

TUESDAY, SUNDAY = "2030-01-01", "2030-01-06"


def person(pid, name, skills, start="09:00:00", end="18:00:00", days=(1, 2, 3, 4, 5, 6), available=True):
    return {"id": pid, "name": name, "skills": list(skills), "shift_start": start, "shift_end": end,
            "work_days": list(days), "is_available": available}


def appt(day, start, end, staff_id="s-aria", status="booked", service="Men's Cut", appt_id="a1"):
    return {"id": appt_id, "staff_id": staff_id, "start_time": f"{day}T{start}:00+00:00",
            "end_time": f"{day}T{end}:00+00:00", "status": status, "services": {"name": service}}


def check(label, condition):
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    assert condition, label


ARIA = person("s-aria", "Aria", ["cut", "styling"])            # the absent stylist
BEN = person("s-ben", "Ben", ["colour", "cut"])
CLEO = person("s-cleo", "Cleo", ["colour", "styling"])
DEV = person("s-dev", "Dev", ["cut", "styling"])


def pick(appointment, skill, staff, day_appts):
    chosen, checks = agent.choose_replacement(appointment, skill, staff, day_appts, "s-aria")
    return (chosen["staff"]["name"] if chosen else None), {c["staff"]["name"]: c["reason"] for c in checks}


def rules():
    cut = appt(TUESDAY, "09:30", "10:00")

    print("== 1. a free, qualified stylist is chosen ==")
    name, why = pick(cut, "cut", [ARIA, BEN, CLEO], [])
    print("   ", why)
    check("Ben (has 'cut', free) is chosen", name == "Ben")
    check("Cleo is skipped: no 'cut' skill", why["Cleo"] == "does not have the 'cut' skill")
    check("the absent stylist is never chosen", why["Aria"] == "is the absent stylist")

    print("\n== 2. an overlapping stylist is skipped ==")
    for status in ("booked", "held", "in_progress"):
        day = [appt(TUESDAY, "09:00", "10:00", "s-ben", status, appt_id="b1")]
        name, why = pick(cut, "cut", [ARIA, BEN, DEV], day)
        check(f"Ben with an overlapping '{status}' appointment is skipped -> Dev", name == "Dev" and "already has an appointment" in why["Ben"])
    day = [appt(TUESDAY, "09:00", "10:00", "s-ben", "cancelled", appt_id="b1")]
    check("a CANCELLED overlap does not block Ben", pick(cut, "cut", [ARIA, BEN], day)[0] == "Ben")
    day = [appt(TUESDAY, "09:00", "09:30", "s-ben", appt_id="b1")]   # ends exactly when ours starts
    check("an appointment that ENDS exactly when ours starts does not block", pick(cut, "cut", [ARIA, BEN], day)[0] == "Ben")

    print("\n== 3. a stylist outside their shift is skipped ==")
    late = appt(TUESDAY, "17:30", "18:30")
    name, why = pick(late, "cut", [ARIA, BEN, DEV], [])
    check("17:30-18:30 is past everyone's 18:00 shift end -> nobody", name is None and "does not cover" in why["Ben"])
    late_dev = person("s-dev", "Dev", ["cut"], start="10:00:00", end="19:00:00")
    check("Dev with a 10:00-19:00 shift DOES cover 17:30-18:30", pick(late, "cut", [ARIA, BEN, late_dev], [])[0] == "Dev")
    early = appt(TUESDAY, "08:30", "09:00")
    check("08:30 is before the 09:00 shift start -> nobody", pick(early, "cut", [ARIA, BEN, DEV], [])[0] is None)

    print("\n== 4. other reasons to skip ==")
    check("is_available = false is skipped", pick(cut, "cut", [ARIA, person("s-ben", "Ben", ["cut"], available=False)], [])[0] is None)
    sunday = appt(SUNDAY, "09:30", "10:00")
    name, why = pick(sunday, "cut", [ARIA, BEN], [])
    check("a stylist who does not work Sundays is skipped", name is None and why["Ben"] == "does not work on Sunday")

    print("\n== 5. the stylist with the FEWEST appointments that day wins ==")
    busy_ben = [appt(TUESDAY, h, e, "s-ben", appt_id=f"b{i}") for i, (h, e) in enumerate([("12:00", "13:00"), ("14:00", "15:00"), ("16:00", "17:00")])]
    busy_dev = [appt(TUESDAY, "12:00", "13:00", "s-dev", appt_id="d1")]
    name, why = pick(cut, "cut", [ARIA, BEN, DEV], busy_ben + busy_dev)
    check("Dev (1 appointment) beats Ben (3 appointments)", name == "Dev")
    name, _ = pick(cut, "cut", [ARIA, BEN, DEV], busy_dev + [appt(TUESDAY, "12:00", "13:00", "s-ben", appt_id="b9")])
    check("on a tie the alphabetical first (Ben) is chosen", name == "Ben")

    print("\n== 6. nobody fits -> no replacement ==")
    facial = appt(TUESDAY, "10:00", "11:00", service="Facial & Skincare")
    name, why = pick(facial, "skincare", [ARIA, BEN, CLEO, DEV], [])
    print("   ", why)
    check("nobody has the 'skincare' skill -> no replacement", name is None)


def full_flow():
    """Run the whole agent on a fake database and check what it would write."""
    print("\n== 7. the whole agent on a fake database (nothing real is written) ==")
    fake_db = MagicMock()
    fake_db.table.return_value.insert.return_value.execute.return_value.data = [{"id": "row-1"}]
    logs, statuses = [], []
    agent.supabase = fake_db
    agent.add_decision_log = lambda event_id, agents, reasoning, outcome: logs.append((outcome, reasoning))
    agent.set_event_status = lambda event_id, status: statuses.append(status)

    event = {"id": "ev-1", "type": "staff_absent", "payload": {
        "staff_id": "s-aria", "from": f"{TUESDAY}T00:00:00+00:00", "to": f"{TUESDAY}T23:59:59+00:00", "reason": "sick"}}
    agent.get_staff = lambda staff_id=None: ARIA if staff_id else [ARIA, BEN, DEV]
    # Ben and Dev each already have ONE appointment that day (15:30), so they tie on workload.
    day = [appt(TUESDAY, "15:30", "16:00", "s-ben", appt_id="b1"), appt(TUESDAY, "15:30", "16:00", "s-dev", appt_id="d1")]
    agent.get_day_appointments = lambda start, end: list(day)

    # Aria has two cuts: 09:30 and 12:30. Both can be covered; the second must go to the LESS busy one.
    agent.get_appointments = lambda *a: [appt(TUESDAY, "09:30", "10:00", appt_id="m1"),
                                         appt(TUESDAY, "12:30", "13:00", appt_id="m2")]
    reassign_spy = MagicMock(side_effect=AssertionError("the agent must NEVER change an appointment"))
    tools.reassign_staff = reassign_spy

    summary = agent.handle_event(event)
    print("   outcomes:", [(r["label"], r["outcome"], r.get("new_staff")) for r in summary["results"]])
    check("event ends 'handled'", summary["status"] == "handled" and statuses == ["handled"])
    check("two proposals were made", [r["outcome"] for r in summary["results"]] == ["reassignment proposed, waiting for approval"] * 2)
    check("1st goes to Ben (tie -> alphabetical), 2nd to Dev (Ben is now busier)",
          [r["new_staff"] for r in summary["results"]] == ["Ben", "Dev"])
    inserts = [(c.args[0], c) for c in fake_db.table.call_args_list]
    tables = [name for name, _ in inserts]
    check("no appointment was touched (no database call on 'appointments')", "appointments" not in tables)
    reassign_spy.assert_not_called()
    actions = [c.args[0] for c in fake_db.table.return_value.insert.call_args_list if "tool_called" in c.args[0]]
    check("agent_actions rows: reassign_staff, medium, awaiting_approval, with old and new stylist",
          len(actions) == 2 and all(a["tool_called"] == "reassign_staff" and a["agent_name"] == "scheduling_agent"
                                    and a["risk_tier"] == "medium" and a["status"] == "awaiting_approval" for a in actions)
          and actions[0]["input"]["old_staff_name"] == "Aria" and actions[0]["input"]["new_staff_name"] == "Ben")
    approvals = [c.args[0] for c in fake_db.table.return_value.insert.call_args_list if "agent_action_id" in c.args[0]]
    check("one 'pending' approval per proposal", len(approvals) == 2 and all(a["status"] == "pending" for a in approvals))
    check("decision_log outcome 'reassignment proposed, waiting for approval' x2",
          [o for o, _ in logs] == ["reassignment proposed, waiting for approval"] * 2)
    print("   reasoning:", logs[0][1])

    print("\n== 8. no replacement -> 'manager must decide', nothing proposed ==")
    logs.clear(); statuses.clear(); fake_db.reset_mock()
    fake_db.table.return_value.insert.return_value.execute.return_value.data = [{"id": "row-1"}]
    agent.get_appointments = lambda *a: [appt(TUESDAY, "10:00", "11:00", service="Facial & Skincare", appt_id="f1")]
    summary = agent.handle_event(event)
    print("   log:", logs[0])
    check("outcome is 'no replacement found, manager must decide'", logs[0][0] == "no replacement found, manager must decide")
    check("the reason lists why each stylist was skipped", "Ben does not have the 'skincare' skill" in logs[0][1])
    check("NO agent_actions or approvals were created", fake_db.table.return_value.insert.call_count == 0)
    check("event still ends 'handled'", statuses == ["handled"] and summary["status"] == "handled")

    print("\n== 9. a service with no skill mapping -> manager must decide ==")
    logs.clear()
    agent.get_appointments = lambda *a: [appt(TUESDAY, "10:00", "11:00", service="Mystery Treatment", appt_id="x1")]
    agent.handle_event(event)
    check("unknown service is not guessed", logs[0][0] == "no replacement found, manager must decide" and "no skill is defined" in logs[0][1])

    print("\n== 10. no appointments in the period ==")
    logs.clear(); agent.get_appointments = lambda *a: []
    agent.handle_event(event)
    check("logged as 'no appointments to reassign'", logs[0][0] == "no appointments to reassign")

    print("\n== 11. a failure marks the event 'failed' and logs why ==")
    logs.clear(); statuses.clear()

    def explode(*a, **k): raise RuntimeError("database exploded")
    agent.get_staff = explode
    summary = agent.handle_event(event)
    check("status 'failed' and reason logged", statuses == ["failed"] and summary["status"] == "failed"
          and logs[0][0] == "event failed" and "database exploded" in logs[0][1])
    bad = dict(event, payload={"from": "x"})
    statuses.clear(); logs.clear()
    check("a payload without staff_id fails clearly", agent.handle_event(bad)["status"] == "failed" and "staff_id" in logs[0][1])


def worker_routing():
    print("\n== 12. the Orchestrator routes staff_absent to the Scheduling Agent ==")
    import backend.agents.orchestrator as orchestrator
    check("ROUTES['staff_absent'] starts with the Scheduling Agent", orchestrator.ROUTES["staff_absent"][0] == "scheduling_agent")
    check("the Scheduling Agent's handle_event is registered", orchestrator.AGENTS["scheduling_agent"] is agent.handle_event)
    check("Inventory routes are unchanged",
          orchestrator.ROUTES["stock_low"] == orchestrator.ROUTES["shelf_observation"] == ["inventory_agent"])


if __name__ == "__main__":
    rules()
    full_flow()
    worker_routing()
    print("\nALL OFFLINE SCHEDULING CHECKS PASSED (no database rows written)")
