"""
main.py - a small FastAPI server for SalonOS.

It has these endpoints:
  GET  /health                    -> says the server is running (no key needed)
  POST /events                    -> saves a new event in the Supabase `events` table (needs X-API-Key)
  GET  /events/recent             -> returns the 20 newest events (needs X-API-Key)
  POST /approvals/{id}/approve    -> a MANAGER approves a proposal (needs "Authorization: Bearer <token>")
  POST /approvals/{id}/reject     -> a MANAGER rejects a proposal, with a reason (needs the same token)

Start it from the project root:   uvicorn backend.api.main:app --reload
Then open http://127.0.0.1:8000/docs to try it in your browser.
"""

import os
import secrets
from enum import Enum
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from supabase import create_client

# The SAME approve() and reject() functions the agents' tests use. The API only checks WHO is asking.
from backend.approvals.approval_actions import approve, reject

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

# CORS: lets the dashboard (running at localhost:5173) call this server from the browser.
# Without this, the browser would block the dashboard's requests.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)


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


# ----------------------------------------------------------------------------
# 5. Manager approvals (used by the dashboard)
# ----------------------------------------------------------------------------

def require_manager(authorization: str | None = Header(default=None)) -> str:
    """
    Check the caller is a signed-in MANAGER and return their user id.

    The dashboard sends:  Authorization: Bearer <the Supabase login token>
      - no token or a bad/expired token -> 401
      - a valid token but the role is not 'manager' -> 403
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Please sign in (missing Authorization: Bearer token)")
    token = authorization[7:].strip()

    # Ask Supabase whether this token is real and who it belongs to.
    try:
        user = supabase.auth.get_user(token).user
    except Exception:
        user = None
    if not user:
        raise HTTPException(status_code=401, detail="Your sign-in is not valid or has expired. Please sign in again.")

    # The role lives in the profiles table. Only managers may approve or reject.
    profile = supabase.table("profiles").select("role").eq("id", user.id).execute().data
    if not profile or profile[0]["role"] != "manager":
        raise HTTPException(status_code=403, detail="Only managers can approve or reject.")
    return user.id


class RejectIn(BaseModel):
    """What the caller must send to reject: a reason (required)."""
    reason: str = Field(min_length=1)


def decide(function, *args) -> dict:
    """Run approve() or reject() and turn its errors into clear HTTP errors."""
    try:
        changes = function(*args)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error))
    except ValueError as error:
        message = str(error)
        # "not found" -> 404. Anything else (already decided, appointment changed...) -> 409 conflict.
        raise HTTPException(status_code=404 if "not found" in message else 409, detail=message)
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not complete this: {error}")
    return {"ok": True, "approval": changes["approval"], "outcome": changes["decision_log"]["outcome"]}


@app.post("/approvals/{approval_id}/approve")
def approve_approval(approval_id: UUID, manager_id: str = Depends(require_manager)):
    """A manager approves a pending proposal. The action is carried out and logged."""
    return decide(approve, str(approval_id), manager_id)


@app.post("/approvals/{approval_id}/reject")
def reject_approval(approval_id: UUID, body: RejectIn, manager_id: str = Depends(require_manager)):
    """A manager rejects a pending proposal. Nothing is carried out, and the reason is logged."""
    if not body.reason.strip():
        raise HTTPException(status_code=422, detail="A reason is required when rejecting.")
    return decide(reject, str(approval_id), manager_id, body.reason.strip())
