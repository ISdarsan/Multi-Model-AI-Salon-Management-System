"""
db.py - the shared Supabase connection and one shared helper.

Everything that talks to the database (tools, agents, approvals) imports from here,
so the connection is set up in ONE place.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

# The .env file lives in the project root, one folder above this file.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise SystemExit("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY. Check your .env file.")

# The service role key lets this backend read and write everything.
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def now_iso() -> str:
    """The current time as text that Supabase understands (UTC)."""
    return datetime.now(timezone.utc).isoformat()


def claim_event(event_id: str) -> bool:
    """
    Switch an event from 'new' to 'processing', but ONLY if it is still 'new'.
    Returns True if we got it, False if someone else already took it. This is what
    makes sure an event can never be handled twice.
    """
    claimed = (
        supabase.table("events")
        .update({"status": "processing"})
        .eq("id", event_id)
        .eq("status", "new")
        .execute()
        .data
    )
    return bool(claimed)


def add_decision_log(event_id: str, agents_involved: list, reasoning: str, outcome: str) -> dict:
    """
    Add ONE row to decision_log (the permanent record of what happened and why).

    This table is append-only: the database refuses to edit or delete its rows,
    so the only thing we ever do is add new rows.
    """
    row = supabase.table("decision_log").insert({
        "event_id": event_id,
        "agents_involved": agents_involved,
        "reasoning": reasoning,
        "outcome": outcome,
    }).execute().data[0]
    return row
