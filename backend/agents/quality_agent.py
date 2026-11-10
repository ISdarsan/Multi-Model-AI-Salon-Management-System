"""
quality_agent.py - the Quality Agent.

Its job (for a complaint): look at the service log of the appointment the complaint is about and
decide, with plain keyword rules, whether the notes mention a problem. If they do, it records a
quality flag.

All decisions are plain Python. There is no AI in this agent.

The Quality Agent does not make proposals and does not write the decision_log row: it hands its
finding to the CX Agent through `context` (a small dictionary the Orchestrator passes along), and
the CX Agent writes ONE log row for the whole complaint.
"""

import re

from backend.proposals import record_executed
from backend.tools.quality_tools import get_service_log

AGENT_NAME = "quality_agent"

# Words in a service-log note that suggest something went wrong. Each is a small pattern:
# "\\w*" means "and anything that continues the word" (so "uneven" also finds "unevenness").
PROBLEM_PATTERNS = [
    r"\bover target\b", r"\buneven\w*", r"\bdelay\w*", r"\blate\b", r"\bmistake\w*", r"\bredo\b",
    r"\bre-do\b", r"\bdirty\b", r"\bnot sterili[sz]ed\b", r"\bdamag\w*", r"\bburn\w*", r"\bpatchy\b",
    r"\bwrong\b", r"\bbroken\b", r"\bcomplain\w*", r"\bcorrected\b",
]


def find_problems(notes: list) -> list:
    """
    Look through the notes (a list of text) and return the ones that mention a problem.
    Each result is {"note": the text, "matches": [the words that matched]}.
    """
    problems = []
    for note in notes:
        if not note:
            continue
        matches = []
        for pattern in PROBLEM_PATTERNS:
            matches += [m.group(0) for m in re.finditer(pattern, note.lower())]
        if matches:
            problems.append({"note": note, "matches": sorted(set(matches))})
    return problems


def handle_event(event: dict, context: dict = None) -> dict:
    """
    Check the service log for a complaint_received event. Does NOT change the event's status
    (the Orchestrator does that). Puts its finding in context["quality"] for the CX Agent.
    """
    if context is None:
        context = {}
    payload = event["payload"] or {}
    appointment_id = payload.get("appointment_id")

    # No appointment linked to the complaint: nothing to check.
    if not appointment_id:
        context["quality"] = {"checked": False, "flagged": False, "problems": [], "rows": 0,
                              "summary": "the complaint is not linked to an appointment, so no service log could be checked"}
        return {"status": "handled", "results": [
            {"label": "service log", "outcome": "no appointment linked", "detail": context["quality"]["summary"]}]}

    rows = get_service_log(appointment_id)
    notes = [text for row in rows for text in (row.get("readiness_notes"), row.get("quality_notes"))]
    problems = find_problems(notes)

    if not rows:
        summary = "no service log was written for this appointment"
    elif problems:
        summary = "the service log mentions a problem: " + "; ".join(f"\"{p['note']}\"" for p in problems)
    else:
        summary = f"the service log ({len(rows)} entry) shows no problem" if len(rows) == 1 else \
                  f"the service log ({len(rows)} entries) shows no problem"
    context["quality"] = {"checked": bool(rows), "flagged": bool(problems), "problems": problems,
                          "rows": len(rows), "summary": summary}

    # A flagged problem is recorded as a (low-risk, internal) quality flag.
    if problems:
        record_executed(
            event["id"], AGENT_NAME, "flag_quality_issue",
            {"appointment_id": appointment_id, "service_log_rows": len(rows)},
            {"flagged": True, "problems": problems},
        )
    outcome = "quality problem flagged" if problems else ("no quality problem found" if rows else "no service log found")
    return {"status": "handled", "results": [{"label": "service log", "outcome": outcome, "detail": summary}]}
