"""
main.py - a small FastAPI server for SalonOS.

It has 3 endpoints:
  GET  /health         -> says the server is running (no key needed)
  POST /events         -> saves a new event in the Supabase `events` table (needs X-API-Key)
  GET  /events/recent  -> returns the 20 newest events (needs X-API-Key)

Start it from the project root:   uvicorn backend.api.main:app --reload
Then open http://127.0.0.1:8000/docs to try it in your browser.
"""

import os
import secrets
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from supabase import create_client

# ----------------------------------------------------------------------------
# 1. Settings and the Supabase connection (same idea as hello_agent.py)
# ----------------------------------------------------------------------------

# The .env file lives in the project root, two folders above this file.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
API_KEY = os.getenv("API_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise SystemExit("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY. Check your .env file.")
if not API_KEY:
    # Without this check, an empty key would let everybody in. So we refuse to start.
    raise SystemExit("Missing API_KEY in .env. Add a line like:  API_KEY=some-long-random-text")

# The service role key lets this backend read and write everything in Supabase.
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(title="SalonOS API")


# ----------------------------------------------------------------------------
# 2. The shapes of the data (FastAPI checks incoming JSON against these)
# ----------------------------------------------------------------------------

class EventType(str, Enum):
    """The only event types allowed (they match the event_type list in the database)."""
    shelf_observation = "shelf_observation"
    stock_low = "stock_low"
    booking_created = "booking_created"
    staff_absent = "staff_absent"
    complaint_received = "complaint_received"
    queue_overload = "queue_overload"


class EventIn(BaseModel):
    """What the caller must send to POST /events."""
    type: EventType                      # a wrong value gives an error listing the allowed ones
    source: str = Field(min_length=1)    # who sent it, e.g. "cv_pipeline"
    payload: dict = Field(default_factory=dict)  # any JSON object (see docs/SCHEMA.md)


# ----------------------------------------------------------------------------
# 3. The API key check (used by the two /events endpoints)
# ----------------------------------------------------------------------------

def require_api_key(x_api_key: str | None = Header(default=None)):
    """Reject the request with 401 unless the X-API-Key header matches API_KEY in .env."""
    # compare_digest compares safely, so timing cannot be used to guess the key.
    if x_api_key is None or not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Missing or wrong X-API-Key header")


# ----------------------------------------------------------------------------
# 4. The endpoints
# ----------------------------------------------------------------------------

@app.get("/health")
def health():
    """Used to check the server is running. No key needed."""
    return {"status": "ok"}


@app.post("/events", status_code=201, dependencies=[Depends(require_api_key)])
def create_event(event: EventIn):
    """Save a new event with status 'new' and return the saved row."""
    try:
        response = supabase.table("events").insert({
            "type": event.type.value,
            "source": event.source,
            "payload": event.payload,
            "status": "new",
        }).execute()
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"Could not save the event: {error}")
    return response.data[0]


@app.get("/events/recent", dependencies=[Depends(require_api_key)])
def recent_events():
    """Return the 20 newest events, newest first."""
    try:
        response = (
            supabase.table("events")
            .select("*")
            .order("created_at", desc=True)
            .limit(20)
            .execute()
        )
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"Could not read events: {error}")
    return response.data
