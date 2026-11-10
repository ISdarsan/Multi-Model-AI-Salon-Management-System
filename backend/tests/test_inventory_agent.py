"""
test_inventory_agent.py - a simple script that tests the Inventory Agent end to end.

What it does:
  1. Creates a stock_low event for COL-6N, runs the agent, prints what was written,
     APPROVES the draft order, then checks that an approved order blocks a new draft.
  2. Creates a stock_low event for COL-7N, runs the agent, checks that a second COL-7N event
     creates no duplicate order, then REJECTS the order.
  3. Deletes the rows it created.

Run it from the project root:   python -m backend.tests.test_inventory_agent

IMPORTANT about cleanup: decision_log is append-only (the database refuses to delete
its rows, even for this script), and an event that has decision_log rows cannot be
deleted either. So this script deletes the purchase orders, agent actions and approvals
it created, but its 4 test events (source = 'inventory_test') and their decision_log
rows stay (the README explains how to remove them by hand if you want to).
"""

from backend.agents.inventory_agent import run
from backend.approvals.approval_actions import approve, reject
from backend.db import supabase
from backend.tools.inventory_tools import get_open_purchase_order, get_stock

TEST_SOURCE = "inventory_test"   # marks the events this script creates

# Ids of rows created by this script, so we can clean up exactly these and nothing else.
created = {"events": [], "agent_actions": [], "purchase_orders": [], "approvals": []}


def heading(text):
    print("\n" + "=" * 70 + "\n" + text + "\n" + "=" * 70)


def show(label, row, fields):
    """Print selected fields of a row on one line."""
    print(f"  {label}: " + ", ".join(f"{f}={row.get(f)!r}" for f in fields))


def create_event(sku):
    """Create a stock_low event shaped like docs/SCHEMA.md says."""
    stock = get_stock(sku=sku)
    event = supabase.table("events").insert({
        "type": "stock_low",
        "source": TEST_SOURCE,
        "payload": {"product_id": stock["product_id"], "sku": sku,
                    "current_qty": stock["current_qty"], "reorder_threshold": stock["reorder_threshold"]},
        "status": "new",
    }).execute().data[0]
    created["events"].append(event["id"])
    print(f"Created event {event['id']} for {sku} (type={event['type']}, status={event['status']})")
    return event


def run_agent_for(event):
    """Run the agent for exactly this event and print what it wrote."""
    summaries = run(event_ids=[event["id"]])
    assert len(summaries) == 1, "the agent should have handled exactly one event"
    summary = summaries[0]
    print(f"Agent finished: event status = {summary['status']}")
    result = summary["results"][0]
    print(f"  outcome: {result['outcome']}")
    print(f"  reason : {result['detail']}")
    if summary["status"] != "handled" or "purchase_order_id" not in result:
        raise SystemExit("The agent did not create a purchase order. Test stopped.")

    created["purchase_orders"].append(result["purchase_order_id"])
    created["agent_actions"].append(result["agent_action_id"])
    created["approvals"].append(result["approval_id"])

    po = supabase.table("purchase_orders").select("*").eq("id", result["purchase_order_id"]).execute().data[0]
    action = supabase.table("agent_actions").select("*").eq("id", result["agent_action_id"]).execute().data[0]
    approval = supabase.table("approvals").select("*").eq("id", result["approval_id"]).execute().data[0]
    logs = supabase.table("decision_log").select("*").eq("event_id", event["id"]).order("created_at").execute().data

    print("\nRows the agent wrote:")
    show("purchase_orders", po, ["status", "qty", "supplier", "created_by_agent"])
    show("agent_actions  ", action, ["agent_name", "tool_called", "risk_tier", "status"])
    print(f"     input : {action['input']}")
    show("approvals      ", approval, ["status", "decided_by", "decided_at"])
    for log in logs:
        show("decision_log   ", log, ["agents_involved", "outcome"])
        print(f"     reasoning: {log['reasoning']}")
    return approval["id"], po["id"], action["id"], event["id"]


def print_changes(label, changes):
    print(f"\n{label} - what changed:")
    show("approvals      ", changes["approval"], ["status", "decided_by", "decided_at", "reason"])
    show("purchase_orders", changes["purchase_order"], ["status"])
    show("agent_actions  ", changes["agent_action"], ["status"])
    show("decision_log   ", changes["decision_log"], ["agents_involved", "outcome"])
    print(f"     reasoning: {changes['decision_log']['reasoning']}")


def main():
    # Get ready: we need a manager to approve/reject, and no leftover draft orders for our products.
    manager = supabase.table("profiles").select("id, full_name").eq("role", "manager").limit(1).execute().data
    if not manager:
        raise SystemExit("No manager profile found in the database.")
    manager_id = manager[0]["id"]
    for sku in ("COL-6N", "COL-7N"):
        leftover = get_open_purchase_order(get_stock(sku=sku)["product_id"])
        if leftover:
            raise SystemExit(f"A draft purchase order already exists for {sku} ({leftover['id']}). "
                             "Approve, reject or delete it first, then run the test again.")
    print(f"Using manager: {manager[0]['full_name']} ({manager_id})")

    try:
        heading("TEST 1: COL-6N low stock -> draft PO -> APPROVE")
        approval_id, po_id, action_id, event_id = run_agent_for(create_event("COL-6N"))
        changes = approve(approval_id, manager_id)
        print_changes("approve()", changes)
        assert changes["approval"]["status"] == "approved"
        assert changes["purchase_order"]["status"] == "approved"
        assert changes["agent_action"]["status"] == "executed"

        print("\nThe order is now 'approved'. A NEW low-stock event for COL-6N must not create another draft...")
        again = create_event("COL-6N")
        again_result = run(event_ids=[again["id"]])[0]["results"][0]
        print(f"  outcome: {again_result['outcome']}")
        print(f"  reason : {again_result['detail']}")
        assert again_result["outcome"] == "skipped: open purchase order already exists"
        orders = (supabase.table("purchase_orders").select("id, status")
                  .eq("product_id", get_stock(sku="COL-6N")["product_id"]).in_("status", ["draft", "approved", "ordered"])
                  .execute().data)
        assert len(orders) == 1, "there must still be only ONE open order for COL-6N"
        print("  still exactly one open purchase order for COL-6N (status 'approved')")

        heading("TEST 2: COL-7N low stock -> draft PO -> duplicate is skipped -> REJECT")
        approval_id, po_id, action_id, event_id = run_agent_for(create_event("COL-7N"))

        print("\nNow a SECOND low-stock event for COL-7N arrives while the draft is still waiting...")
        second = create_event("COL-7N")
        second_summary = run(event_ids=[second["id"]])[0]
        second_result = second_summary["results"][0]
        print(f"  outcome: {second_result['outcome']}")
        print(f"  reason : {second_result['detail']}")
        assert second_result["outcome"] == "skipped: open purchase order already exists"
        drafts = (supabase.table("purchase_orders").select("id")
                  .eq("product_id", get_stock(sku="COL-7N")["product_id"]).eq("status", "draft").execute().data)
        assert len(drafts) == 1, "there must still be only ONE draft order for COL-7N"
        print("  still exactly one draft purchase order for COL-7N")

        changes = reject(approval_id, manager_id, "Supplier price too high this week, wait for next quote")
        print_changes("reject()", changes)
        assert changes["approval"]["status"] == "rejected"
        assert changes["purchase_order"]["status"] == "rejected"
        assert changes["agent_action"]["status"] == "rejected"

        print("\nALL CHECKS PASSED")
    finally:
        heading("CLEANUP")
        # Children first, because of the links between tables (approvals -> agent_actions -> purchase_orders).
        for table in ("approvals", "agent_actions", "purchase_orders"):
            for row_id in created[table]:
                supabase.table(table).delete().eq("id", row_id).execute()
            print(f"  deleted {len(created[table])} row(s) from {table}")
        remaining = (supabase.table("decision_log").select("id")
                     .in_("event_id", created["events"]).execute().data) if created["events"] else []
        print(f"  KEPT {len(created['events'])} test event(s) and {len(remaining)} decision_log row(s): "
              "decision_log is append-only, so the database refuses to delete them.")
        print("  The events are easy to spot: source = 'inventory_test'. Your hello_agent rows were not touched.")


if __name__ == "__main__":
    main()
