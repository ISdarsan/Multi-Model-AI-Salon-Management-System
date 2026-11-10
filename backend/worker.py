"""
worker.py - runs forever and hands new events to the Orchestrator.

Every 3 seconds it looks in the `events` table for rows with status 'new' (oldest first).
For each one it asks the Orchestrator whether any agent handles that event type. If so, it marks
the event 'processing' (so it can never be picked up twice) and gives it to the Orchestrator,
which decides which agent runs (see ROUTES in backend/agents/orchestrator.py).
Events with no agent yet are left alone.

Start it from the project root:   python -m backend.worker
Stop it with Ctrl+C (it finishes the event it is working on first).
"""

import signal
import time
from datetime import datetime

from backend.agents import orchestrator
from backend.db import add_decision_log, claim_event, supabase

# ----------------------------------------------------------------------------
# 1. Settings
# ----------------------------------------------------------------------------

POLL_SECONDS = 3       # how often to look for new events
MAX_EVENTS = 200       # how many new events to read per look

# Ids of events we already printed a "skipped" line for, so we say it only ONCE per event.
already_reported = set()

# Set to True when you press Ctrl+C. The loop checks it and stops cleanly.
stop_requested = False


# ----------------------------------------------------------------------------
# 2. Small helpers
# ----------------------------------------------------------------------------

def say(message: str):
    """Print one line with the current time, like:  14:05:12 event stock_low ..."""
    print(f"{datetime.now().strftime('%H:%M:%S')} {message}", flush=True)


def describe(event: dict) -> str:
    """A short label for an event: its type plus the product(s) it is about, e.g. 'stock_low (COL-6N)'."""
    payload = event.get("payload") or {}
    skus = []
    if payload.get("sku"):
        skus.append(payload["sku"])
    for obs in payload.get("observations") or []:
        if isinstance(obs, dict) and obs.get("sku"):
            skus.append(obs["sku"])
    return f"{event['type']} ({', '.join(skus)})" if skus else event["type"]


def fetch_new_events() -> list:
    """Read the 'new' events, oldest first."""
    return (supabase.table("events").select("*").eq("status", "new")
            .order("created_at").limit(MAX_EVENTS).execute().data)


def mark_failed(event: dict, agent_name: str, error: Exception):
    # (A safety net: the Orchestrator already handles agent crashes itself.)
    """Mark an event 'failed' and write the reason to decision_log. Never raises."""
    try:
        supabase.table("events").update({"status": "failed"}).eq("id", event["id"]).execute()
    except Exception:
        pass
    try:
        add_decision_log(event["id"], [agent_name],
                         f"Worker: the Orchestrator crashed: {type(error).__name__}: {error}", "event failed")
    except Exception:
        pass  # if even the log fails, the 'failed' status is still set


def outcome_text(summary: dict) -> str:
    """Turn an agent's summary into a short phrase for the log line."""
    if summary.get("status") == "failed":
        return f"FAILED: {summary.get('error')}"
    results = summary.get("results") or []
    if len(results) == 1:
        return results[0]["outcome"]
    # Each result has a short name: a product sku (Inventory Agent) or a label (Scheduling Agent, Orchestrator).
    return "; ".join(f"{r.get('sku') or r.get('label') or '-'}: {r['outcome']}" for r in results) or "handled"


# ----------------------------------------------------------------------------
# 3. The main logic: handle the events that were found
# ----------------------------------------------------------------------------

def handle_events(events: list):
    """Go through a list of new events, oldest first, and give each to the Orchestrator."""
    for event in events:
        if stop_requested:
            break  # Ctrl+C was pressed: do not start any more events

        # No agent for this type yet: leave the event as 'new' and say so only once.
        if not orchestrator.is_routed(event):
            if event["id"] not in already_reported:
                already_reported.add(event["id"])
                say(f"event {describe(event)} -> no agent yet, left as 'new'")
            continue

        # Mark it 'processing' first. If someone else got there first, skip it.
        if not claim_event(event["id"]):
            continue

        # Give it to the Orchestrator. Nothing that goes wrong may stop the worker.
        try:
            summary = orchestrator.handle_event(event)
            say(f"event {describe(event)} -> orchestrator -> {summary.get('routed_to')} -> {outcome_text(summary)}")
        except Exception as error:
            mark_failed(event, "orchestrator", error)
            say(f"event {describe(event)} -> orchestrator -> FAILED: {type(error).__name__}: {error}")


# ----------------------------------------------------------------------------
# 4. The forever loop and clean stopping
# ----------------------------------------------------------------------------

def request_stop(signum, frame):
    """Called when you press Ctrl+C. We only set a flag, so the current event can finish."""
    global stop_requested
    if stop_requested:
        raise KeyboardInterrupt  # pressed twice: stop right now
    stop_requested = True
    say("stopping after the current event (press Ctrl+C again to force)...")


def main():
    # Ctrl+C (SIGINT) and, where the system has them, the other usual stop signals.
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), request_stop)

    say(f"worker started. Looking for new events every {POLL_SECONDS}s. Routes: {orchestrator.ROUTES}. Press Ctrl+C to stop.")
    last_problem = None

    while not stop_requested:
        try:
            events = fetch_new_events()
            # Forget events that are no longer 'new', so the "already reported" list stays small.
            already_reported.intersection_update(e["id"] for e in events)
            handle_events(events)
            last_problem = None
        except Exception as error:
            # A hiccup (for example the internet dropping) must not kill the worker. Say it once, retry.
            problem = f"{type(error).__name__}: {error}"
            if problem != last_problem:
                say(f"problem while checking for events, will keep trying: {problem}")
                last_problem = problem

        # Sleep in short steps so Ctrl+C is noticed quickly.
        waited = 0.0
        while waited < POLL_SECONDS and not stop_requested:
            time.sleep(0.25)
            waited += 0.25

    say("worker stopped.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("worker stopped (forced).")
