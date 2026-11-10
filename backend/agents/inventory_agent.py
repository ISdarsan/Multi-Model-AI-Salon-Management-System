"""
inventory_agent.py - the Inventory Agent.

Its job: watch for events that say "stock may be low" and, when a product really
needs reordering, create a DRAFT purchase order that a manager must approve.

All decisions are made by plain Python rules (the maths in decide_reorder below).
The AI (Ollama) is used ONLY to write a short plain-English explanation. If the AI
fails for any reason, a template sentence is used instead, so the agent never fails
because of the AI.

Run it from the project root:   python -m backend.agents.inventory_agent
"""

import math
import os

from backend.db import add_decision_log, supabase
from backend.tools.inventory_tools import (
    create_draft_po,
    get_open_purchase_order,
    get_sales_velocity,
    get_stock,
)

AGENT_NAME = "inventory_agent"
EVENT_TYPES = ["stock_low", "shelf_observation"]  # the events this agent cares about

SALES_WINDOW_DAYS = 7      # look at the last 7 days of sales
SAFETY_DAYS = 2            # reorder if stock will run out within lead time + 2 days
COVER_DAYS = 14            # a new order should cover lead time + 14 days of sales


# ----------------------------------------------------------------------------
# 1. The rules (plain Python, no AI, no database)
# ----------------------------------------------------------------------------

def decide_reorder(current_qty: int, units_sold: int, days: int, lead_time_days: int,
                   reorder_threshold: int) -> dict:
    """
    Decide whether to reorder, using only numbers. There are two situations.

    A) The product HAS sold recently ("normal"):
         average daily sales = units sold / days
         days left           = current stock / average daily sales
         reorder needed      = days left < lead time + 2
         reorder quantity    = (lead time + 14) days of sales - current stock, rounded UP, at least 1

    B) The product had NO sales in the window:
         we cannot work out days left, so we look at the stock level instead.
         reorder needed      = current stock < reorder threshold
         reorder quantity    = enough to reach 2 x the reorder threshold, at least 1
    """
    if units_sold <= 0:
        if current_qty < reorder_threshold:
            qty = max(1, 2 * reorder_threshold - current_qty)
            return {"needs_reorder": True, "mode": "low_stock_no_sales", "avg_daily_sales": 0.0,
                    "days_left": None, "qty": qty, "note": ""}
        return {"needs_reorder": False, "mode": "no_sales", "avg_daily_sales": 0.0, "days_left": None, "qty": 0,
                "note": (f"no sales in the last {days} days, and stock ({current_qty}) is not below "
                         f"the reorder level ({reorder_threshold})")}

    avg_daily_sales = units_sold / days
    days_left = current_qty / avg_daily_sales

    # Whole-number maths for the yes/no test, so rounding can never flip the answer:
    #   days_left < lead + 2   <=>   current_qty * days < (lead + 2) * units_sold
    needs_reorder = current_qty * days < (lead_time_days + SAFETY_DAYS) * units_sold

    qty = 0
    if needs_reorder:
        wanted = -(-units_sold * (lead_time_days + COVER_DAYS) // days)  # ceiling division
        qty = max(1, wanted - current_qty)

    return {"needs_reorder": needs_reorder, "mode": "normal", "avg_daily_sales": avg_daily_sales,
            "days_left": days_left, "qty": qty, "note": ""}


# ----------------------------------------------------------------------------
# 2. The small AI part: write a 2-sentence reason (with a safe fallback)
# ----------------------------------------------------------------------------

def write_reason(stock: dict, decision: dict) -> str:
    """Return a short plain-English reason. Never raises: falls back to a template."""
    if decision["mode"] == "low_stock_no_sales":
        # No sales rate to talk about: the reason is simply "stock is below the reorder level".
        template = (
            f"{stock['sku']} has only {stock['current_qty']} units, below its reorder level of "
            f"{stock['reorder_threshold']}, and nothing was sold in the last {SALES_WINDOW_DAYS} days. "
            f"A draft order for {decision['qty']} units brings stock up to twice the reorder level."
        )
        facts = (
            f"Product: {stock['sku']} ({stock['name']})\n"
            f"Units in stock: {stock['current_qty']}\n"
            f"Reorder level: {stock['reorder_threshold']}\n"
            f"Sales in the last {SALES_WINDOW_DAYS} days: none\n"
            f"Quantity in the draft order (brings stock up to twice the reorder level): {decision['qty']}"
        )
    else:
        template = (
            f"{stock['sku']} has {decision['days_left']:.1f} days of stock left "
            f"({stock['current_qty']} units, selling about {decision['avg_daily_sales']:.1f} per day), "
            f"but delivery takes {stock['lead_time_days']} days. "
            f"A draft order for {decision['qty']} units was created for the manager to approve."
        )
        facts = (
            f"Product: {stock['sku']} ({stock['name']})\n"
            f"Units in stock: {stock['current_qty']}\n"
            f"Average sales per day (last {SALES_WINDOW_DAYS} days): {decision['avg_daily_sales']:.1f}\n"
            f"Days of stock left: {decision['days_left']:.1f}\n"
            f"Supplier delivery time: {stock['lead_time_days']} days\n"
            f"Quantity in the draft order: {decision['qty']}"
        )
    try:
        # Imported here so that even a broken AI library cannot stop the agent.
        from langchain_ollama import ChatOllama

        model = os.getenv("OLLAMA_MODEL")
        if os.getenv("LLM_PROVIDER", "ollama") != "ollama" or not model:
            return template

        llm = ChatOllama(model=model, temperature=0, client_kwargs={"timeout": 60})
        prompt = (
            "Write exactly two short plain-English sentences explaining why a purchase order "
            "was drafted. Reply with ONLY the two sentences, no introduction. "
            "Use only these facts and do not invent any numbers.\n" + facts
        )
        reply = str(llm.invoke(prompt).content)
        # Models often add an intro like "Here are two sentences:". Keep only the real sentences.
        lines = [line.strip() for line in reply.splitlines() if line.strip() and not line.strip().endswith(":")]
        reply = " ".join(lines)
        if reply:
            return reply
    except Exception:
        pass  # any AI problem -> use the template below
    return template


# ----------------------------------------------------------------------------
# 3. Checking ONE product: rules -> (maybe) purchase order + approval + log
# ----------------------------------------------------------------------------

def check_product(event: dict, stock: dict) -> dict:
    """Run the reorder rules for one product and write the database rows. Returns a short summary."""
    sku = stock["sku"]
    velocity = get_sales_velocity(stock["product_id"], SALES_WINDOW_DAYS)
    decision = decide_reorder(stock["current_qty"], velocity["units_sold"],
                              velocity["days"], stock["lead_time_days"], stock["reorder_threshold"])

    # Case A: the rules say no reorder is needed.
    if not decision["needs_reorder"]:
        if decision["note"]:
            why = f"{sku}: {decision['note']}."
        else:
            why = (f"{sku} has {decision['days_left']:.1f} days of stock left "
                   f"({stock['current_qty']} units, about {decision['avg_daily_sales']:.1f} sold per day), "
                   f"which is enough for the {stock['lead_time_days']}-day delivery time plus {SAFETY_DAYS} days.")
        add_decision_log(event["id"], [AGENT_NAME], why, "no reorder needed")
        return {"sku": sku, "outcome": "no reorder needed", "detail": why}

    # Case B: a reorder is needed, but an order is already waiting. Do not double up.
    existing = get_open_purchase_order(stock["product_id"])
    if existing:
        # Draft, approved and ordered purchase orders all count as "already on the way".
        if decision["days_left"] is None:
            need = f"{sku} needs a reorder (low stock, no recent sales)"
        else:
            need = f"{sku} needs a reorder ({decision['days_left']:.1f} days left)"
        article = "an" if existing["status"][0] in "aeiou" else "a"   # "an approved", "a draft"
        why = (f"{need}, but {article} {existing['status']} purchase order {existing['id']} "
               f"({existing['qty']} units) already exists, so no new order was created.")
        add_decision_log(event["id"], [AGENT_NAME], why, "skipped: open purchase order already exists")
        return {"sku": sku, "outcome": "skipped: open purchase order already exists", "detail": why}

    # Case C: create the draft purchase order and everything that goes with it.
    po = create_draft_po(stock["product_id"], decision["qty"], stock["supplier"], AGENT_NAME)
    action = None
    try:
        reason = write_reason(stock, decision)
        if decision["mode"] == "low_stock_no_sales":
            # Always say clearly WHY this order exists, whatever the AI wrote.
            reason = "Low-stock reorder with no recent sales. " + reason

        # agent_actions: what the agent did. Medium risk, so it waits for approval.
        action = supabase.table("agent_actions").insert({
            "event_id": event["id"],
            "agent_name": AGENT_NAME,
            "tool_called": "create_draft_po",
            "input": {
                "sku": sku,
                "product_id": stock["product_id"],
                "current_qty": stock["current_qty"],
                "units_sold_last_7_days": velocity["units_sold"],
                "avg_daily_sales": round(decision["avg_daily_sales"], 2),
                # days_left is None when nothing was sold (low-stock reorder with no recent sales)
                "days_left": None if decision["days_left"] is None else round(decision["days_left"], 2),
                "reorder_mode": decision["mode"],
                "reorder_threshold": stock["reorder_threshold"],
                "lead_time_days": stock["lead_time_days"],
            },
            "result": {
                "purchase_order_id": po["id"],
                "sku": sku,
                "qty": po["qty"],
                "supplier": po["supplier"],
                "status": po["status"],
            },
            "risk_tier": "medium",
            "status": "awaiting_approval",
        }).execute().data[0]

        # approvals: the manager's inbox. It starts as 'pending'.
        approval = supabase.table("approvals").insert({
            "agent_action_id": action["id"],
            "status": "pending",
        }).execute().data[0]

        # decision_log: the permanent record of why.
        add_decision_log(event["id"], [AGENT_NAME], reason, "draft PO created, waiting for approval")
    except Exception:
        # Do not leave a half-finished order behind that would block future orders.
        if action:
            supabase.table("approvals").delete().eq("agent_action_id", action["id"]).execute()
            supabase.table("agent_actions").delete().eq("id", action["id"]).execute()
        supabase.table("purchase_orders").delete().eq("id", po["id"]).execute()
        raise

    return {"sku": sku, "outcome": "draft PO created, waiting for approval", "detail": reason,
            "purchase_order_id": po["id"], "qty": po["qty"],
            "agent_action_id": action["id"], "approval_id": approval["id"]}


# ----------------------------------------------------------------------------
# 4. Working out WHICH products to check for an event
# ----------------------------------------------------------------------------

def products_to_check(event: dict):
    """
    Returns (list of stock dicts to check, list of notes).

    stock_low        -> the one product named in the payload.
    shelf_observation-> every product on the shelf whose observed count is below its reorder threshold.
    """
    payload = event["payload"] or {}
    notes = []
    stocks = []

    if event["type"] == "stock_low":
        sku, product_id = payload.get("sku"), payload.get("product_id")
        if not sku and not product_id:
            raise ValueError("stock_low payload needs a 'sku' or 'product_id'")
        stock = get_stock(sku=sku, product_id=None if sku else product_id)
        if not stock:
            raise ValueError(f"product not found: {sku or product_id}")
        stocks.append(stock)

    elif event["type"] == "shelf_observation":
        observations = payload.get("observations")
        if not isinstance(observations, list):
            raise ValueError("shelf_observation payload needs an 'observations' list")
        for obs in observations:
            if "sku" not in obs or "observed_count" not in obs:
                raise ValueError(f"each observation needs 'sku' and 'observed_count': {obs}")
            stock = get_stock(sku=obs["sku"])
            if not stock:
                notes.append(f"unknown SKU {obs['sku']}")
                continue
            # The shelf looks low when fewer units are seen than the reorder threshold.
            if obs["observed_count"] < stock["reorder_threshold"]:
                stocks.append(stock)

    return stocks, notes


# ----------------------------------------------------------------------------
# 5. Handling one event, and running the agent
# ----------------------------------------------------------------------------

def set_event_status(event_id: str, status: str):
    supabase.table("events").update({"status": status}).eq("id", event_id).execute()


def process_event(event: dict):
    """Handle one event from start to finish. Returns a summary, or None if someone else took it."""
    # Claim the event: only switch new -> processing if it is STILL new.
    claimed = (supabase.table("events").update({"status": "processing"})
               .eq("id", event["id"]).eq("status", "new").execute().data)
    if not claimed:
        return None

    try:
        stocks, notes = products_to_check(event)
        results = [check_product(event, stock) for stock in stocks]

        if not results:
            # Nothing to do still deserves a record.
            why = "No low products found in this event." + (f" Ignored: {'; '.join(notes)}." if notes else "")
            add_decision_log(event["id"], [AGENT_NAME], why, "no low products found")
            results.append({"sku": None, "outcome": "no low products found", "detail": why})
        elif notes:
            # Things we ignored (like unknown SKUs) must not disappear silently.
            why = "Ignored: " + "; ".join(notes) + "."
            add_decision_log(event["id"], [AGENT_NAME], why, "some items ignored")
            results.append({"sku": None, "outcome": "some items ignored", "detail": why})

        set_event_status(event["id"], "handled")
        return {"event_id": event["id"], "type": event["type"], "status": "handled", "results": results}

    except Exception as error:
        # Mark the event failed and write down why (decision_log is where reasons live).
        set_event_status(event["id"], "failed")
        try:
            add_decision_log(event["id"], [AGENT_NAME], f"Event failed: {type(error).__name__}: {error}",
                             "event failed")
        except Exception:
            pass  # if even the log fails, the 'failed' status is still set
        return {"event_id": event["id"], "type": event["type"], "status": "failed", "error": str(error),
                "results": []}


def run(event_ids: list = None) -> list:
    """
    Process every 'new' stock_low / shelf_observation event (oldest first).
    Pass event_ids to process only those events.
    """
    query = (supabase.table("events").select("*").eq("status", "new")
             .in_("type", EVENT_TYPES).order("created_at"))
    if event_ids:
        query = query.in_("id", event_ids)
    events = query.execute().data

    summaries = []
    for event in events:
        summary = process_event(event)
        if summary:
            summaries.append(summary)
    return summaries


if __name__ == "__main__":
    summaries = run()
    if not summaries:
        print("No new stock_low or shelf_observation events to handle.")
    for summary in summaries:
        print(f"Event {summary['event_id']} ({summary['type']}): {summary['status']}")
        if summary["status"] == "failed":
            print(f"   FAILED: {summary['error']}")
        for result in summary["results"]:
            print(f"   {result['sku'] or '-'}: {result['outcome']}")
            print(f"      {result['detail']}")
