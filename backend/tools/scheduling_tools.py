"""
scheduling_tools.py - small database functions ("tools") used by the Scheduling Agent.

Each function does ONE job and has no AI in it. The agent decides when to call them.
"""

from backend.db import supabase

# ----------------------------------------------------------------------------
# Which skill does each service need?
# ----------------------------------------------------------------------------
# The database does not say which staff skill a service needs, so we keep that
# knowledge here, keyed by the service NAME. Each skill must match a value in
# staff.skills (for example {colour,cut}). If you add a new service, add it here.
SERVICE_SKILLS = {
    "Women's Cut & Style": "cut",
    "Men's Cut": "cut",
    "Full Colour": "colour",
    "Root Touch-up": "colour",
    "Highlights": "colour",
    "Blow Dry & Style": "styling",
    "Deep Conditioning Treatment": "styling",
    "Facial & Skincare": "skincare",
}

# Appointments in these states take up a stylist's time, so they block a slot.
BLOCKING_STATUSES = ["booked", "held", "in_progress"]
# Appointments in these states can still be moved to another stylist.
MOVABLE_STATUSES = ["booked", "held"]


def required_skill(service_name: str):
    """The skill a service needs, or None if we do not know this service."""
    return SERVICE_SKILLS.get(service_name)


def get_staff(staff_id: str = None):
    """Return one stylist (as a dict, or None) if staff_id is given, otherwise ALL stylists."""
    query = supabase.table("staff").select("*")
    if staff_id:
        rows = query.eq("id", staff_id).execute().data
        return rows[0] if rows else None
    return query.order("name").execute().data


def get_appointments(staff_id: str, from_iso: str, to_iso: str) -> list:
    """
    This stylist's appointments that start between from_iso and to_iso (both included),
    that can still be moved (booked or held). Includes the service name and duration.
    """
    return (
        supabase.table("appointments")
        .select("*, services(name, duration_minutes)")
        .eq("staff_id", staff_id)
        .in_("status", MOVABLE_STATUSES)
        .gte("start_time", from_iso)
        .lte("start_time", to_iso)
        .order("start_time")
        .execute()
        .data
    )


def get_day_appointments(day_start_iso: str, day_end_iso: str) -> list:
    """
    Everybody's appointments that start in one day (all stylists, anything not cancelled).
    The agent uses this to check for overlaps and to count how busy each stylist is.
    """
    return (
        supabase.table("appointments")
        .select("id, staff_id, start_time, end_time, status")
        .neq("status", "cancelled")
        .gte("start_time", day_start_iso)
        .lt("start_time", day_end_iso)
        .execute()
        .data
    )


def get_appointment(appointment_id: str):
    """Return one appointment (as a dict) or None."""
    rows = supabase.table("appointments").select("*").eq("id", appointment_id).execute().data
    return rows[0] if rows else None


def reassign_staff(appointment_id: str, new_staff_id: str) -> dict:
    """
    Move an appointment to another stylist. This is the ONLY place an appointment is changed,
    and it is only called after a manager approves. If the new stylist is already busy at
    that time, the database refuses (double-booking protection) and we raise a clear error.
    """
    try:
        rows = (
            supabase.table("appointments")
            .update({"staff_id": new_staff_id})
            .eq("id", appointment_id)
            .execute()
            .data
        )
    except Exception as error:
        raise ValueError(f"could not reassign appointment {appointment_id}: {error}")
    if not rows:
        raise ValueError(f"appointment {appointment_id} not found")
    return rows[0]


def get_next_booked_appointments(from_iso: str, limit: int) -> list:
    """
    The next `limit` 'booked' appointments that start at or after from_iso (all stylists), earliest first.
    Each comes with its service name and duration.
    """
    return (
        supabase.table("appointments")
        .select("*, services(name, duration_minutes)")
        .eq("status", "booked")
        .gte("start_time", from_iso)
        .order("start_time")
        .order("id")          # same start time (different stylists): keep a fixed order
        .limit(limit)
        .execute()
        .data
    )


def shift_appointment(appointment_id: str, new_start_iso: str, new_end_iso: str) -> dict:
    """
    Move an appointment to a new time (same stylist). The ONLY place a shift is applied, and only after a
    manager approves. If the stylist is already busy then, the database refuses (double-booking protection)
    and we raise a clear error.
    """
    try:
        rows = (
            supabase.table("appointments")
            .update({"start_time": new_start_iso, "end_time": new_end_iso})
            .eq("id", appointment_id)
            .execute()
            .data
        )
    except Exception as error:
        raise ValueError(f"could not shift appointment {appointment_id}: {error}")
    if not rows:
        raise ValueError(f"appointment {appointment_id} not found")
    return rows[0]
