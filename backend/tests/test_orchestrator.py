"""
test_orchestrator.py - checks the Orchestrator's rules OFFLINE with fake data.
It never touches the database.

Run it from the project root:   python -m backend.tests.test_orchestrator
"""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import backend.agents.orchestrator as orch

os.environ["OLLAMA_MODEL"] = ""   # no AI call in these tests: the template sentence is used

NOW = datetime.now(timezone.utc)


def booking(booking_id, hours_from_now, needs, service="Full Colour"):
    """A fake 'booked' appointment that starts `hours_from_now` hours from now and needs `needs` units."""
    start = NOW + timedelta(hours=hours_from_now)
    return {"id": booking_id, "status": "booked", "needs": needs, "services": {"name": service},
            "start_time": start.isoformat(), "end_time": (start + timedelta(hours=2)).isoformat()}


def check(label, condition):
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    assert condition, label


def ids(clashes):
    return [c["booking"]["id"] for c in clashes]


# ----------------------------------------------------------------------------
# A fake database and fake tools, so we can see what the Orchestrator WOULD do
# ----------------------------------------------------------------------------

logs = []
state = {"stock": {"product_id": "p1", "sku": "COL-6N", "current_qty": 2}, "bookings": [], "pending": set()}
fake_db = MagicMock()
fake_db.table.return_value.insert.return_value.execute.return_value.data = [{"id": "row-1"}]
orch.supabase = fake_db
orch.add_decision_log = lambda event_id, agents, reasoning, outcome: logs.append(
    {"agents": agents, "reasoning": reasoning, "outcome": outcome})
orch.get_stock = lambda sku=None, product_id=None: dict(state["stock"], sku=sku) if state["stock"] else None
orch.get_upcoming_bookings = lambda product_id, from_iso: list(state["bookings"])
orch.get_pending_hold_appointment_ids = lambda: set(state["pending"])


def reset(stock_qty, bookings, pending=()):
    logs.clear()
    fake_db.reset_mock()
    fake_db.table.return_value.insert.return_value.execute.return_value.data = [{"id": "row-1"}]
    state["stock"] = {"product_id": "p1", "sku": "COL-6N", "current_qty": stock_qty}
    state["bookings"], state["pending"] = list(bookings), set(pending)


def inserted(key):
    """The rows the Orchestrator tried to insert that contain `key` (for example 'tool_called')."""
    return [c.args[0] for c in fake_db.table.return_value.insert.call_args_list if key in c.args[0]]


EVENT = {"id": "ev-1", "type": "stock_low", "payload": {"sku": "COL-6N"}}


def rules():
    print("== 1. the clash rule (find_clashes) ==")
    five = [booking("a", 1, 2), booking("b", 2, 2), booking("c", 3, 2)]
    check("enough stock (10 for 3 x 2) -> no clash", orch.find_clashes(10, five) == [])
    clashes = orch.find_clashes(5, five)
    check("not enough stock (5 for 3 x 2) -> only the LATER appointment 'c' clashes", ids(clashes) == ["c"])
    check("the clash records the stock left at that point (1) and the need (2)", clashes[0]["stock_before"] == 1 and clashes[0]["needs"] == 2)
    clashes = orch.find_clashes(3, [booking("a", 1, 2), booking("b", 2, 2)])
    check("two appointments needing 2 each with 3 in stock -> the SECOND is held", ids(clashes) == ["b"])
    check("exactly enough (4 for 2 x 2) -> no clash", orch.find_clashes(4, [booking("a", 1, 2), booking("b", 2, 2)]) == [])
    check("zero stock -> every appointment clashes", ids(orch.find_clashes(0, [booking("a", 1, 1), booking("b", 2, 1)])) == ["a", "b"])
    check("no bookings -> no clash", orch.find_clashes(2, []) == [])
    check("a clashing booking uses up no stock (3: needs 2,2,1 -> 2nd clashes, 3rd still fits)",
          ids(orch.find_clashes(3, [booking("a", 1, 2), booking("b", 2, 2), booking("c", 3, 1)])) == ["b"])


def lookahead():
    print("\n== 2. only bookings inside the lookahead window count ==")
    check("LOOKAHEAD_DAYS is 3", orch.LOOKAHEAD_DAYS == 3)
    reset(2, [booking("near", 24, 2), booking("far", 4 * 24, 2)])
    check("stock 2: 'near' fits and 'far' (4 days away) is ignored -> no clash", orch.check_clashes(EVENT, ["COL-6N"]) == [])
    reset(1, [booking("near", 24, 2), booking("far", 4 * 24, 2)])
    results = orch.check_clashes(EVENT, ["COL-6N"])
    check("stock 1: only 'near' clashes; 'far' is ignored", [r["appointment_id"] for r in results] == ["near"])
    reset(0, [booking("in", 71, 1), booking("out", 73, 1)])
    results = orch.check_clashes(EVENT, ["COL-6N"])
    check("71 hours away is inside the window, 73 hours is outside", [r["appointment_id"] for r in results] == ["in"])


def proposals():
    print("\n== 3. what a proposal writes ==")
    reset(2, [booking("a1", 24, 2), booking("a2", 26, 2, "Root Touch-up")])
    results = orch.check_clashes(EVENT, ["COL-6N"])
    print("   result:", results[0]["outcome"], "|", results[0]["label"])
    check("one clash -> one proposal", len(results) == 1 and results[0]["appointment_id"] == "a2")
    actions = inserted("tool_called")
    check("agent_actions: orchestrator, hold_appointment, medium, awaiting_approval",
          len(actions) == 1 and actions[0]["agent_name"] == "orchestrator" and actions[0]["tool_called"] == "hold_appointment"
          and actions[0]["risk_tier"] == "medium" and actions[0]["status"] == "awaiting_approval")
    check("input and result are JSON with the appointment, the numbers and 'applied': false",
          actions[0]["input"]["appointment_id"] == "a2" and actions[0]["input"]["units_needed"] == 2
          and actions[0]["input"]["stock_before_this_booking"] == 0 and actions[0]["result"]["applied"] is False)
    approvals = inserted("agent_action_id")
    check("one approval, 'pending'", len(approvals) == 1 and approvals[0]["status"] == "pending")
    check("the appointment was NOT changed (no database call on 'appointments')",
          "appointments" not in [c.args[0] for c in fake_db.table.call_args_list])
    check("ONE decision_log row, with the three agents and the right outcome",
          len(logs) == 1 and logs[0]["agents"] == ["orchestrator", "inventory_agent", "scheduling_agent"]
          and logs[0]["outcome"] == "clash found, hold proposed, waiting for approval")
    print("   reasoning:", logs[0]["reasoning"])
    check("reasoning has the real numbers (stock left, needed, stock now) and the time",
          "0 unit(s) would be left" in logs[0]["reasoning"] and "needs 2" in logs[0]["reasoning"]
          and "stock now 2" in logs[0]["reasoning"] and "Root Touch-up" in logs[0]["reasoning"])

    print("\n== 4. a duplicate pending hold is not created ==")
    reset(2, [booking("a1", 24, 2), booking("a2", 26, 2)], pending={"a2"})
    results = orch.check_clashes(EVENT, ["COL-6N"])
    check("no agent_actions or approvals were created", inserted("tool_called") == [] and inserted("agent_action_id") == [])
    check("it is written to the log as 'clash found, hold already proposed'",
          len(logs) == 1 and logs[0]["outcome"] == "clash found, hold already proposed")
    check("the result says so", results[0]["outcome"] == "clash found, hold already proposed")

    print("\n== 5. two products clashing on the SAME booking make only one proposal ==")
    reset(0, [booking("a1", 24, 1)])
    results = orch.check_clashes(EVENT, ["COL-6N", "COL-7N"])
    check("one proposal and one 'already proposed' note", [r["outcome"] for r in results] ==
          ["clash found, hold proposed, waiting for approval", "clash found, hold already proposed"])
    check("only one agent_actions row", len(inserted("tool_called")) == 1)

    print("\n== 6. a product that does not exist is ignored ==")
    reset(2, [booking("a1", 24, 2)]); state["stock"] = None
    check("no result, no rows", orch.check_clashes(EVENT, ["NOPE"]) == [] and inserted("tool_called") == [])


def cap():
    print("\n== 6b. at most MAX_HOLDS_PER_EVENT (3) holds per event, earliest first ==")
    check("MAX_HOLDS_PER_EVENT is 3", orch.MAX_HOLDS_PER_EVENT == 3)
    five = [booking(f"b{n}", 10 + n, 2, "Full Colour") for n in range(1, 6)]   # b1 earliest ... b5 latest
    reset(0, five)
    results = orch.check_clashes(EVENT, ["COL-6N"])
    proposals = [r for r in results if r["outcome"].startswith("clash found, hold proposed")]
    check("5 clashes -> only 3 holds are proposed", len(proposals) == 3 and len(inserted("tool_called")) == 3)
    check("they are the 3 EARLIEST bookings (b1, b2, b3)", [r["appointment_id"] for r in proposals] == ["b1", "b2", "b3"])
    check("3 approvals, all 'pending'", len(inserted("agent_action_id")) == 3 and all(a["status"] == "pending" for a in inserted("agent_action_id")))
    cap_logs = [l for l in logs if l["outcome"] == "clash limit reached, more holds not proposed"]
    check("ONE extra log row says how many were not proposed (2)", len(cap_logs) == 1 and "2 more clash(es)" in cap_logs[0]["reasoning"])
    print("   cap log:", cap_logs[0]["reasoning"][:170], "...")
    check("the log names the 2 bookings that were left out", cap_logs[0]["reasoning"].count("Full Colour") == 2)
    check("3 hold logs + 1 cap log = 4 rows in total", len(logs) == 4)

    reset(0, five[:3])
    results = orch.check_clashes(EVENT, ["COL-6N"])
    check("exactly 3 clashes -> 3 holds and NO cap log row", len(inserted("tool_called")) == 3
          and not any(l["outcome"].startswith("clash limit") for l in logs))

    print("   two products: the cap counts ALL products together, earliest booking first")
    reset(0, [booking("late", 30, 1), booking("early", 11, 1), booking("mid", 20, 1), booking("mid2", 21, 1)])
    results = orch.check_clashes(EVENT, ["COL-6N", "COL-7N"])
    proposals = [r["appointment_id"] for r in results if r["outcome"].startswith("clash found, hold proposed")]
    check("still only 3 holds in total, and they are the earliest ones", proposals == ["early", "mid", "mid2"])

    print("   a hold that is already pending does not use up the cap")
    reset(0, five, pending={"b1"})
    results = orch.check_clashes(EVENT, ["COL-6N"])
    proposals = [r["appointment_id"] for r in results if r["outcome"].startswith("clash found, hold proposed")]
    check("b1 is 'already proposed', and 3 NEW holds are still proposed (b2, b3, b4)", proposals == ["b2", "b3", "b4"])


def routing():
    print("\n== 7. routing ==")
    check("stock_low and shelf_observation -> inventory_agent, staff_absent -> scheduling_agent first (unchanged)",
          orch.ROUTES["stock_low"] == ["inventory_agent"] and orch.ROUTES["shelf_observation"] == ["inventory_agent"]
          and orch.ROUTES["staff_absent"][0] == "scheduling_agent")
    check("staff_absent and queue_overload -> scheduling_agent THEN cx_agent (the CX Agent drafts the notices)",
          orch.ROUTES["staff_absent"] == ["scheduling_agent", "cx_agent"] and orch.ROUTES["queue_overload"] == ["scheduling_agent", "cx_agent"])
    check("complaint_received -> quality_agent THEN cx_agent", orch.ROUTES["complaint_received"] == ["quality_agent", "cx_agent"])
    check("is_routed is True for routed types", all(orch.is_routed({"type": t}) for t in orch.ROUTES))
    check("an unrouted type (booking_created) is NOT routed, so the worker skips it",
          not orch.is_routed({"type": "booking_created"}) and not orch.is_routed({"type": "something_new"}))

    handled, statuses = [], []
    fake_inventory = lambda ev: handled.append(("inventory", ev["id"])) or {
        "status": "handled", "results": [{"sku": "COL-6N", "outcome": "draft PO created, waiting for approval"}]}
    fake_scheduling = lambda ev: handled.append(("scheduling", ev["id"])) or {
        "status": "handled", "results": [{"label": "Men's Cut 09:30", "outcome": "reassignment proposed, waiting for approval"}]}
    fake_cx = lambda ev, context: handled.append(("cx", ev["id"])) or {"status": "handled", "results": []}
    orch.AGENTS = {"inventory_agent": fake_inventory, "scheduling_agent": fake_scheduling, "cx_agent": fake_cx}

    print("\n== 8. the clash check runs after the Inventory Agent ==")
    reset(2, [booking("a1", 24, 2), booking("a2", 26, 2)])
    summary = orch.handle_event(EVENT)
    print("   outcomes:", [r["outcome"] for r in summary["results"]])
    check("stock_low went to the Inventory Agent", handled == [("inventory", "ev-1")] and summary["routed_to"] == "inventory_agent")
    check("the summary has the agent's result AND the clash result",
          [r["outcome"] for r in summary["results"]] == ["draft PO created, waiting for approval",
                                                         "clash found, hold proposed, waiting for approval"])

    reset(2, [booking("a1", 24, 2), booking("a2", 26, 2)])
    orch.AGENTS["inventory_agent"] = lambda ev: {"status": "handled", "results": [
        {"sku": "COL-6N", "outcome": "skipped: open purchase order already exists"}]}
    summary = orch.handle_event(EVENT)
    check("the clash check STILL runs when the Inventory Agent skipped (an order already exists)",
          any(r["outcome"].startswith("clash found") for r in summary["results"]))

    reset(2, [booking("a1", 24, 2), booking("a2", 26, 2)])
    orch.AGENTS["inventory_agent"] = lambda ev: {"status": "failed", "error": "x", "results": []}
    summary = orch.handle_event(EVENT)
    check("no clash check if the Inventory Agent failed", inserted("tool_called") == [] and summary["status"] == "failed")

    reset(2, [booking("a1", 24, 2), booking("a2", 26, 2)])
    summary = orch.handle_event({"id": "ev-2", "type": "staff_absent", "payload": {}})
    check("staff_absent goes to the Scheduling Agent then the CX Agent, with no clash check",
          ("scheduling", "ev-2") in handled and ("cx", "ev-2") in handled and summary["routed_to"] == "scheduling_agent + cx_agent"
          and inserted("tool_called") == [])

    print("\n== 9. failures ==")
    def crash(ev): raise RuntimeError("boom, the agent crashed")
    orch.AGENTS["inventory_agent"] = crash
    reset(2, [booking("a1", 24, 2)])
    summary = orch.handle_event(EVENT)
    check("a crashing agent -> summary 'failed', event set to 'failed'",
          summary["status"] == "failed" and any(c.args == ({"status": "failed"},) for c in fake_db.table.return_value.update.call_args_list))
    check("the reason is logged with outcome 'event failed'", logs[-1]["outcome"] == "event failed" and "boom, the agent crashed" in logs[-1]["reasoning"]
          and logs[-1]["agents"] == ["orchestrator", "inventory_agent"])

    orch.AGENTS["inventory_agent"] = fake_inventory
    reset(2, [booking("a1", 24, 2)])
    orch.get_upcoming_bookings = lambda *a: (_ for _ in ()).throw(RuntimeError("clash check exploded"))
    summary = orch.handle_event(EVENT)
    check("if the clash check itself crashes the event is marked failed and the reason logged",
          summary["status"] == "failed" and "clash check exploded" in logs[-1]["reasoning"] and logs[-1]["outcome"] == "event failed")


if __name__ == "__main__":
    rules()
    lookahead()
    proposals()
    cap()
    routing()
    print("\nALL OFFLINE ORCHESTRATOR CHECKS PASSED (no database rows written)")
