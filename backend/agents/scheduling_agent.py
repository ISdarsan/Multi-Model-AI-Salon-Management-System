"""
scheduling_agent.py - the Scheduling Agent.

Its job: when a stylist is marked absent (a `staff_absent` event), find a replacement for
each of their upcoming appointments and PROPOSE a reassignment for a manager to approve.

All decisions are made by plain Python rules (see choose_replacement below). The AI
(Ollama) is used ONLY to write a short plain-English reason. If the AI fails, a template
sentence is used, so the agent never fails because of the AI.

IMPORTANT: this agent never changes an appointment. It only writes proposals. The
appointment changes later, when a manager approves (see backend/approvals/approval_actions.py).

Times: appointments are stored in UTC and the database has no salon time zone, so weekdays
and shift hours are read in UTC (the same way the demo data was created).

Run it from the project root:   python -m backend.agents.scheduling_agent
"""

import os
import re
from datetime import datetime, time, timedelta, timezone

from backend.db import add_decision_log, claim_event, supabase
from backend.proposals import pending_input_values, propose
from backend.tools.scheduling_tools import (
    BLOCKING_STATUSES,
    get_appointments,
    get_day_appointments,
    get_next_booked_appointments,
    get_staff,
    required_skill,
)

AGENT_NAME = "scheduling_agent"
EVENT_TYPES = ["staff_absent"]   # the events this agent picks up on its own (queue_overload comes through the Orchestrator)


# ----------------------------------------------------------------------------
# 1. Small time helpers
# ----------------------------------------------------------------------------

def parse_dt(text: str) -> datetime:
    """Turn '2026-11-12T09:30:00+00:00' (or ending in Z) into a datetime in UTC."""
    text = text.replace("Z", "+00:00")
    # Python 3.10 only accepts 3 or 6 decimal digits of seconds, so pad any fraction to 6.
    text = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], text)
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)   # no time zone given: assume UTC
    return value.astimezone(timezone.utc)


def hhmm(value: datetime) -> str:
    return value.strftime("%H:%M")


# ----------------------------------------------------------------------------
# 2. The rules (plain Python, no AI, no database)
# ----------------------------------------------------------------------------

def choose_replacement(appointment: dict, skill: str, staff_list: list, day_appointments: list,
                       absent_staff_id: str):
    """
    Pick a replacement stylist for ONE appointment.

    A stylist fits only if ALL of these are true:
      - is not the absent stylist and is_available = true
      - works that weekday, and the whole slot is inside their shift
      - has the skill the service needs
      - has no overlapping appointment (booked, held or in_progress)
    If several fit, the one with the FEWEST appointments that day wins (ties: alphabetical).

    day_appointments = everyone's non-cancelled appointments on that day.
    Returns (chosen, checks):
      chosen = the winning check dict, or None if nobody fits
      checks = one dict per stylist: {"staff", "ok", "reason", "appointments_that_day"}
    """
    start = parse_dt(appointment["start_time"])
    end = parse_dt(appointment["end_time"])
    checks = []

    for person in sorted(staff_list, key=lambda s: s["name"]):
        shift_start = time.fromisoformat(person["shift_start"])
        shift_end = time.fromisoformat(person["shift_end"])
        reason = None

        if person["id"] == absent_staff_id:
            reason = "is the absent stylist"
        elif not person["is_available"]:
            reason = "is marked unavailable"
        elif start.isoweekday() not in person["work_days"]:
            reason = f"does not work on {start:%A}"
        elif end.date() != start.date() or start.time() < shift_start or end.time() > shift_end:
            reason = f"shift {hhmm_time(shift_start)}-{hhmm_time(shift_end)} does not cover {hhmm(start)}-{hhmm(end)}"
        elif skill not in person["skills"]:
            reason = f"does not have the '{skill}' skill"
        else:
            # Two time ranges overlap when each one starts before the other one ends.
            for other in day_appointments:
                if other["staff_id"] != person["id"] or other["status"] not in BLOCKING_STATUSES:
                    continue
                other_start, other_end = parse_dt(other["start_time"]), parse_dt(other["end_time"])
                if other_start < end and start < other_end:
                    reason = f"already has an appointment {hhmm(other_start)}-{hhmm(other_end)} ({other['status']})"
                    break

        # How busy is this stylist that day? (every non-cancelled appointment counts)
        count = sum(1 for other in day_appointments if other["staff_id"] == person["id"])
        checks.append({"staff": person, "ok": reason is None,
                       "reason": reason or "free and qualified", "appointments_that_day": count})

    fitting = [c for c in checks if c["ok"]]
    chosen = min(fitting, key=lambda c: (c["appointments_that_day"], c["staff"]["name"])) if fitting else None
    return chosen, checks


def hhmm_time(value: time) -> str:
    return value.strftime("%H:%M")


# ----------------------------------------------------------------------------
# 3. The small AI part: write a 2-sentence reason (with a safe fallback)
# ----------------------------------------------------------------------------

def write_reason(old_name: str, new_name: str, service: str, start: datetime, skill: str, count: int) -> str:
    """Return a short plain-English reason. Never raises: falls back to a template."""
    others = f"{count} other appointment{'' if count == 1 else 's'} that day"
    template = (
        f"{old_name} is absent, so {new_name} takes the {hhmm(start)} {service} because "
        f"{new_name} has the {skill} skill and a free slot ({others})."
    )
    try:
        # Imported here so that even a broken AI library cannot stop the agent.
        from langchain_ollama import ChatOllama

        model = os.getenv("OLLAMA_MODEL")
        if os.getenv("LLM_PROVIDER", "ollama") != "ollama" or not model:
            return template

        llm = ChatOllama(model=model, temperature=0, client_kwargs={"timeout": 60})
        prompt = (
            "Write exactly two short plain-English sentences explaining why a salon appointment "
            "was reassigned to another stylist. Reply with ONLY the two sentences, no introduction. "
            "Use only these facts and do not invent anything.\n"
            f"Absent stylist: {old_name}\n"
            f"Replacement stylist: {new_name}\n"
            f"Service: {service} at {hhmm(start)}\n"
            f"Skill needed (the replacement has it): {skill}\n"
            f"The replacement has a free slot and {others}."
        )
        reply = str(llm.invoke(prompt).content)
        # Models often add an intro like "Here are two sentences:". Keep only the real sentences.
        lines = [line.strip() for line in reply.splitlines() if line.strip() and not line.strip().endswith(":")]
        reply = " ".join(lines)
        if reply:
            return reply
    except Exception:
        pass  # any AI problem -> use the template below
    return template


# ----------------------------------------------------------------------------
# 4. Handling ONE appointment: find a replacement, then write the proposal rows
# ----------------------------------------------------------------------------

def propose_reassignment(event: dict, appointment: dict, absent: dict, chosen: dict, skill: str,
                         start: datetime) -> dict:
    """Write the agent_actions, approvals and decision_log rows for ONE proposal. The appointment is NOT changed."""
    new = chosen["staff"]
    service = appointment["services"]["name"]
    reason = write_reason(absent["name"], new["name"], service, start, skill, chosen["appointments_that_day"])

    # agent_actions: what the agent proposes. Medium risk, so it waits for approval.
    action = supabase.table("agent_actions").insert({
        "event_id": event["id"],
        "agent_name": AGENT_NAME,
        "tool_called": "reassign_staff",
        "input": {
            "appointment_id": appointment["id"],
            "service": service,
            "start_time": appointment["start_time"],
            "end_time": appointment["end_time"],
            "required_skill": skill,
            "old_staff_id": absent["id"],
            "old_staff_name": absent["name"],
            "new_staff_id": new["id"],
            "new_staff_name": new["name"],
            "new_staff_appointments_that_day": chosen["appointments_that_day"],
        },
        "result": {"proposed": True, "applied": False, "appointment_id": appointment["id"],
                   "new_staff_id": new["id"]},
        "risk_tier": "medium",
        "status": "awaiting_approval",
    }).execute().data[0]

    try:
        # approvals: the manager's inbox. It starts as 'pending'.
        approval = supabase.table("approvals").insert({
            "agent_action_id": action["id"],
            "status": "pending",
        }).execute().data[0]
        # decision_log: the permanent record of why.
        add_decision_log(event["id"], [AGENT_NAME], reason, "reassignment proposed, waiting for approval")
    except Exception:
        # Do not leave a half-finished proposal behind.
        supabase.table("approvals").delete().eq("agent_action_id", action["id"]).execute()
        supabase.table("agent_actions").delete().eq("id", action["id"]).execute()
        raise

    return {"label": f"{service} {hhmm(start)}", "outcome": "reassignment proposed, waiting for approval",
            "detail": reason, "appointment_id": appointment["id"], "new_staff": new["name"],
            # (these extra details let the CX Agent draft a notice for the customer)
            "service": service, "start_time": appointment["start_time"], "old_staff": absent["name"],
            "customer_id": appointment.get("customer_id"),
            "agent_action_id": action["id"], "approval_id": approval["id"]}


def handle_appointment(event: dict, appointment: dict, absent: dict, staff_list: list, day_cache: dict) -> dict:
    """Find a replacement for one appointment and write either a proposal or a 'manager must decide' log."""
    start = parse_dt(appointment["start_time"])
    service = appointment["services"]["name"]
    skill = required_skill(service)

    # All appointments that day (loaded once per day, then reused).
    day = start.date()
    if day not in day_cache:
        day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        day_cache[day] = get_day_appointments(day_start.isoformat(), (day_start + timedelta(days=1)).isoformat())

    if skill is None:
        chosen, checks = None, []
        why = f"no skill is defined for the service '{service}', so a replacement cannot be chosen"
    else:
        chosen, checks = choose_replacement(appointment, skill, staff_list, day_cache[day], absent["id"])
        why = "; ".join(f"{c['staff']['name']} {c['reason']}" for c in checks)

    if chosen:
        result = propose_reassignment(event, appointment, absent, chosen, skill, start)
        # Remember this proposal, so the next appointment sees this stylist as busier at that time.
        day_cache[day].append({"id": appointment["id"], "staff_id": chosen["staff"]["id"],
                               "start_time": appointment["start_time"], "end_time": appointment["end_time"],
                               "status": "booked"})
        return result

    text = (f"{absent['name']}'s {hhmm(start)} {service} on {start:%Y-%m-%d} has no replacement: {why}. "
            "No reassignment was proposed.")
    add_decision_log(event["id"], [AGENT_NAME], text, "no replacement found, manager must decide")
    return {"label": f"{service} {hhmm(start)}", "outcome": "no replacement found, manager must decide",
            "detail": text, "appointment_id": appointment["id"]}


# ----------------------------------------------------------------------------
# 4b. Waiting area overloaded (scenario 5): propose moving the next few appointments later
# ----------------------------------------------------------------------------

MAX_SHIFTS = 3            # shift at most this many appointments per event (the next ones to start)
DEFAULT_CAPACITY = 4      # how many people the waiting area holds, if the event does not say
SHIFT_OUTCOME = "shift proposed, waiting for approval"
SHIFT_AGENTS = ["scheduling_agent", "cx_agent"]


def choose_shift_minutes(waiting_count: int, capacity, avg_wait_minutes) -> int:
    """
    How far to move appointments later, by a plain rule:
      30 minutes if 3 or more people are over capacity, or the average wait is already 30 minutes or more;
      otherwise 15 minutes.
    """
    excess = waiting_count - (capacity or DEFAULT_CAPACITY)
    return 30 if excess >= 3 or (avg_wait_minutes or 0) >= 30 else 15


def plan_shift(appointment: dict, preferred_minutes: int, staff_member, day_appointments: list):
    """
    Work out whether ONE appointment can move later without breaking anything.

    Tries the preferred number of minutes first, then 15 minutes as a smaller fallback. A shift is only
    allowed if the same stylist has NO overlapping appointment (booked, held or in_progress) in the new
    time, and the appointment still ends within the stylist's shift on the same day.
    Returns (plan, None) with plan = {"minutes", "new_start", "new_end"}, or (None, the reasons it cannot move).
    """
    start, end = parse_dt(appointment["start_time"]), parse_dt(appointment["end_time"])
    shift_end = time.fromisoformat(staff_member["shift_end"]) if staff_member else time(23, 59)
    options = [preferred_minutes] + ([15] if preferred_minutes > 15 else [])
    reasons = []

    for minutes in options:
        new_start, new_end = start + timedelta(minutes=minutes), end + timedelta(minutes=minutes)
        if new_end.date() != start.date() or new_end.time() > shift_end:
            reasons.append(f"{minutes} min later would end after the shift ({hhmm_time(shift_end)})")
            continue
        clash = next((other for other in day_appointments
                      if other["staff_id"] == appointment["staff_id"] and other["id"] != appointment["id"]
                      and other["status"] in BLOCKING_STATUSES
                      and parse_dt(other["start_time"]) < new_end and new_start < parse_dt(other["end_time"])), None)
        if clash:
            reasons.append(f"{minutes} min later would overlap the {hhmm(parse_dt(clash['start_time']))}-"
                           f"{hhmm(parse_dt(clash['end_time']))} appointment")
            continue
        return {"minutes": minutes, "new_start": new_start, "new_end": new_end}, None
    return None, "; ".join(reasons)


def handle_queue_overload(event: dict, context: dict) -> dict:
    """
    A queue_overload event: propose moving the next few booked appointments later (medium risk, 'shift_appointment').
    NOTHING is changed here: a manager must approve each shift. The proposals are put in context["shifts"]
    so the CX Agent can draft a notice for each affected customer. Does not change the event's status.
    """
    payload = event["payload"] or {}
    waiting = payload.get("waiting_count")
    if not isinstance(waiting, int) or waiting <= 0:
        raise ValueError("queue_overload payload needs a 'waiting_count' above 0")
    # "The next appointments" are counted from the event's time (optional payload "time", otherwise when it was created).
    when = parse_dt(payload["time"]) if payload.get("time") else parse_dt(event["created_at"])
    minutes = choose_shift_minutes(waiting, payload.get("capacity"), payload.get("avg_wait_minutes"))
    capacity = payload.get("capacity") or DEFAULT_CAPACITY

    results, shifts = [], []
    appointments = get_next_booked_appointments(when.isoformat(), MAX_SHIFTS)
    if not appointments:
        why = f"The waiting area is overloaded ({waiting} waiting), but there are no booked appointments after {when:%Y-%m-%d %H:%M} to move."
        add_decision_log(event["id"], SHIFT_AGENTS, why, "no appointments to shift")
        results.append({"label": "-", "outcome": "no appointments to shift", "detail": why})
    else:
        staff_by_id = {member["id"]: member for member in get_staff()}
        pending = pending_input_values("shift_appointment", "appointment_id")   # shifts already waiting
        day_cache = {}
        for appointment in appointments:
            start = parse_dt(appointment["start_time"])
            service = appointment["services"]["name"]
            label = f"{service} {hhmm(start)}"
            staff_member = staff_by_id.get(appointment["staff_id"])
            stylist = staff_member["name"] if staff_member else "the stylist"

            if appointment["id"] in pending:
                why = f"The {service} at {hhmm(start)} already has a shift proposal waiting for approval, so no new one was made."
                add_decision_log(event["id"], SHIFT_AGENTS, why, "shift already proposed")
                results.append({"label": label, "outcome": "shift already proposed", "detail": why})
                continue

            # The stylist's day, loaded once. Each shift is checked against the appointments AS THEY ARE NOW,
            # so every proposal stands on its own and can be approved in any order.
            day = start.date()
            if day not in day_cache:
                day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
                day_cache[day] = get_day_appointments(day_start.isoformat(), (day_start + timedelta(days=1)).isoformat())
            plan, why_not = plan_shift(appointment, minutes, staff_member, day_cache[day])

            if not plan:
                why = f"The {service} at {hhmm(start)} with {stylist} cannot be moved safely: {why_not}. No shift was proposed."
                add_decision_log(event["id"], SHIFT_AGENTS, why, "no safe shift found, manager must decide")
                results.append({"label": label, "outcome": "no safe shift found, manager must decide", "detail": why})
                continue

            reasoning = (f"The waiting area is overloaded ({waiting} waiting, capacity {capacity}). The {service} with {stylist} "
                         f"is proposed to start {plan['minutes']} minutes later ({hhmm(start)} to {hhmm(plan['new_start'])}). "
                         f"{stylist} has no other booking in that time and it still ends within the shift.")
            action, approval = propose(
                event["id"], AGENT_NAME, "shift_appointment", "medium",
                {"appointment_id": appointment["id"], "service": service, "customer_id": appointment.get("customer_id"),
                 "staff_id": appointment["staff_id"], "staff_name": stylist,
                 "old_start": appointment["start_time"], "old_end": appointment["end_time"],
                 "new_start": plan["new_start"].isoformat(), "new_end": plan["new_end"].isoformat(),
                 "minutes": plan["minutes"], "waiting_count": waiting},
                {"proposed": True, "applied": False, "appointment_id": appointment["id"]},
                reasoning, SHIFT_OUTCOME, SHIFT_AGENTS,
            )
            shifts.append({"appointment_id": appointment["id"], "service": service,
                           "customer_id": appointment.get("customer_id"), "old_start": appointment["start_time"],
                           "new_start": plan["new_start"].isoformat(), "minutes": plan["minutes"]})
            results.append({"label": label, "outcome": SHIFT_OUTCOME, "detail": reasoning,
                            "appointment_id": appointment["id"], "agent_action_id": action["id"],
                            "approval_id": approval["id"]})

    context["shifts"] = shifts
    return {"status": "handled", "results": results}


# ----------------------------------------------------------------------------
# 5. Handling one event, and running the agent
# ----------------------------------------------------------------------------

def set_event_status(event_id: str, status: str):
    supabase.table("events").update({"status": status}).eq("id", event_id).execute()


def handle_event(event: dict, context: dict = None) -> dict:
    """
    Do the agent's work for ONE event that is already marked 'processing'.
    The caller (run() below, or backend/worker.py) must claim the event first.

    staff_absent   -> propose replacements. Always returns a summary; on failure the event is marked 'failed'.
    queue_overload -> propose shifting the next appointments later (the Orchestrator sets the event status).
    `context` is shared with the other agents that run for the same event.
    """
    if event["type"] == "queue_overload":
        return handle_queue_overload(event, context if context is not None else {})

    try:
        # The payload shape is frozen in docs/SCHEMA.md: staff_id, from, to, reason.
        payload = event["payload"] or {}
        for key in ("staff_id", "from", "to"):
            if not payload.get(key):
                raise ValueError(f"staff_absent payload needs '{key}'")
        window_start, window_end = parse_dt(payload["from"]), parse_dt(payload["to"])
        if window_end < window_start:
            raise ValueError("'to' must not be before 'from'")

        absent = get_staff(payload["staff_id"])
        if not absent:
            raise ValueError(f"stylist not found: {payload['staff_id']}")

        # Only appointments that start in the future (and inside the absence window) matter.
        earliest = max(window_start, datetime.now(timezone.utc))
        appointments = get_appointments(absent["id"], earliest.isoformat(), window_end.isoformat())

        results = []
        if not appointments:
            why = f"{absent['name']} has no booked or held appointments to reassign in that period."
            add_decision_log(event["id"], [AGENT_NAME], why, "no appointments to reassign")
            results.append({"label": "-", "outcome": "no appointments to reassign", "detail": why})
        else:
            staff_list, day_cache = get_staff(), {}
            for appointment in appointments:
                results.append(handle_appointment(event, appointment, absent, staff_list, day_cache))

        # Tell the CX Agent which customers' appointments were proposed to move, so it can draft notices.
        if context is not None:
            context["reassignments"] = [r for r in results if r.get("agent_action_id")]

        set_event_status(event["id"], "handled")
        return {"event_id": event["id"], "type": event["type"], "status": "handled", "results": results}

    except Exception as error:
        # Mark the event failed and write down why (decision_log is where reasons live).
        set_event_status(event["id"], "failed")
        try:
            add_decision_log(event["id"], [AGENT_NAME], f"Event failed: {type(error).__name__}: {error}",
                             "event failed")
        except Exception:
            pass  # if even the log fails, the 'failed' status is still set
        return {"event_id": event["id"], "type": event["type"], "status": "failed", "error": str(error),
                "results": []}


def process_event(event: dict):
    """Claim one event and handle it. Returns a summary, or None if someone else took it."""
    if not claim_event(event["id"]):
        return None
    return handle_event(event)


def run(event_ids: list = None) -> list:
    """
    Process every 'new' staff_absent event (oldest first).
    Pass event_ids to process only those events.
    """
    query = (supabase.table("events").select("*").eq("status", "new")
             .in_("type", EVENT_TYPES).order("created_at"))
    if event_ids:
        query = query.in_("id", event_ids)

    summaries = []
    for event in query.execute().data:
        summary = process_event(event)
        if summary:
            summaries.append(summary)
    return summaries


if __name__ == "__main__":
    summaries = run()
    if not summaries:
        print("No new staff_absent events to handle.")
    for summary in summaries:
        print(f"Event {summary['event_id']} ({summary['type']}): {summary['status']}")
        if summary["status"] == "failed":
            print(f"   FAILED: {summary['error']}")
        for result in summary["results"]:
            print(f"   {result['label']}: {result['outcome']}")
            print(f"      {result['detail']}")
