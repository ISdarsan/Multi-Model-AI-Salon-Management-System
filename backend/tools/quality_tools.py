"""
quality_tools.py - small database functions ("tools") used by the Quality Agent.
"""

from backend.db import supabase


def get_service_log(appointment_id: str) -> list:
    """The service_log rows (readiness and quality notes) written for one appointment, oldest first."""
    return (supabase.table("service_log").select("*")
            .eq("appointment_id", appointment_id).order("created_at").execute().data)
