"""
orchestrator.py - the Orchestrator. It is the "traffic controller" between the worker and the agents.

It does two jobs:

  A) ROUTING. The worker gives it every new event. The Orchestrator looks up which agent handles
     that event type (the ROUTES table below) and runs that agent.

  B) CLASH CHECK. After the Inventory Agent has dealt with a stock event, the Orchestrator checks
     whether the low stock would leave an upcoming booking without the product it needs. If so it
     PROPOSES putting that appointment on hold, for a manager to approve.

All decisions are plain Python rules (see find_clashes). The AI (Ollama) is used ONLY to write a
short explanation; if it fails, a template sentence is used.

IMPORTANT: the Orchestrator never changes an appointment itself. It only writes proposals. The
appointment becomes 'held' later, when a manager approves (backend/approvals/approval_actions.py).
"""

import inspect
from datetime import datetime, timedelta, timezone

from backend.agents import cx_agent, inventory_agent, quality_agent, scheduling_agent
from backend.agents.scheduling_agent import parse_dt
from backend.ai import short_explanation
from backend.db import add_decision_log, supabase
from backend.tools.inventory_tools import get_stock
from backend.tools.orchestrator_tools import get_pending_hold_appointment_ids, get_upcoming_bookings

AGENT_NAME = "orchestrator"

LOOKAHEAD_DAYS = 3   # only bookings that start within the next 3 days are checked for a clash
MAX_HOLDS_PER_EVENT = 3   # at most 3 hold proposals per event (earliest bookings first)

# ----------------------------------------------------------------------------
# 1. Routing: which agent handles which event type
# ----------------------------------------------------------------------------

# event type -> the agents that handle it, IN ORDER. Event types that are NOT in this dictionary have no agent yet.
# When several agents are listed they run one after the other and share a `context` dictionary,
# so a later agent can use what an earlier one found (for example the Quality Agent's finding).
ROUTES = {
    "stock_low": ["inventory_agent"],
    "shelf_observation": ["inventory_agent"],
    "staff_absent": ["scheduling_agent", "cx_agent"],    # replacements, then notices for the affected customers
    "complaint_received": ["quality_agent", "cx_agent"],
    "queue_overload": ["scheduling_agent", "cx_agent"],  # shifts, then delay notices for the affected customers
}

# agent name -> the function that handles ONE event that is already marked 'processing'.
# To add an agent later: write its handle_event function, add it here, add its types above.
# (A handle_event function may take an optional second argument called `context`.)
AGENTS = {
    "inventory_agent": inventory_agent.handle_event,
    "scheduling_agent": scheduling_agent.handle_event,
    "quality_agent": quality_agent.handle_event,
    "cx_agent": cx_agent.handle_event,
}

# After the Inventory Agent handles one of these, the Orchestrator runs the clash check.
STOCK_EVENT_TYPES = ["stock_low", "shelf_observation"]

# Everyone who takes part in a clash decision (written to decision_log).
CLASH_AGENTS = ["orchestrator", "inventory_agent", "scheduling_agent"]


def agents_for(event_type: str) -> list:
    """The names of the agents that handle this event type (an empty list if none do)."""
    names = ROUTES.get(event_type) or []
    return [names] if isinstance(names, str) else list(names)


def is_routed(event: dict) -> bool:
    """True if agents handle this event type (and all of them exist). The worker leaves other events alone."""
    names = agents_for(event["type"])
    return bool(names) and all(AGENTS.get(name) is not None for name in names)


def call_agent(agent, event: dict, context: dict) -> dict:
    """Run one agent. Agents that accept a `context` argument get it; older agents only get the event."""
    if "context" in inspect.signature(agent).parameters:
        return agent(event, context=context)
    return agent(event)


# ----------------------------------------------------------------------------
# 2. The clash rule (plain Python, no AI, no database)
# ----------------------------------------------------------------------------

def find_clashes(current_qty: int, bookings: list) -> list:
    """
    Walk through the bookings in start-time order with a running stock number.

      - If the stock covers the booking, subtract what the booking needs.
      - If not, that booking is in CLASH (and nothing is subtracted for it).

    Incoming purchase orders are NOT counted as stock, because they have not arrived yet.

    `bookings` must already be in start-time order, and each one needs a "needs" number.
    Returns one dict per clashing booking: {"booking", "stock_before", "needs"}.
    """
    stock = current_qty
    clashes = []
    for booking in bookings:
        if booking["needs"] <= stock:
            stock -= booking["needs"]
        else:
            clashes.append({"booking": booking, "stock_before": stock, "needs": booking["needs"]})
    return clashes


# ----------------------------------------------------------------------------
# 3. The small AI part: a short explanation for the decision log
# ----------------------------------------------------------------------------

def write_reasoning(sku: str, current_qty: int, clash: dict, service: str, start: datetime) -> str:
    """
    The decision_log reasoning. The real numbers always come first (written by code, not by the AI),
    followed by a short explanation from the AI (or a template sentence if the AI fails).
    """
    when = start.strftime("%a %Y-%m-%d %H:%M")
    facts = (f"Clash on {sku}: {clash['stock_before']} unit(s) would be left when the {service} on {when} "
             f"needs {clash['needs']} (stock now {current_qty}; incoming orders are not counted).")
    template = (f"There is not enough {sku} for this booking, so it is safest to hold it until more stock "
                "arrives. The manager can approve the hold or keep the booking as it is.")
    prompt = (
        "Write exactly two short plain-English sentences explaining why a salon appointment should be put "
        "on hold. Reply with ONLY the two sentences, no introduction. Use only these facts and do not "
        "invent anything.\n"
        f"Product: {sku}\n"
        f"Units left for this booking: {clash['stock_before']}\n"
        f"Units the booking needs: {clash['needs']}\n"
        f"Service and time: {service} on {when}"
    )
    return facts + " " + short_explanation(prompt, template)


# ----------------------------------------------------------------------------
# 4. Proposing a hold for ONE clashing appointment
# ----------------------------------------------------------------------------

def propose_hold(event: dict, sku: str, current_qty: int, clash: dict) -> dict:
    """Write the agent_actions, approvals and decision_log rows for ONE hold proposal. The appointment is NOT changed."""
    booking = clash["booking"]
    service = booking["services"]["name"]
    start = parse_dt(booking["start_time"])
    reasoning = write_reasoning(sku, current_qty, clash, service, start)

    # agent_actions: what the Orchestrator proposes. Medium risk, so it waits for approval.
    action = supabase.table("agent_actions").insert({
        "event_id": event["id"],
        "agent_name": AGENT_NAME,
        "tool_called": "hold_appointment",
        "input": {
            "appointment_id": booking["id"],
            "service": service,
            "start_time": booking["start_time"],
            "sku": sku,
            "stock_now": current_qty,
            "stock_before_this_booking": clash["stock_before"],
            "units_needed": clash["needs"],
            "current_status": booking["status"],
        },
        "result": {"proposed": True, "applied": False, "appointment_id": booking["id"],
                   "new_status": "held"},
        "risk_tier": "medium",
        "status": "awaiting_approval",
    }).execute().data[0]

    try:
        # approvals: the manager's inbox. It starts as 'pending'.
        approval = supabase.table("approvals").insert({
            "agent_action_id": action["id"],
            "status": "pending",
        }).execute().data[0]
        # decision_log: ONE row per clash.
        add_decision_log(event["id"], CLASH_AGENTS, reasoning, "clash found, hold proposed, waiting for approval")
    except Exception:
        # Do not leave a half-finished proposal behind.
        supabase.table("approvals").delete().eq("agent_action_id", action["id"]).execute()
        supabase.table("agent_actions").delete().eq("id", action["id"]).execute()
        raise

    return {"label": f"hold {service} {start:%H:%M}", "outcome": "clash found, hold proposed, waiting for approval",
            "detail": reasoning, "appointment_id": booking["id"],
            "agent_action_id": action["id"], "approval_id": approval["id"]}


# ----------------------------------------------------------------------------
# 5. The clash check for the products an event was about
# ----------------------------------------------------------------------------

def check_clashes(event: dict, skus: list) -> list:
    """
    Run the clash rule for each product (by sku) and write the proposals. Returns a list of result dicts.

    At most MAX_HOLDS_PER_EVENT holds are proposed per event (the EARLIEST bookings first), so a very low
    stock does not flood the manager's inbox. One log row says how many were not proposed.
    """
    now = datetime.now(timezone.utc)
    limit = now + timedelta(days=LOOKAHEAD_DAYS)
    pending = get_pending_hold_appointment_ids()   # appointments that already have a hold waiting
    results = []

    # Step 1: find every clash for every product (nothing is written yet).
    found = []   # (sku, stock now, clash)
    for sku in skus:
        stock = get_stock(sku=sku)
        if not stock:
            continue

        # Upcoming 'booked' appointments that use this product, but only those inside the lookahead window.
        bookings = [b for b in get_upcoming_bookings(stock["product_id"], now.isoformat())
                    if parse_dt(b["start_time"]) < limit]
        for clash in find_clashes(stock["current_qty"], bookings):
            found.append((sku, stock["current_qty"], clash))

    # Step 2: earliest bookings first, so the cap keeps the ones that matter most.
    found.sort(key=lambda item: (parse_dt(item[2]["booking"]["start_time"]), item[2]["booking"]["id"]))

    proposed = 0
    not_proposed = {}   # appointment id -> short description of the clash we did not propose
    for sku, current_qty, clash in found:
        booking = clash["booking"]
        service = booking["services"]["name"]
        start = parse_dt(booking["start_time"])

        # Never create a second proposal for an appointment that already has one waiting.
        if booking["id"] in pending:
            why = (f"Clash on {sku} for the {service} on {start:%a %Y-%m-%d %H:%M}, but a hold "
                   "proposal for this appointment is already waiting for approval, so no new one was made.")
            add_decision_log(event["id"], CLASH_AGENTS, why, "clash found, hold already proposed")
            results.append({"label": f"hold {service} {start:%H:%M}",
                            "outcome": "clash found, hold already proposed", "detail": why})
            continue

        # The cap: after MAX_HOLDS_PER_EVENT proposals, remember the rest but do not propose them.
        if proposed >= MAX_HOLDS_PER_EVENT:
            not_proposed.setdefault(booking["id"], f"{service} {start:%a %Y-%m-%d %H:%M} ({sku})")
            continue

        results.append(propose_hold(event, sku, current_qty, clash))
        pending.add(booking["id"])   # so a second product clashing on the same booking does not double up
        proposed += 1

    # Step 3: one log row saying how many clashes were NOT proposed because of the cap.
    if not_proposed:
        count = len(not_proposed)
        why = (f"{count} more clash(es) were found but no hold was proposed for them, because at most "
               f"{MAX_HOLDS_PER_EVENT} holds are proposed per event (the earliest bookings first). "
               f"Not proposed: {'; '.join(not_proposed.values())}. "
               "They will be checked again the next time a stock event for this product arrives.")
        add_decision_log(event["id"], CLASH_AGENTS, why, "clash limit reached, more holds not proposed")
        results.append({"label": "more holds", "outcome": "clash limit reached, more holds not proposed",
                        "detail": why, "not_proposed": count})

    return results


# ----------------------------------------------------------------------------
# 6. Handling one event
# ----------------------------------------------------------------------------

def fail_event(event: dict, agent_name: str, error: Exception, what: str) -> dict:
    """Mark the event 'failed', write why to decision_log, and return a 'failed' summary. Never raises."""
    try:
        supabase.table("events").update({"status": "failed"}).eq("id", event["id"]).execute()
    except Exception:
        pass
    try:
        add_decision_log(event["id"], [AGENT_NAME, agent_name], f"{what}: {type(error).__name__}: {error}",
                         "event failed")
    except Exception:
        pass  # if even the log fails, the 'failed' status is still set
    return {"event_id": event["id"], "type": event["type"], "status": "failed", "error": str(error),
            "results": [], "routed_to": agent_name}


def handle_event(event: dict) -> dict:
    """
    Handle ONE event that the worker has already marked 'processing'.
    Runs its agents in order (they share a `context`), then (for stock events) runs the clash check.
    Always returns a summary.
    """
    names = agents_for(event["type"])
    if not names or any(AGENTS.get(name) is None for name in names):
        raise ValueError(f"no agent handles events of type '{event['type']}'")

    summary = {"event_id": event["id"], "type": event["type"], "status": "handled", "results": [],
               "routed_to": " + ".join(names)}
    context = {}   # what the agents tell each other while handling this event

    # Step 1: let the specialist agents do their work, one after the other.
    # A crash must not escape: the event is marked failed and the reason is logged.
    for name in names:
        try:
            part = call_agent(AGENTS[name], event, context)
        except Exception as error:
            return fail_event(event, name, error, f"The {name} crashed")
        summary["results"] = summary["results"] + list(part.get("results") or [])
        if part.get("status") == "failed":
            # The agent already marked the event failed and logged why. Stop here.
            return {**summary, "status": "failed", "error": part.get("error")}

    # All agents finished: the event is handled. (Some agents already set this; setting it again is harmless.)
    supabase.table("events").update({"status": "handled"}).eq("id", event["id"]).execute()

    # Step 2: for stock events, check the low products against upcoming bookings. This runs even if
    # the Inventory Agent skipped (for example because an order already exists). It does not run
    # if the Inventory Agent failed, because then we do not know which products are low.
    if event["type"] in STOCK_EVENT_TYPES:
        skus = list(dict.fromkeys(r["sku"] for r in summary["results"] if r.get("sku")))
        try:
            summary["results"] = summary["results"] + check_clashes(event, skus)
        except Exception as error:
            return fail_event(event, names[0], error, "The clash check failed")

    return summary
