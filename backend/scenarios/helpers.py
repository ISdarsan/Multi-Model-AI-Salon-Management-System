"""
helpers.py - shared by the live tests and the scenario runner.

It creates TEMPORARY test data (events, appointments, feedback, service-log rows) and removes everything
again, except the decision_log rows, which can never be deleted (the log is append-only). An event that has
decision_log rows cannot be deleted either, so those test events stay (their source says what they are).
"""

import time
from datetime import datetime, timedelta, timezone

from backend.db import claim_event, supabase


class TestData:
    """Keeps track of everything a test creates or changes, so cleanup() can undo it."""

    def __init__(self, source: str):
        self.source = source                 # written on the events we create, so they are easy to recognise
        self.event_ids = []                  # events created or claimed by the test
        self.appointment_ids = []            # temporary appointments (deleted at the end)
        self.feedback_ids = []               # temporary feedback rows (deleted at the end)
        self.snapshots = {}                  # appointment id -> how it looked before the test changed it

    # ----- creating things -----

    def new_event(self, type_: str, payload: dict) -> dict:
        """Create an event that is already 'processing', exactly as the worker would hand it to the Orchestrator."""
        event = supabase.table("events").insert({
            "type": type_, "source": self.source, "status": "processing", "payload": payload,
        }).execute().data[0]
        self.event_ids.append(event["id"])
        return event

    def watch_appointment(self, appointment_id: str):
        """Remember an appointment as it is NOW, so cleanup can put it back after the test changes it."""
        if appointment_id not in self.snapshots:
            row = supabase.table("appointments").select("*").eq("id", appointment_id).execute().data[0]
            self.snapshots[appointment_id] = {k: row[k] for k in ("status", "staff_id", "start_time", "end_time")}

    def temp_appointment(self, staff_id: str, service_id: str, start: datetime, minutes: int,
                         customer_id: str = None, status: str = "booked") -> dict:
        """Create a temporary appointment (deleted at the end)."""
        row = supabase.table("appointments").insert({
            "staff_id": staff_id, "service_id": service_id, "customer_id": customer_id, "status": status,
            "start_time": start.isoformat(), "end_time": (start + timedelta(minutes=minutes)).isoformat(),
        }).execute().data[0]
        self.appointment_ids.append(row["id"])
        return row

    # ----- cleaning up -----

    def cleanup(self) -> dict:
        """Undo everything. Returns a short report of what was removed."""
        report = {}

        # 1. Put every appointment the test changed back as it was.
        restored = 0
        for appointment_id, before in self.snapshots.items():
            if appointment_id in self.appointment_ids:
                continue
            now = supabase.table("appointments").select("*").eq("id", appointment_id).execute().data
            if now and any(now[0][k] != before[k] for k in before):
                supabase.table("appointments").update(before).eq("id", appointment_id).execute()
                restored += 1
        report["appointments restored"] = restored

        # 2. Delete the approvals, actions and purchase orders created for the test events (children first).
        actions = []
        if self.event_ids:
            actions = supabase.table("agent_actions").select("id, tool_called, result").in_("event_id", self.event_ids).execute().data
        action_ids = [a["id"] for a in actions]
        po_ids = [a["result"]["purchase_order_id"] for a in actions if a["tool_called"] == "create_draft_po"]
        if action_ids:
            supabase.table("approvals").delete().in_("agent_action_id", action_ids).execute()
            supabase.table("agent_actions").delete().in_("id", action_ids).execute()
        for po_id in po_ids:
            supabase.table("purchase_orders").delete().eq("id", po_id).execute()
        report["actions (with approvals) deleted"] = len(action_ids)
        report["purchase orders deleted"] = len(po_ids)

        # 3. Delete temporary feedback (its tickets go with it) and temporary appointments (their service log goes too).
        for feedback_id in self.feedback_ids:
            supabase.table("feedback").delete().eq("id", feedback_id).execute()
        report["feedback rows deleted"] = len(self.feedback_ids)
        # The appointments' own "booking_created" events (made by a database trigger) are found and registered too.
        if self.appointment_ids:
            for event in supabase.table("events").select("id, payload").eq("type", "booking_created").execute().data:
                if (event["payload"] or {}).get("appointment_id") in self.appointment_ids:
                    self.event_ids.append(event["id"])
            for appointment_id in self.appointment_ids:
                supabase.table("appointments").delete().eq("id", appointment_id).execute()
        report["temporary appointments deleted"] = len(self.appointment_ids)

        # 4. Delete test events that have NO decision_log rows. The others must stay (the log is append-only).
        deleted = kept = 0
        for event_id in set(self.event_ids):
            has_log = supabase.table("decision_log").select("id").eq("event_id", event_id).limit(1).execute().data
            if has_log:
                kept += 1
            else:
                supabase.table("events").delete().eq("id", event_id).execute()
                deleted += 1
        report["events deleted"] = deleted
        report["events kept (they have append-only log rows)"] = kept
        return report


def find_complaint_event(feedback_id: str, wait_seconds: float = 10.0):
    """
    Wait for the complaint_received event that the database trigger creates when a low-rated feedback row is
    inserted, and CLAIM it (new -> processing) so no worker can take it. Returns the event row, or None.
    """
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        rows = (supabase.table("events").select("*").eq("type", "complaint_received")
                .order("created_at", desc=True).limit(20).execute().data)
        for event in rows:
            if (event["payload"] or {}).get("feedback_id") == feedback_id:
                if event["status"] == "new" and claim_event(event["id"]):
                    event["status"] = "processing"
                    return event
                return None   # someone else (a running worker) already took it
        time.sleep(0.3)
    return None


def ids_by_name(table: str, name: str) -> str:
    """The id of the row in `table` (staff or services) with this name."""
    return supabase.table(table).select("id").eq("name", name).execute().data[0]["id"]


def demo_customer_ids() -> list:
    """The ids of the seeded demo customers (profiles with role 'customer')."""
    return [row["id"] for row in supabase.table("profiles").select("id").eq("role", "customer").order("id").execute().data]


def day_at(days_from_today: int, hour: int, minute: int = 0) -> datetime:
    """A UTC datetime at hour:minute, `days_from_today` days from today (negative = in the past)."""
    today = datetime.now(timezone.utc).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return today + timedelta(days=days_from_today)
