"""
cx_agent.py - the Customer Experience (CX) Agent.

It also drafts notices for customers (see handle_notices at the bottom) when their appointment is proposed
to move, because a stylist is absent (`staff_absent`) or the waiting area is overloaded (`queue_overload`).

For a complaint (a `complaint_received` event) it:
  1. classifies the complaint with keyword rules (category + sentiment) and saves that on the feedback row,
  2. opens a ticket (priority from the rating),
  3. finds the best matching salon policies with simple keyword matching,
  4. drafts a reply to the customer (MEDIUM risk, tool 'draft_reply'),
  5. if the complaint looks justified, suggests a refund or compensation within the policy limit
     (HIGH risk, tool 'suggest_refund'),
  6. writes ONE decision_log row for the whole complaint.

All decisions are plain Python rules. The AI (Ollama, through backend/ai.py) ONLY writes the wording of
the draft reply; if it fails, a template sentence is used.

IMPORTANT: nothing is ever sent to a customer. A draft is just text stored in the proposal. Nothing
becomes final until a manager approves it (backend/approvals/approval_actions.py).
"""

import re

from backend.agents.scheduling_agent import parse_dt
from backend.ai import short_explanation
from backend.db import add_decision_log
from backend.proposals import pending_input_values, propose, record_executed
from backend.tools.cx_tools import (
    get_appointment_details,
    get_feedback,
    get_policies,
    get_ticket_for_feedback,
    open_ticket,
    set_classification,
)

AGENT_NAME = "cx_agent"

# The most a staff suggestion may be, as a percentage of the service price. This mirrors the salon's
# "Refund Policy" ("Refunds up to 50% of the service price may be suggested by staff") and the
# "Delay Compensation" policy (10% off for a wait over 30 minutes).
REFUND_LIMIT_PERCENT = 50
DELAY_COMPENSATION_PERCENT = 10

# The one decision_log outcome used for a handled complaint. (The dashboard uses it to show the reasoning.)
COMPLAINT_OUTCOME = "complaint handled, waiting for approval"
# The decision_log outcome for each drafted customer notice.
NOTICE_OUTCOME = "customer notice drafted, waiting for approval"

# ----------------------------------------------------------------------------
# 1. The rules (plain Python, no AI, no database)
# ----------------------------------------------------------------------------

# Words that point to each category. The category with the most matching words wins;
# on a tie the one listed FIRST wins. No match at all gives 'other'.
CATEGORY_KEYWORDS = {
    "wait time": ["wait", "waited", "waiting", "late", "delay", "queue", "hour"],
    "price": ["price", "pricey", "expensive", "cost", "charge", "fee", "discount"],
    "product": ["product", "shampoo", "conditioner", "serum", "allerg", "reaction", "smell", "scalp", "itch"],
    "staff behaviour": ["rude", "unfriendly", "ignored", "attitude", "apolog", "unprofessional", "drink",
                        "cancelled", "no notice"],
    "service quality": ["cut", "colour", "color", "patchy", "orange", "uneven", "fringe", "haircut", "highlights",
                        "result", "trim", "style", "fade", "shade", "refund", "rushed", "clean", "dirty", "wet",
                        "clippings"],
}

# Which policy title (a word in it) fits each category best.
CATEGORY_POLICY_HINT = {
    "wait time": "delay", "price": "refund", "product": "allergy", "staff behaviour": "escalation",
    "service quality": "refund", "other": "escalation",
}

STOP_WORDS = {"that", "this", "with", "from", "have", "were", "very", "they", "them", "when", "what", "your",
              "would", "could", "about", "which", "there", "their", "been", "also", "into", "than", "then"}


def classify_complaint(text: str) -> str:
    """Pick the category whose keywords appear most often in the complaint text."""
    lowered = text.lower()
    best, best_hits = "other", 0
    for category, words in CATEGORY_KEYWORDS.items():
        hits = sum(1 for word in words if word in lowered)
        if hits > best_hits:      # strictly more: on a tie the earlier category stays
            best, best_hits = category, hits
    return best


def sentiment_from_rating(rating: int) -> str:
    """1-2 stars is negative, 3 is neutral, 4-5 is positive."""
    return "negative" if rating <= 2 else ("neutral" if rating == 3 else "positive")


def priority_from_rating(rating: int) -> str:
    """The lower the rating, the more urgent the ticket: 1 -> high, 2 -> medium, otherwise low."""
    return "high" if rating <= 1 else ("medium" if rating == 2 else "low")


def match_policies(text: str, category: str, policies: list, top: int = 2) -> list:
    """
    Simple keyword matching: score every policy by how many words of the complaint it contains,
    plus a bonus when its title fits the category. Returns the best `top` policies (score above 0).
    """
    words = {w for w in re.findall(r"[a-z]{4,}", text.lower())} - STOP_WORDS
    hint = CATEGORY_POLICY_HINT.get(category, "escalation")
    scored = []
    for policy in policies:
        policy_words = set(re.findall(r"[a-z]{4,}", (policy["title"] + " " + policy["body"]).lower()))
        score = len(words & policy_words) + (5 if hint in policy["title"].lower() else 0)
        if score > 0:
            scored.append((-score, policy["title"], policy))
    scored.sort(key=lambda item: (item[0], item[1]))   # best score first, then alphabetical
    return [item[2] for item in scored[:top]]


def decide_refund(rating: int, quality_flagged: bool, category: str, service_price):
    """
    Should the agent suggest a refund or compensation? Only if the complaint looks JUSTIFIED:
    the Quality Agent flagged a problem, or the rating is 1. The amount never goes above the policy limit.

      wait time complaint -> compensation of DELAY_COMPENSATION_PERCENT (10%) of the service price
      anything else       -> refund of REFUND_LIMIT_PERCENT (50%) of the service price

    Returns None, or {"percent", "amount", "basis", "reasons"}.
    """
    reasons = []
    if quality_flagged:
        reasons.append("the Quality Agent found a problem in the service log")
    if rating == 1:
        reasons.append("the rating is 1")
    if not reasons:
        return None
    if service_price is None:
        return None   # we do not know what the customer paid, so no amount can be suggested

    if category == "wait time":
        percent, basis = DELAY_COMPENSATION_PERCENT, "Delay Compensation"
    else:
        percent, basis = REFUND_LIMIT_PERCENT, "Refund Policy"
    return {"percent": percent, "amount": round(float(service_price) * percent / 100, 2),
            "basis": basis, "reasons": reasons}


# ----------------------------------------------------------------------------
# 2. The small AI part: the wording of the draft reply (with a safe fallback)
# ----------------------------------------------------------------------------

def draft_reply_text(category: str, service: str, complaint: str) -> str:
    """A short, polite reply for the manager to review. Never promises money. Falls back to a template."""
    about = f"your {service}" if service else "your visit"
    template = (f"Thank you for telling us about {about}. We are very sorry it did not meet your expectations. "
                "A manager is looking into this personally and will contact you soon.")
    prompt = (
        "Write a short, polite reply (at most three sentences) from a salon to a customer who complained. "
        "Do NOT promise a refund, discount or compensation. Do NOT invent facts. Say that a manager will look "
        "into it and be in touch. Reply with ONLY the message, no introduction.\n"
        f"Complaint category: {category}\n"
        f"Service: {service or 'unknown'}\n"
        f"The customer wrote: {complaint}"
    )
    return short_explanation(prompt, template)


# ----------------------------------------------------------------------------
# 3. Handling a complaint
# ----------------------------------------------------------------------------

def handle_complaint(event: dict, context: dict) -> dict:
    payload = event["payload"] or {}
    if not payload.get("feedback_id"):
        raise ValueError("complaint_received payload needs a 'feedback_id'")
    feedback = get_feedback(payload["feedback_id"])
    if not feedback:
        raise ValueError(f"feedback not found: {payload['feedback_id']}")

    # Step 1: classify. This is harmless, so it is saved straight away (low risk) and logged.
    category = classify_complaint(feedback["text"])
    sentiment = sentiment_from_rating(feedback["rating"])
    set_classification(feedback["id"], category, sentiment)
    record_executed(event["id"], AGENT_NAME, "classify_feedback", {"feedback_id": feedback["id"], "rating": feedback["rating"]},
                    {"category": category, "sentiment": sentiment})

    # Step 2: open a ticket (low risk, internal). If one exists already, reuse it instead of opening a second.
    ticket = get_ticket_for_feedback(feedback["id"])
    if ticket:
        ticket_note = f"an existing {ticket['priority']}-priority ticket was reused"
    else:
        priority = priority_from_rating(feedback["rating"])
        ticket = open_ticket(feedback["id"], priority)
        record_executed(event["id"], AGENT_NAME, "open_ticket", {"feedback_id": feedback["id"], "priority": priority},
                        {"ticket_id": ticket["id"], "status": ticket["status"]})
        ticket_note = f"a {priority}-priority ticket was opened"

    # Step 3: which policies apply? (A read only, so nothing to log as an action.)
    policies = match_policies(feedback["text"], category, get_policies())
    policy_titles = [p["title"] for p in policies]

    # Step 4: what happened at the salon? Load the appointment (service, price, stylist).
    appointment = get_appointment_details(feedback["appointment_id"]) if feedback["appointment_id"] else None
    service = appointment["services"]["name"] if appointment else None
    price = appointment["services"]["price"] if appointment else None
    stylist = appointment["staff"]["name"] if appointment else None
    quality = context.get("quality") or {"flagged": False, "summary": "the Quality Agent did not check a service log"}

    # Step 5: the draft reply (medium risk). The AI only writes the words.
    reply = draft_reply_text(category, service, feedback["text"])
    reply_action, reply_approval = propose(
        event["id"], AGENT_NAME, "draft_reply", "medium",
        {"ticket_id": ticket["id"], "feedback_id": feedback["id"], "category": category, "rating": feedback["rating"],
         "service": service, "matched_policies": policy_titles},
        {"draft_text": reply, "sent": False},
    )
    results = [
        {"label": "classify", "outcome": f"classified as {category}, {sentiment}", "detail": f"rating {feedback['rating']}"},
        {"label": "ticket", "outcome": ticket_note, "detail": f"ticket {ticket['id']}"},
        {"label": "draft reply", "outcome": "reply drafted, waiting for approval", "detail": reply,
         "agent_action_id": reply_action["id"], "approval_id": reply_approval["id"]},
    ]

    # Step 6: a refund or compensation, only if the complaint looks justified (HIGH risk).
    refund = decide_refund(feedback["rating"], quality.get("flagged", False), category, price)
    if refund:
        refund_action, refund_approval = propose(
            event["id"], AGENT_NAME, "suggest_refund", "high",
            {"ticket_id": ticket["id"], "feedback_id": feedback["id"], "appointment_id": feedback["appointment_id"],
             "service": service, "service_price": float(price), "percent": refund["percent"],
             "basis": refund["basis"], "reasons": refund["reasons"]},
            {"suggested_amount": refund["amount"], "applied": False},
        )
        refund_text = (f"A {refund['percent']}% {'compensation' if category == 'wait time' else 'refund'} of "
                       f"{refund['amount']:.2f} (the {service} costs {float(price):.2f}) is suggested under the "
                       f"{refund['basis']}, because {' and '.join(refund['reasons'])}.")
        results.append({"label": "refund", "outcome": "refund suggested, waiting for approval", "detail": refund_text,
                        "agent_action_id": refund_action["id"], "approval_id": refund_approval["id"]})
    elif price is None:
        refund_text = "No refund was suggested because the complaint is not linked to an appointment, so the price is unknown."
    else:
        refund_text = "No refund was suggested because the complaint does not look justified (no problem in the service log and the rating is not 1)."

    # Step 7: ONE decision_log row for the whole complaint, with the real details.
    where = f"the {service} with {stylist}" if appointment else "an unknown appointment"
    reasoning = (
        f"Complaint with rating {feedback['rating']} about {where}. Classified as '{category}' ({sentiment}); "
        f"{ticket_note}. Service log: {quality['summary']}. "
        f"Matching policies: {', '.join(policy_titles) or 'none found'}. {refund_text} "
        "A reply was drafted for the manager to review. Nothing was sent to the customer."
    )
    add_decision_log(event["id"], ["cx_agent", "quality_agent"], reasoning, COMPLAINT_OUTCOME)

    return {"status": "handled", "results": results}


# ----------------------------------------------------------------------------
# 4. Customer notices (scenarios 2 and 5)
# ----------------------------------------------------------------------------

def when_text(iso: str) -> str:
    """'2026-11-12T09:30:00+00:00' -> '09:30 UTC on 12 Nov'."""
    moment = parse_dt(iso)
    return f"{moment:%H:%M} UTC on {moment:%d %b}"


def notice_text(kind: str, item: dict) -> str:
    """
    A short, polite notice for ONE customer. The AI only writes the wording; a template is used if it fails.
    kind 'delay'        -> the appointment was moved later because the salon is busy
    kind 'reassignment' -> the appointment is now with another stylist because the usual one is unavailable
    """
    if kind == "delay":
        template = (f"We are sorry: because the salon is very busy, your {item['service']} has been moved from "
                    f"{when_text(item['old_start'])} to {when_text(item['new_start'])}. Thank you for your patience.")
        facts = (f"Service: {item['service']}\nOriginal time: {when_text(item['old_start'])}\n"
                 f"New time ({item['minutes']} minutes later): {when_text(item['new_start'])}\nReason: the salon is very busy")
    else:
        template = (f"Your {item['service']} on {when_text(item['start_time'])} will now be with {item['new_staff']} "
                    f"because {item['old_staff']} is not available. The time of your appointment stays the same.")
        facts = (f"Service: {item['service']}\nTime (unchanged): {when_text(item['start_time'])}\n"
                 f"New stylist: {item['new_staff']}\nReason: the usual stylist {item['old_staff']} is not available")
    prompt = ("Write a short, polite notice (at most three sentences) from a salon to a customer about a change to "
              "their appointment. Do NOT invent facts and do NOT offer compensation. Reply with ONLY the message, "
              "no introduction.\n" + facts)
    return short_explanation(prompt, template)


def handle_notices(event: dict, context: dict) -> dict:
    """
    Draft a notice (MEDIUM risk, 'draft_customer_notice') for each customer whose appointment another agent
    proposed to change. The proposals come in `context` (the Scheduling Agent put them there).
    Walk-ins have no customer on file, so they get no notice. NOTHING is sent to anyone.
    """
    if event["type"] == "queue_overload":
        kind, items = "delay", context.get("shifts", [])
    else:
        kind, items = "reassignment", context.get("reassignments", [])

    pending = pending_input_values("draft_customer_notice", "appointment_id")   # notices already waiting
    results, walk_ins = [], 0
    for item in items:
        service = item["service"]
        if not item.get("customer_id"):
            walk_ins += 1
            continue
        if item["appointment_id"] in pending:
            results.append({"label": f"notice {service}", "outcome": "notice already proposed", "detail": "a notice for this appointment is already waiting"})
            continue

        text = notice_text(kind, item)
        if kind == "delay":
            about = (f"Delay notice for the customer of the {service} (proposed to move {item['minutes']} minutes later, "
                     f"{when_text(item['old_start'])} to {when_text(item['new_start'])}) because the waiting area is overloaded.")
        else:
            about = (f"Notice for the customer of the {service} on {when_text(item['start_time'])}: the appointment is proposed to "
                     f"move from {item['old_staff']} to {item['new_staff']} because {item['old_staff']} is absent.")
        reasoning = about + " It only applies if the manager approves the change. Nothing was sent to the customer."
        action, approval = propose(
            event["id"], AGENT_NAME, "draft_customer_notice", "medium",
            {"appointment_id": item["appointment_id"], "customer_id": item["customer_id"], "kind": kind, "service": service,
             "old_start": item.get("old_start") or item.get("start_time"), "new_start": item.get("new_start"),
             "old_staff": item.get("old_staff"), "new_staff": item.get("new_staff")},
            {"draft_text": text, "sent": False},
            reasoning, NOTICE_OUTCOME, ["cx_agent", "scheduling_agent"],
        )
        results.append({"label": f"notice {service}", "outcome": NOTICE_OUTCOME, "detail": text,
                        "appointment_id": item["appointment_id"], "agent_action_id": action["id"],
                        "approval_id": approval["id"]})

    if walk_ins:
        why = f"{walk_ins} affected appointment(s) belong to walk-ins with no customer on file, so no notice was drafted for them."
        add_decision_log(event["id"], ["cx_agent", "scheduling_agent"], why, "no notice needed for walk-ins")
        results.append({"label": "walk-ins", "outcome": "no notice needed for walk-ins", "detail": why})
    return {"status": "handled", "results": results}


# ----------------------------------------------------------------------------
# 5. The entry point the Orchestrator calls
# ----------------------------------------------------------------------------

def handle_event(event: dict, context: dict = None) -> dict:
    """
    Handle one event that the Orchestrator has routed to the CX Agent.
    Does NOT change the event's status (the Orchestrator does that).
    `context` is shared with the other agents that run for the same event (for example the Quality Agent's finding).
    """
    if context is None:
        context = {}
    if event["type"] == "complaint_received":
        return handle_complaint(event, context)
    if event["type"] in ("queue_overload", "staff_absent"):
        return handle_notices(event, context)
    raise ValueError(f"the CX Agent does not handle events of type '{event['type']}'")
