"""
orchestrator_tools.py - small database functions ("tools") used by the Orchestrator.

Each function does ONE job and has no AI in it.
"""

from backend.db import supabase


def get_upcoming_bookings(product_id: str, from_iso: str) -> list:
    """
    All 'booked' appointments that start after from_iso and whose service uses this product,
    in start-time order. Each appointment gets two extra keys:
      "needs"        = how many units of the product the service uses (service_products.qty_per_service)
      "services"     = {"name": ...} (the service name)
    The caller decides how far ahead to look.
    """
    uses = (supabase.table("service_products").select("service_id, qty_per_service")
            .eq("product_id", product_id).execute().data)
    if not uses:
        return []  # no service uses this product, so no booking can need it
    needs = {use["service_id"]: use["qty_per_service"] for use in uses}

    rows = (
        supabase.table("appointments")
        .select("*, services(name)")
        .eq("status", "booked")
        .gte("start_time", from_iso)
        .in_("service_id", list(needs))
        .order("start_time")
        .order("id")          # same start time (different stylists): keep a fixed order
        .execute()
        .data
    )
    for row in rows:
        row["needs"] = needs[row["service_id"]]
    return rows


def get_pending_hold_appointment_ids() -> set:
    """The ids of appointments that already have a hold proposal waiting for approval."""
    rows = (supabase.table("agent_actions").select("input")
            .eq("tool_called", "hold_appointment").eq("status", "awaiting_approval").execute().data)
    return {row["input"].get("appointment_id") for row in rows}


def hold_appointment(appointment_id: str) -> dict:
    """
    Put a 'booked' appointment on hold (status 'held'). Only called after a manager approves.
    Refuses if the appointment is no longer 'booked'.
    """
    rows = (supabase.table("appointments").update({"status": "held"})
            .eq("id", appointment_id).eq("status", "booked").execute().data)
    if not rows:
        raise ValueError(f"appointment {appointment_id} is no longer 'booked', so it cannot be put on hold")
    return rows[0]


def release_hold(appointment_id: str) -> dict:
    """Undo hold_appointment (held -> booked). Only used to put things back if an approval fails halfway."""
    rows = (supabase.table("appointments").update({"status": "booked"})
            .eq("id", appointment_id).eq("status", "held").execute().data)
    return rows[0] if rows else {}
