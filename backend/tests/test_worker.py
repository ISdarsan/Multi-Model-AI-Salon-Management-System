"""
test_worker.py - checks the worker's rules OFFLINE. It never touches the database.

It feeds the worker a fake list of events and fake agents, and replaces the database
with a recorder, so we can see exactly what the worker WOULD do.

Run it from the project root:   python -m backend.tests.test_worker
"""

import io
from contextlib import redirect_stdout
from unittest.mock import MagicMock

import backend.worker as worker

# ----------------------------------------------------------------------------
# Fakes: a recorder instead of the database, and agents that do nothing real
# ----------------------------------------------------------------------------

calls = {"agent_ran_for": [], "claimed": [], "failed_events": [], "decision_logs": []}


def fake_claim(event_id):
    calls["claimed"].append(event_id)
    return event_id != "e-taken"        # pretend another worker already took 'e-taken'


def fake_inventory_agent(event):
    calls["agent_ran_for"].append(event["id"])
    return {"status": "handled", "results": [{"sku": "COL-6N", "outcome": "draft PO created, waiting for approval"}]}


def crashing_agent(event):
    calls["agent_ran_for"].append(event["id"])
    raise RuntimeError("boom, the agent crashed")


fake_db = MagicMock()   # stands in for supabase; we look at what the worker asks it to do
worker.supabase = fake_db
worker.claim_event = fake_claim
worker.add_decision_log = lambda event_id, agents, reasoning, outcome: calls["decision_logs"].append(
    (event_id, agents, reasoning, outcome))

worker.ROUTES = {"stock_low": "inventory_agent", "shelf_observation": "crashy_agent"}
worker.AGENTS = {"inventory_agent": fake_inventory_agent, "crashy_agent": crashing_agent}


def event(event_id, type_, payload=None):
    return {"id": event_id, "type": type_, "payload": payload or {}, "status": "new"}


def run_handle(events):
    """Run the worker's logic on a list of events and return what it printed."""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        worker.handle_events(events)
    return buffer.getvalue()


def check(label, condition):
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    assert condition, label


# ----------------------------------------------------------------------------
# The checks
# ----------------------------------------------------------------------------

def main():
    print("== 1. stock_low goes to the inventory agent ==")
    out = run_handle([event("e1", "stock_low", {"sku": "COL-6N"})])
    print("   printed:", out.strip())
    check("agent ran for e1", calls["agent_ran_for"] == ["e1"])
    check("event was claimed ('processing') BEFORE the agent ran", calls["claimed"] == ["e1"])
    check("line shows type, sku, agent and outcome",
          "event stock_low (COL-6N) -> inventory_agent -> draft PO created, waiting for approval" in out)
    check("line starts with a HH:MM:SS time", out[2] == ":" and out[5] == ":" and out[8] == " ")

    print("\n== 2. a type with no agent is skipped, and only reported ONCE ==")
    calls["agent_ran_for"].clear(); calls["claimed"].clear()
    queued = event("e2", "queue_overload", {"waiting_count": 7})
    first = run_handle([queued])
    second = run_handle([queued])     # the worker looks again 3 seconds later
    print("   first look :", first.strip())
    print("   second look:", repr(second))
    check("first look prints one skipped line", first.count("no agent yet") == 1)
    check("second look prints nothing", second == "")
    check("agent was never run and event was never claimed or changed",
          calls["agent_ran_for"] == [] and calls["claimed"] == [])
    check("db was not asked to change anything", fake_db.table.call_count == 0)

    print("\n== 3. a crashing agent marks the event failed, and the worker keeps going ==")
    calls["agent_ran_for"].clear(); calls["claimed"].clear()
    crash = event("e3", "shelf_observation", {"observations": [{"sku": "SKN-001", "observed_count": 1}]})
    after = event("e4", "stock_low", {"sku": "COL-7N"})
    out = run_handle([crash, after])
    print("   printed:\n     " + out.strip().replace("\n", "\n     "))
    check("crashing agent was run for e3", "e3" in calls["agent_ran_for"])
    check("event e3 was set to 'failed'",
          any(c.args == ({"status": "failed"},) for c in fake_db.table.return_value.update.call_args_list))
    check("the reason was written to decision_log",
          len(calls["decision_logs"]) == 1 and calls["decision_logs"][0][0] == "e3"
          and "boom, the agent crashed" in calls["decision_logs"][0][2] and calls["decision_logs"][0][3] == "event failed")
    check("failure line printed with the reason", "FAILED: RuntimeError: boom, the agent crashed" in out)
    check("worker carried on: the NEXT event (e4) was still handled", "e4" in calls["agent_ran_for"])

    print("\n== 4. an event another worker already took is left alone ==")
    calls["agent_ran_for"].clear()
    out = run_handle([event("e-taken", "stock_low", {"sku": "COL-6N"})])
    check("no agent run and nothing printed", calls["agent_ran_for"] == [] and out == "")

    print("\n== 5. events are handled oldest first (in the order they are given) ==")
    calls["agent_ran_for"].clear()
    run_handle([event("a", "stock_low"), event("b", "stock_low"), event("c", "stock_low")])
    check("order a, b, c", calls["agent_ran_for"] == ["a", "b", "c"])

    print("\n== 6. the main loop survives a database hiccup, and stops cleanly ==")
    state = {"looks": 0}

    def flaky_fetch():
        state["looks"] += 1
        if state["looks"] == 1:
            raise ConnectionError("internet dropped")
        if state["looks"] == 3:
            worker.stop_requested = True     # same flag that Ctrl+C sets
        return []

    worker.fetch_new_events = flaky_fetch
    worker.POLL_SECONDS = 0.25
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        worker.main()
    out = buffer.getvalue()
    print("   printed:\n     " + out.strip().replace("\n", "\n     "))
    check("problem was reported, not fatal", "problem while checking for events" in out)
    check("worker kept looking after the problem (3 looks)", state["looks"] == 3)
    check("worker said it stopped cleanly", "worker stopped." in out)

    print("\nALL OFFLINE WORKER CHECKS PASSED (no database rows written)")


if __name__ == "__main__":
    main()
