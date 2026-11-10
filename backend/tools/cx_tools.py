"""
cx_tools.py - small database functions ("tools") used by the Customer Experience (CX) Agent
and by the approval code that finishes its proposals.

Each function does ONE job and has no AI in it.
"""

from backend.db import supabase


def get_feedback(feedback_id: str):
    """Return one feedback row (a dict) or None."""
    rows = supabase.table("feedback").select("*").eq("id", feedback_id).execute().data
    return rows[0] if rows else None


def get_appointment_details(appointment_id: str):
    """Return an appointment with its service (name, price) and stylist name, or None."""
    rows = (supabase.table("appointments").select("*, services(name, price), staff(name)")
            .eq("id", appointment_id).execute().data)
    return rows[0] if rows else None


def get_policies() -> list:
    """All salon policy documents (title and body)."""
    return supabase.table("salon_policies").select("title, body").order("title").execute().data


def set_classification(feedback_id: str, category: str, sentiment: str) -> dict:
    """Save the category and sentiment the CX Agent worked out on the feedback row."""
    return (supabase.table("feedback").update({"category": category, "sentiment": sentiment})
            .eq("id", feedback_id).execute().data[0])


def get_ticket_for_feedback(feedback_id: str):
    """Return the ticket already opened for this feedback, or None."""
    rows = supabase.table("tickets").select("*").eq("feedback_id", feedback_id).limit(1).execute().data
    return rows[0] if rows else None


def get_ticket(ticket_id: str):
    rows = supabase.table("tickets").select("*").eq("id", ticket_id).execute().data
    return rows[0] if rows else None


def open_ticket(feedback_id: str, priority: str) -> dict:
    """Open a ticket (status 'open') for a complaint."""
    return supabase.table("tickets").insert({
        "feedback_id": feedback_id, "status": "open", "priority": priority,
    }).execute().data[0]


def resolve_ticket(ticket_id: str, notes: str) -> dict:
    """Mark a ticket 'resolved' and record how. Only called after a manager approves."""
    rows = (supabase.table("tickets").update({"status": "resolved", "resolution_notes": notes})
            .eq("id", ticket_id).execute().data)
    if not rows:
        raise ValueError(f"ticket {ticket_id} not found")
    return rows[0]


def restore_ticket(ticket_id: str, status: str, notes) -> None:
    """Put a ticket back as it was (only used to undo a half-finished approval)."""
    supabase.table("tickets").update({"status": status, "resolution_notes": notes}).eq("id", ticket_id).execute()
