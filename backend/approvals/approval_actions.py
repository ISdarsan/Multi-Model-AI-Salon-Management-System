"""
approval_actions.py - what happens when a manager approves or rejects a proposal.

Two functions, which work for ALL agents (the action's tool_called decides what happens):
  approve(approval_id, decided_by)          -> the proposal is carried out and the action counts as executed
  reject(approval_id, decided_by, reason)   -> nothing is carried out and the reason is recorded

  create_draft_po (Inventory Agent): approve -> the purchase order becomes 'approved'.
                                     reject  -> the purchase order becomes 'rejected'.
  reassign_staff (Scheduling Agent): approve -> the appointment moves to the new stylist.
                                     reject  -> the appointment is NOT changed.
  hold_appointment (Orchestrator):   approve -> the appointment becomes 'held' (refused if it is no longer 'booked').
                                     reject  -> the appointment is NOT changed.

Both also ADD a row to decision_log. Old decision_log rows are never edited or deleted.

Note: the database cannot run several updates as one all-or-nothing step from here, so
the approval row is switched first. That "claims" it: a second click on the same
approval is refused instead of being applied twice.
"""

from backend.db import add_decision_log, now_iso, supabase
from backend.tools.orchestrator_tools import hold_appointment, release_hold
from backend.tools.scheduling_tools import get_appointment, reassign_staff


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
    if action["tool_called"] == "reassign_staff":
        return _approve_reassignment(approval_id, decided_by, action)
    if action["tool_called"] == "hold_appointment":
        return _approve_hold(approval_id, decided_by, action)

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
    if action["tool_called"] == "reassign_staff":
        return _reject_reassignment(approval_id, decided_by, action, reason.strip())
    if action["tool_called"] == "hold_appointment":
        return _reject_hold(approval_id, decided_by, action, reason.strip())

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


# ----------------------------------------------------------------------------
# Scheduling Agent proposals (tool_called = 'reassign_staff')
# ----------------------------------------------------------------------------

def _approve_reassignment(approval_id: str, decided_by: str, action: dict) -> dict:
    """Carry out an approved reassignment: the appointment moves to the new stylist."""
    info = action["input"]

    # The proposal was made some time ago. Check the appointment is still as the agent saw it.
    appointment = get_appointment(info["appointment_id"])
    if not appointment:
        raise ValueError("the appointment no longer exists, so it cannot be reassigned")
    if appointment["status"] not in ("booked", "held"):
        raise ValueError(f"the appointment is now '{appointment['status']}', so it cannot be reassigned")
    if appointment["staff_id"] != info["old_staff_id"]:
        raise ValueError("the appointment was already changed since this was proposed")

    # Change the appointment FIRST. If the new stylist became busy in the meantime, the database
    # refuses and we stop here with the approval still pending (nothing is half done).
    appointment_row = reassign_staff(info["appointment_id"], info["new_staff_id"])

    try:
        approval_row = _decide(approval_id, "approved", decided_by)
    except Exception:
        # Someone else decided at the same moment: put the appointment back.
        reassign_staff(info["appointment_id"], info["old_staff_id"])
        raise

    result = {**(action.get("result") or {}), "applied": True}
    action_row = (supabase.table("agent_actions").update({"status": "executed", "result": result})
                  .eq("id", action["id"]).execute().data[0])
    log_row = add_decision_log(
        action["event_id"], [action["agent_name"]],
        f"Manager {decided_by} approved moving the {info['service']} on {info['start_time']} "
        f"from {info['old_staff_name']} to {info['new_staff_name']}. The appointment was changed.",
        "reassignment approved and applied",
    )
    return {"approval": approval_row, "appointment": appointment_row, "agent_action": action_row,
            "decision_log": log_row}


def _reject_reassignment(approval_id: str, decided_by: str, action: dict, reason: str) -> dict:
    """A rejected reassignment changes nothing on the appointment. Only the reason is recorded."""
    info = action["input"]

    approval_row = _decide(approval_id, "rejected", decided_by, reason)
    action_row = supabase.table("agent_actions").update({"status": "rejected"}).eq("id", action["id"]).execute().data[0]
    log_row = add_decision_log(
        action["event_id"], [action["agent_name"]],
        f"Manager {decided_by} rejected moving the {info['service']} on {info['start_time']} "
        f"from {info['old_staff_name']} to {info['new_staff_name']}. Reason: {reason}",
        "reassignment rejected",
    )
    # The appointment is returned exactly as it is now, to show that it was not touched.
    return {"approval": approval_row, "appointment": get_appointment(info["appointment_id"]),
            "agent_action": action_row, "decision_log": log_row}


# ----------------------------------------------------------------------------
# Orchestrator proposals (tool_called = 'hold_appointment')
# ----------------------------------------------------------------------------

def _approve_hold(approval_id: str, decided_by: str, action: dict) -> dict:
    """Carry out an approved hold: the appointment becomes 'held'."""
    info = action["input"]

    # The proposal was made some time ago. The appointment must still be 'booked'.
    appointment = get_appointment(info["appointment_id"])
    if not appointment:
        raise ValueError("the appointment no longer exists, so it cannot be put on hold")
    if appointment["status"] != "booked":
        raise ValueError(f"the appointment is now '{appointment['status']}', not 'booked', so it cannot be put on hold")

    # Change the appointment FIRST. If it fails, we stop here and the approval stays pending.
    appointment_row = hold_appointment(info["appointment_id"])

    try:
        approval_row = _decide(approval_id, "approved", decided_by)
    except Exception:
        # Someone else decided at the same moment: put the appointment back.
        release_hold(info["appointment_id"])
        raise

    result = {**(action.get("result") or {}), "applied": True}
    action_row = (supabase.table("agent_actions").update({"status": "executed", "result": result})
                  .eq("id", action["id"]).execute().data[0])
    log_row = add_decision_log(
        action["event_id"], [action["agent_name"]],
        f"Manager {decided_by} approved putting the {info['service']} on {info['start_time']} on hold "
        f"because of low {info['sku']} stock. The appointment is now 'held'.",
        "hold approved and applied",
    )
    return {"approval": approval_row, "appointment": appointment_row, "agent_action": action_row,
            "decision_log": log_row}


def _reject_hold(approval_id: str, decided_by: str, action: dict, reason: str) -> dict:
    """A rejected hold changes nothing on the appointment. Only the reason is recorded."""
    info = action["input"]

    approval_row = _decide(approval_id, "rejected", decided_by, reason)
    action_row = supabase.table("agent_actions").update({"status": "rejected"}).eq("id", action["id"]).execute().data[0]
    log_row = add_decision_log(
        action["event_id"], [action["agent_name"]],
        f"Manager {decided_by} rejected putting the {info['service']} on {info['start_time']} on hold. "
        f"Reason: {reason}",
        "hold rejected",
    )
    # The appointment is returned exactly as it is now, to show that it was not touched.
    return {"approval": approval_row, "appointment": get_appointment(info["appointment_id"]),
            "agent_action": action_row, "decision_log": log_row}
