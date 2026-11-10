"""
approval_actions.py - what happens when a manager approves or rejects a proposal.

Two functions:
  approve(approval_id, decided_by)          -> the draft order is approved and the action counts as executed
  reject(approval_id, decided_by, reason)   -> the draft order is rejected and the reason is recorded

Both also ADD a row to decision_log. Old decision_log rows are never edited or deleted.

Note: the database cannot run several updates as one all-or-nothing step from here, so
the approval row is switched first. That "claims" it: a second click on the same
approval is refused instead of being applied twice.
"""

from backend.db import add_decision_log, now_iso, supabase


def _load(approval_id: str, decided_by: str):
    """Fetch the approval, its agent action and its purchase order. Raise a clear error if anything is wrong."""
    # Only a manager may decide (the architecture says so).
    profile = supabase.table("profiles").select("role").eq("id", decided_by).execute().data
    if not profile or profile[0]["role"] != "manager":
        raise PermissionError(f"user {decided_by} is not a manager and cannot approve or reject")

    rows = supabase.table("approvals").select("*").eq("id", approval_id).execute().data
    if not rows:
        raise ValueError(f"approval {approval_id} not found")
    approval = rows[0]
    if approval["status"] != "pending":
        raise ValueError(f"approval {approval_id} is already {approval['status']}")

    action = supabase.table("agent_actions").select("*").eq("id", approval["agent_action_id"]).execute().data[0]
    # The agent stored the purchase order's id inside the action's result.
    po_id = (action.get("result") or {}).get("purchase_order_id")
    return approval, action, po_id


def _decide(approval_id: str, status: str, decided_by: str, reason: str = None) -> dict:
    """Switch the approval row, but ONLY if it is still pending. Returns the updated row."""
    updated = (
        supabase.table("approvals")
        .update({"status": status, "decided_by": decided_by, "decided_at": now_iso(), "reason": reason})
        .eq("id", approval_id)
        .eq("status", "pending")
        .execute()
        .data
    )
    if not updated:
        raise ValueError(f"approval {approval_id} was already decided by someone else")
    return updated[0]


def approve(approval_id: str, decided_by: str) -> dict:
    """Approve a pending approval. Returns the rows that changed."""
    approval, action, po_id = _load(approval_id, decided_by)

    approval_row = _decide(approval_id, "approved", decided_by)
    po_row = None
    if po_id:
        po_row = supabase.table("purchase_orders").update({"status": "approved"}).eq("id", po_id).execute().data[0]
    action_row = supabase.table("agent_actions").update({"status": "executed"}).eq("id", action["id"]).execute().data[0]

    sku = (action.get("result") or {}).get("sku", "?")
    qty = (action.get("result") or {}).get("qty", "?")
    log_row = add_decision_log(
        action["event_id"], [action["agent_name"]],
        f"Manager {decided_by} approved the draft purchase order for {sku} ({qty} units).",
        "purchase order approved",
    )
    return {"approval": approval_row, "purchase_order": po_row, "agent_action": action_row, "decision_log": log_row}


def reject(approval_id: str, decided_by: str, reason: str) -> dict:
    """Reject a pending approval and record why. Returns the rows that changed."""
    if not reason or not reason.strip():
        raise ValueError("a reason is required when rejecting")
    approval, action, po_id = _load(approval_id, decided_by)

    approval_row = _decide(approval_id, "rejected", decided_by, reason.strip())
    po_row = None
    if po_id:
        po_row = supabase.table("purchase_orders").update({"status": "rejected"}).eq("id", po_id).execute().data[0]
    action_row = supabase.table("agent_actions").update({"status": "rejected"}).eq("id", action["id"]).execute().data[0]

    sku = (action.get("result") or {}).get("sku", "?")
    log_row = add_decision_log(
        action["event_id"], [action["agent_name"]],
        f"Manager {decided_by} rejected the draft purchase order for {sku}. Reason: {reason.strip()}",
        "purchase order rejected",
    )
    return {"approval": approval_row, "purchase_order": po_row, "agent_action": action_row, "decision_log": log_row}
