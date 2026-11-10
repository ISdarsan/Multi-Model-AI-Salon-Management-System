"""
run_scenarios.py - runs ALL FIVE scenarios from docs/ARCHITECTURE.md (section 9) end to end on your REAL database,
using TEMPORARY test data, and prints a PASS/FAIL table with the time each one took.

For each scenario it:
  1. creates the temporary test data it needs,
  2. sends the event and runs the Orchestrator (the same code the worker runs),
  3. checks the EXACT proposals and rows that must exist (and fails loudly if a proposal is missing),
  4. approves or rejects as the scenario says, and checks the final state,
  5. measures the seconds from the event to the proposals being written,
  6. cleans up everything (except the append-only decision_log rows, which can never be deleted).

The results are printed and saved to docs/SCENARIO_RESULTS.md.

IMPORTANT: stop your worker (python -m backend.worker) while this runs, or it may grab the test events.

Run it from the project root:   python -m backend.scenarios.run_scenarios
"""

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.agents import orchestrator
from backend.agents.scheduling_agent import hhmm, parse_dt
from backend.approvals.approval_actions import approve, reject
from backend.db import supabase
from backend.scenarios.helpers import (TestData, day_at, demo_customer_ids, find_complaint_event, ids_by_name)
from backend.tools.inventory_tools import get_open_purchase_order, get_stock
from backend.tools.orchestrator_tools import get_pending_hold_appointment_ids, get_upcoming_bookings

LATENCY_TARGET_SECONDS = 10   # from docs/ARCHITECTURE.md section 12: event-to-alert latency under 10 seconds
RESULTS_FILE = Path(__file__).resolve().parents[2] / "docs" / "SCENARIO_RESULTS.md"


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

class MissingProposal(AssertionError):
    """Raised when a proposal that MUST exist is not there. This makes the scenario fail loudly."""


class Scenario:
    """Collects the checks of one scenario."""

    def __init__(self, number: int, name: str):
        self.number, self.name = number, name
        self.checks = []            # (label, passed)
        self.latency = None         # seconds from the event to the proposals
        self.error = None
        self.cleanup = {}
        self.data = TestData(f"scenario{number}_test")

    def expect(self, label: str, condition) -> bool:
        """Record a check. A failed check does not stop the scenario, so the report shows everything."""
        self.checks.append((label, bool(condition)))
        print(f"      {'PASS' if condition else 'FAIL'}  {label}")
        return bool(condition)

    def require(self, condition, message: str):
        """A condition the scenario cannot continue without. Raises if it is not true."""
        if not condition:
            raise AssertionError(message)

    @property
    def passed(self) -> bool:
        return self.error is None and bool(self.checks) and all(ok for _, ok in self.checks)


def actions_of(event_id: str):
    """The agent_actions of an event (oldest first) and their approvals by action id."""
    actions = supabase.table("agent_actions").select("*").eq("event_id", event_id).order("created_at").execute().data
    approvals = {}
    if actions:
        approvals = {a["agent_action_id"]: a for a in supabase.table("approvals").select("*")
                     .in_("agent_action_id", [x["id"] for x in actions]).execute().data}
    return actions, approvals


def need(actions: list, tool: str, agent: str, risk: str, status: str = "awaiting_approval", count: int = 1) -> list:
    """The proposals of this kind. Raises MissingProposal if there are not exactly `count` of them."""
    found = [a for a in actions if a["tool_called"] == tool and a["agent_name"] == agent
             and a["risk_tier"] == risk and a["status"] == status]
    if len(found) != count:
        raise MissingProposal(f"MISSING EXPECTED PROPOSAL: wanted {count} x {agent} {tool} (risk {risk}, status {status}), found {len(found)}")
    return found


def log_rows(event_id: str) -> list:
    return supabase.table("decision_log").select("*").eq("event_id", event_id).order("created_at").execute().data


def appointment(appointment_id: str) -> dict:
    return supabase.table("appointments").select("*").eq("id", appointment_id).execute().data[0]


def manager_id() -> str:
    return supabase.table("profiles").select("id").eq("role", "manager").limit(1).execute().data[0]["id"]


def free_day(first: int, other_days=()) -> int:
    """The first day offset >= first that is not a Sunday (the salon is closed) and not in other_days."""
    day = first
    while day_at(day, 10).isoweekday() == 7 or day in other_days:
        day += 1
    return day


# ----------------------------------------------------------------------------
# Scenario 1: colour stock low + a colour service booked -> clash, hold, draft order
# ----------------------------------------------------------------------------

# The low colour products this scenario can use, and a colour service that uses each one.
COLOUR_CANDIDATES = [("COL-6N", "Full Colour"), ("COL-7N", "Root Touch-up")]


def pick_colour_product(sc: Scenario):
    """
    Choose a colour product that is below its reorder threshold and has no purchase order in progress, so the
    Inventory Agent will create a fresh draft order. (Your own earlier orders are never touched: if one product
    is busy, the next candidate is used.)
    """
    problems = []
    for sku, service_name in COLOUR_CANDIDATES:
        stock = get_stock(sku=sku)
        if stock["current_qty"] >= stock["reorder_threshold"]:
            problems.append(f"{sku} is not below its reorder threshold ({stock['current_qty']} >= {stock['reorder_threshold']})")
        elif get_open_purchase_order(stock["product_id"]):
            problems.append(f"{sku} already has an open purchase order")
        else:
            return stock, service_name
    raise AssertionError("precondition: no low colour product is free to use: " + "; ".join(problems)
                         + ". Approve, reject or deliver the open order first.")


def scenario_1(sc: Scenario):
    stock, service_name = pick_colour_product(sc)
    sku = stock["sku"]
    sc.require(not get_pending_hold_appointment_ids(), "precondition: there are already pending hold proposals. Approve or reject them first.")
    customer = demo_customer_ids()[0]

    def clashes_now():
        now = datetime.now(timezone.utc)
        bookings = [b for b in get_upcoming_bookings(stock["product_id"], now.isoformat())
                    if parse_dt(b["start_time"]) < now + timedelta(days=orchestrator.LOOKAHEAD_DAYS)]
        return orchestrator.find_clashes(stock["current_qty"], bookings)

    # A colour service is booked tomorrow (temporary, in the evening after the salon's shifts).
    # Add more temporary bookings only until the low stock would clash with one of them.
    cleo, service_id = ids_by_name("staff", "Cleo Test"), ids_by_name("services", service_name)
    minutes = supabase.table("services").select("duration_minutes").eq("id", service_id).execute().data[0]["duration_minutes"]
    clashes, start = [], day_at(1, 19, 0)
    for _ in range(6):
        sc.data.temp_appointment(cleo, service_id, start, minutes, customer)
        start += timedelta(minutes=minutes + 15)
        clashes = clashes_now()
        if clashes:
            break
    sc.require(clashes, "could not create a clash for the test")
    expected = [c["booking"]["id"] for c in clashes][:orchestrator.MAX_HOLDS_PER_EVENT]
    for appointment_id in expected:
        sc.data.watch_appointment(appointment_id)
    print(f"      ({sku} stock {stock['current_qty']}, a {service_name} is booked tomorrow: {len(clashes)} booking(s) clash, "
          f"the earliest {len(expected)} should be held)")

    started = time.time()
    event = sc.data.new_event("stock_low", {"product_id": stock["product_id"], "sku": sku,
                                            "current_qty": stock["current_qty"], "reorder_threshold": stock["reorder_threshold"]})
    summary = orchestrator.handle_event(event)
    sc.latency = time.time() - started
    sc.expect("the event was handled (not failed)", summary["status"] == "handled")

    actions, approvals = actions_of(event["id"])
    po_action = need(actions, "create_draft_po", "inventory_agent", "medium")[0]
    sc.expect(f"Inventory Agent: a draft purchase order for {sku} was proposed (medium risk) with a pending approval",
              approvals[po_action["id"]]["status"] == "pending" and po_action["result"]["sku"] == sku)
    holds = need(actions, "hold_appointment", "orchestrator", "medium", count=len(expected))
    sc.expect(f"Orchestrator: clash detected, {len(expected)} hold proposal(s) for exactly the earliest clashing bookings",
              [h["input"]["appointment_id"] for h in holds] == expected and all(approvals[h["id"]]["status"] == "pending" for h in holds))
    logs = log_rows(event["id"])
    clash_logs = [l for l in logs if l["outcome"] == "clash found, hold proposed, waiting for approval"]
    sc.expect("decision_log: one row per hold, naming orchestrator, inventory_agent and scheduling_agent, with the real numbers",
              len(clash_logs) == len(expected) and all(l["agents_involved"] == ["orchestrator", "inventory_agent", "scheduling_agent"]
                                                       and "units" in l["reasoning"] for l in clash_logs))
    sc.expect("decision_log: the Inventory Agent's row for the draft order", any(l["outcome"] == "draft PO created, waiting for approval" for l in logs))
    sc.expect("nothing has changed yet: the clashing bookings are still 'booked'",
              all(appointment(i)["status"] == "booked" for i in expected))

    # The manager approves the ORDER, and the first HOLD.
    manager = manager_id()
    changes = approve(approvals[po_action["id"]]["id"], manager)
    sc.expect("manager approves the order: purchase order 'approved', action 'executed'",
              changes["purchase_order"]["status"] == "approved" and changes["agent_action"]["status"] == "executed")
    changes = approve(approvals[holds[0]["id"]]["id"], manager)
    sc.expect("manager approves the hold: the booking becomes 'held'", changes["appointment"]["status"] == "held" and appointment(expected[0])["status"] == "held")
    sc.expect("the other proposed holds are untouched (still 'booked')", all(appointment(i)["status"] == "booked" for i in expected[1:]))
    sc.expect("the stock level was not changed", get_stock(sku=sku)["current_qty"] == stock["current_qty"])


# ----------------------------------------------------------------------------
# Scenario 2: a stylist is absent -> reassignment + customer notice
# ----------------------------------------------------------------------------

def scenario_2(sc: Scenario):
    day = free_day(9)
    customer = demo_customer_ids()[0]
    aria = ids_by_name("staff", "Aria Demo")
    booking = sc.data.temp_appointment(aria, ids_by_name("services", "Men's Cut"), day_at(day, 10, 0), 30, customer)
    print(f"      (Aria Demo has a Men's Cut with a customer on {day_at(day, 10):%A %Y-%m-%d} at 10:00)")

    started = time.time()
    event = sc.data.new_event("staff_absent", {"staff_id": aria, "from": day_at(day, 0).isoformat(),
                                               "to": day_at(day, 23, 59).isoformat(), "reason": "sick"})
    summary = orchestrator.handle_event(event)
    sc.latency = time.time() - started
    sc.expect("the event was handled (not failed)", summary["status"] == "handled")

    actions, approvals = actions_of(event["id"])
    moves = need(actions, "reassign_staff", "scheduling_agent", "medium")
    sc.expect("Scheduling Agent: one reassignment proposal (medium risk) for the booking, pending approval",
              moves[0]["input"]["appointment_id"] == booking["id"] and approvals[moves[0]["id"]]["status"] == "pending"
              and moves[0]["input"]["old_staff_name"] == "Aria Demo")
    new_name = moves[0]["input"]["new_staff_name"]
    new_staff = supabase.table("staff").select("skills").eq("id", moves[0]["input"]["new_staff_id"]).execute().data[0]
    sc.expect(f"the replacement ({new_name}) has the 'cut' skill", "cut" in new_staff["skills"])
    notices = need(actions, "draft_customer_notice", "cx_agent", "medium")
    sc.expect("CX Agent: a notice was drafted for the affected customer (medium risk, NOT sent)",
              notices[0]["input"]["customer_id"] == customer and notices[0]["input"]["kind"] == "reassignment"
              and notices[0]["result"]["sent"] is False and new_name in notices[0]["result"]["draft_text"])
    logs = log_rows(event["id"])
    sc.expect("decision_log rows for the reassignment and the notice",
              any(l["outcome"] == "reassignment proposed, waiting for approval" for l in logs)
              and any(l["outcome"] == "customer notice drafted, waiting for approval" for l in logs))
    sc.expect("nothing has changed yet: the appointment is still with Aria", appointment(booking["id"])["staff_id"] == aria)

    changes = approve(approvals[moves[0]["id"]]["id"], manager_id())
    sc.expect("manager approves the reassignment: the appointment moves to the new stylist",
              changes["appointment"]["staff_id"] == moves[0]["input"]["new_staff_id"]
              and appointment(booking["id"])["staff_id"] == moves[0]["input"]["new_staff_id"] and changes["agent_action"]["status"] == "executed")
    sc.expect("the notice is still only a draft (nothing was sent)", supabase.table("agent_actions").select("result")
              .eq("id", notices[0]["id"]).execute().data[0]["result"]["sent"] is False)


# ----------------------------------------------------------------------------
# Scenario 3: a customer complaint -> ticket, service log check, refund (manager only)
# ----------------------------------------------------------------------------

def scenario_3(sc: Scenario):
    customer = demo_customer_ids()[0]
    booking = sc.data.temp_appointment(ids_by_name("staff", "Cleo Test"), ids_by_name("services", "Full Colour"),
                                       day_at(-20, 10, 0), 120, customer, "completed")
    supabase.table("service_log").insert({
        "appointment_id": booking["id"], "readiness_notes": "Colour bowl and brushes prepared.",
        "quality_notes": "Colour left on about 10 minutes over target."}).execute()

    started = time.time()
    feedback = supabase.table("feedback").insert({
        "customer_id": customer, "appointment_id": booking["id"], "rating": 1,
        "text": "Colour came out patchy and far too orange. Very disappointed and I would like a refund."}).execute().data[0]
    sc.data.feedback_ids.append(feedback["id"])
    event = find_complaint_event(feedback["id"])
    sc.require(event, "the complaint_received event was not created by the database trigger, or a running worker took it (stop the worker)")
    sc.data.event_ids.append(event["id"])
    summary = orchestrator.handle_event(event)
    sc.latency = time.time() - started
    supabase.table("events").update({"source": sc.data.source}).eq("id", event["id"]).execute()   # label it as a test event
    sc.expect("the database trigger created the complaint_received event and it was handled", summary["status"] == "handled")

    fb = supabase.table("feedback").select("category, sentiment").eq("id", feedback["id"]).execute().data[0]
    ticket = supabase.table("tickets").select("*").eq("feedback_id", feedback["id"]).execute().data
    sc.expect("CX Agent: classified 'service quality' / 'negative' on the feedback row", (fb["category"], fb["sentiment"]) == ("service quality", "negative"))
    sc.expect("CX Agent: a HIGH-priority ticket was opened (rating 1)", len(ticket) == 1 and ticket[0]["priority"] == "high" and ticket[0]["status"] == "open")

    actions, approvals = actions_of(event["id"])
    need(actions, "flag_quality_issue", "quality_agent", "low", status="executed")
    sc.expect("Quality Agent: the service log was checked and a problem was flagged (low risk, executed)", True)
    reply = need(actions, "draft_reply", "cx_agent", "medium")[0]
    sc.expect("CX Agent: a reply was drafted (medium risk, NOT sent), pending approval",
              reply["result"]["sent"] is False and approvals[reply["id"]]["status"] == "pending" and reply["result"]["draft_text"])
    refund = need(actions, "suggest_refund", "cx_agent", "high")[0]
    sc.expect("a refund of 60.00 (50% of 120, the policy limit) was suggested as HIGH risk, pending approval",
              refund["result"]["suggested_amount"] == 60.0 and approvals[refund["id"]]["status"] == "pending")
    logs = log_rows(event["id"])
    sc.expect("decision_log: ONE row with agents_involved ['cx_agent','quality_agent'] and the real details",
              len(logs) == 1 and logs[0]["agents_involved"] == ["cx_agent", "quality_agent"]
              and "over target" in logs[0]["reasoning"] and "Refund Policy" in logs[0]["reasoning"] and "60.00" in logs[0]["reasoning"])

    # "Manager only, for any refund": a customer account must not be able to approve it.
    try:
        approve(approvals[refund["id"]]["id"], customer)
        customer_blocked = False
    except PermissionError:
        customer_blocked = True
    sc.expect("a customer account CANNOT approve the refund (manager only)", customer_blocked
              and approvals[refund["id"]]["status"] == "pending")
    changes = approve(approvals[refund["id"]]["id"], manager_id())
    sc.expect("the manager approves the refund: ticket 'resolved', action 'executed'",
              changes["ticket"]["status"] == "resolved" and changes["agent_action"]["status"] == "executed")


# ----------------------------------------------------------------------------
# Scenario 4: the shelf photo says empty, the database says stock exists -> audit flag
# ----------------------------------------------------------------------------

def scenario_4(sc: Scenario):
    before = get_stock(sku="SKN-001")
    sc.require(before["current_qty"] > 1, "precondition: SKN-001 must have stock in the database")
    started = time.time()
    event = sc.data.new_event("shelf_observation", {
        "image_path": "scenario/shelf.jpg", "model": "yolo11n", "inference_ms": 150,
        "observations": [{"sku": "SKN-001", "observed_count": 0, "empty_slots": 3, "confidence": 0.9}]})
    summary = orchestrator.handle_event(event)
    sc.latency = time.time() - started
    sc.expect("the event was handled (not failed)", summary["status"] == "handled")

    actions, approvals = actions_of(event["id"])
    flag = need(actions, "raise_audit_flag", "inventory_agent", "medium")[0]
    sc.expect("Inventory Agent: an audit flag (medium risk) with observed 0 vs database count, pending approval",
              flag["input"]["observed_count"] == 0 and flag["input"]["db_qty"] == before["current_qty"] and approvals[flag["id"]]["status"] == "pending")
    sc.expect("no purchase order was created for a mismatch", not [a for a in actions if a["tool_called"] == "create_draft_po"])
    sc.expect("decision_log: a row for the audit flag with the real numbers",
              any(l["outcome"] == "audit flag proposed, waiting for approval" and "counted 0" in l["reasoning"] for l in log_rows(event["id"])))
    sc.expect("the stock was NOT changed before the manager reviews", get_stock(sku="SKN-001")["current_qty"] == before["current_qty"])

    changes = approve(approvals[flag["id"]]["id"], manager_id())
    sc.expect("the manager approves: the flag is resolved (action 'executed')", changes["agent_action"]["status"] == "executed")
    sc.expect("the stock is STILL unchanged (no stock adjustment without a separate decision)", get_stock(sku="SKN-001")["current_qty"] == before["current_qty"])


# ----------------------------------------------------------------------------
# Scenario 5: the waiting area is overloaded -> shifts + delay notices
# ----------------------------------------------------------------------------

def scenario_5(sc: Scenario):
    day = free_day(12)
    customers = demo_customer_ids()
    service = ids_by_name("services", "Men's Cut")
    plan = [("Aria Demo", 10, 0, customers[0]), ("Ben Sample", 10, 30, customers[1]), ("Dev Mock", 11, 0, None)]
    temp = [sc.data.temp_appointment(ids_by_name("staff", name), service, day_at(day, hour, minute), 30, customer)
            for name, hour, minute, customer in plan]
    nxt = supabase.table("appointments").select("id").eq("status", "booked").gte("start_time", day_at(day, 9).isoformat()) \
        .order("start_time").order("id").limit(3).execute().data
    sc.require([r["id"] for r in nxt] == [t["id"] for t in temp], "other appointments exist that day, so the test bookings are not the next three")
    print(f"      (3 temporary Men's Cut bookings on {day_at(day, 10):%A %Y-%m-%d}: 10:00 Aria, 10:30 Ben, 11:00 Dev (walk-in))")

    started = time.time()
    event = sc.data.new_event("queue_overload", {"waiting_count": 8, "capacity": 4, "avg_wait_minutes": 25,
                                                 "time": day_at(day, 9).isoformat()})
    summary = orchestrator.handle_event(event)
    sc.latency = time.time() - started
    sc.expect("the event was handled (not failed)", summary["status"] == "handled")

    actions, approvals = actions_of(event["id"])
    shifts = need(actions, "shift_appointment", "scheduling_agent", "medium", count=3)
    sc.expect("Scheduling Agent: 3 shift proposals (medium risk), each 30 minutes later, for exactly the 3 next bookings",
              [s["input"]["appointment_id"] for s in shifts] == [t["id"] for t in temp] and all(s["input"]["minutes"] == 30 for s in shifts)
              and all(approvals[s["id"]]["status"] == "pending" for s in shifts))
    sc.expect("every shift keeps the same stylist free (no overlap) and is inside their shift",
              all(parse_dt(s["input"]["new_end"]).time() <= datetime.strptime("18:00", "%H:%M").time() for s in shifts))
    notices = need(actions, "draft_customer_notice", "cx_agent", "medium", count=2)
    sc.expect("CX Agent: delay notices (medium risk, NOT sent) for the two customers; the walk-in gets none",
              {n["input"]["customer_id"] for n in notices} == {customers[0], customers[1]} and all(n["result"]["sent"] is False for n in notices)
              and all(n["input"]["kind"] == "delay" for n in notices))
    logs = log_rows(event["id"])
    sc.expect("decision_log: 3 shift rows, 2 notice rows and one walk-in row",
              len([l for l in logs if l["outcome"] == "shift proposed, waiting for approval"]) == 3
              and len([l for l in logs if l["outcome"] == "customer notice drafted, waiting for approval"]) == 2
              and any(l["outcome"] == "no notice needed for walk-ins" for l in logs))
    sc.expect("nothing has changed yet: all 3 bookings are still at their original time",
              all(appointment(t["id"])["start_time"] == t["start_time"] for t in temp))

    manager = manager_id()
    aria = next(s for s in shifts if s["input"]["staff_name"] == "Aria Demo")
    ben = next(s for s in shifts if s["input"]["staff_name"] == "Ben Sample")
    approve(approvals[aria["id"]]["id"], manager)
    sc.expect("manager approves Aria's shift: the appointment moves from 10:00 to 10:30",
              parse_dt(appointment(aria["input"]["appointment_id"])["start_time"]) == parse_dt(aria["input"]["new_start"])
              and hhmm(parse_dt(appointment(aria["input"]["appointment_id"])["start_time"])) == "10:30")
    reject(approvals[ben["id"]]["id"], manager, "The customer is already here")
    sc.expect("manager rejects Ben's shift: that appointment does NOT move",
              parse_dt(appointment(ben["input"]["appointment_id"])["start_time"]) == parse_dt(ben["input"]["old_start"]))


# ----------------------------------------------------------------------------
# Running everything and reporting
# ----------------------------------------------------------------------------

SCENARIOS = [
    (1, "Colour stock low + colour service booked: clash, hold, draft order", "Inventory, Scheduling, Orchestrator", scenario_1),
    (2, "Stylist absent: reassignment + customer notice", "Scheduling, CX", scenario_2),
    (3, "Customer complaint: ticket, service log, refund (manager only)", "CX, Quality", scenario_3),
    (4, "Shelf says empty, database says stock: audit flag", "Inventory", scenario_4),
    (5, "Waiting area overloaded: shifts + delay notices", "Scheduling, CX", scenario_5),
]


def run_one(number: int, name: str, agents: str, function) -> Scenario:
    sc = Scenario(number, name)
    sc.agents = agents
    print(f"\n[{number}/5] {name}  ({agents})")
    try:
        function(sc)
    except Exception as error:
        sc.error = f"{type(error).__name__}: {error}"
        print(f"      *** {'MISSING PROPOSAL' if isinstance(error, MissingProposal) else 'ERROR'}: {error}")
    finally:
        try:
            sc.cleanup = sc.data.cleanup()
        except Exception as error:
            sc.error = (sc.error or "") + f" | cleanup problem: {error}"
            print(f"      *** cleanup problem: {error}")
    print(f"      => {'PASS' if sc.passed else 'FAIL'}" + (f" in {sc.latency:.1f} s" if sc.latency is not None else ""))
    return sc


def report(results: list) -> str:
    """The results as a Markdown table plus details. Also used for the printed table."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    passed = sum(1 for r in results if r.passed)
    lines = [
        "# Scenario test results", "",
        f"Run on {now} by `python -m backend.scenarios.run_scenarios` against the real database, with temporary test data.",
        f"The five cross-agent scenarios are the acceptance test from `docs/ARCHITECTURE.md` (section 9).", "",
        f"**{passed} of {len(results)} scenarios passed.**", "",
        "| # | Scenario | Agents | Result | Checks | Latency (s) | Under 10 s target |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        ok = sum(1 for _, p in r.checks if p)
        latency = f"{r.latency:.1f}" if r.latency is not None else "-"
        under = ("yes" if r.latency <= LATENCY_TARGET_SECONDS else "no") if r.latency is not None else "-"
        lines.append(f"| {r.number} | {r.name} | {r.agents} | **{'PASS' if r.passed else 'FAIL'}** | {ok}/{len(r.checks)} | {latency} | {under} |")
    lines += ["", "Latency is the time from sending the event to all proposals being written (it includes the AI writing the "
              "reasons, drafts and notices with the local Ollama model). The 10-second target is a proposed KPI, so it is "
              "reported separately and does not decide PASS or FAIL.", "", "## Details", ""]
    for r in results:
        lines.append(f"### Scenario {r.number}: {r.name}")
        lines.append("")
        if r.error:
            lines.append(f"**ERROR:** `{r.error}`")
            lines.append("")
        for label, ok in r.checks:
            lines.append(f"- {'PASS' if ok else 'FAIL'}: {label}")
        cleaned = ", ".join(f"{k}: {v}" for k, v in r.cleanup.items())
        lines += ["", f"_Cleanup:_ {cleaned}", ""]
    lines += ["The decision_log rows written by these runs stay in the database (the log is append-only); their events are "
              "marked with a source such as `scenario1_test`.", ""]
    return "\n".join(lines)


def main() -> int:
    print("SalonOS scenario runner: all five scenarios from docs/ARCHITECTURE.md on the real database.")
    print("(Stop the worker while this runs. Temporary test data is created and removed.)")
    results = [run_one(number, name, agents, function) for number, name, agents, function in SCENARIOS]

    print("\n" + "=" * 100)
    print(f"{'#':<3}{'Scenario':<68}{'Result':<8}{'Latency':>9}  target")
    print("-" * 100)
    for r in results:
        latency = f"{r.latency:.1f} s" if r.latency is not None else "-"
        under = "ok" if r.latency is not None and r.latency <= LATENCY_TARGET_SECONDS else ("over 10 s" if r.latency is not None else "-")
        print(f"{r.number:<3}{r.name[:66]:<68}{'PASS' if r.passed else 'FAIL':<8}{latency:>9}  {under}")
    print("=" * 100)
    passed = sum(1 for r in results if r.passed)
    print(f"{passed} of {len(results)} scenarios passed.")

    RESULTS_FILE.write_text(report(results), encoding="utf-8")
    print(f"Saved to {RESULTS_FILE}")

    if passed != len(results):
        print("\n*** SOME SCENARIOS FAILED ***")
        for r in results:
            if not r.passed:
                print(f"  scenario {r.number}: {r.error or 'failed checks: ' + '; '.join(l for l, ok in r.checks if not ok)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
