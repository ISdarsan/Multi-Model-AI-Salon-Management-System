"""
inventory_tools.py - small database functions ("tools") used by the Inventory Agent.

Each function does ONE job and has no AI in it. The agent decides when to call them.
"""

from datetime import datetime, timedelta, timezone

from backend.db import supabase

# Which stock_transactions count as "stock going out" when we measure sales speed.
# The task said 'sale'. The demo data records colour tubes as 'use' (used up in
# services, not sold), so we count both. Remove "use" here if you only want 'sale'.
CONSUMPTION_TYPES = ["sale", "use"]

# A purchase order in one of these states means "an order is already on its way",
# so the agent must not create another one. Only 'rejected' orders are finished.
OPEN_PO_STATUSES = ["draft", "approved", "ordered"]


def get_stock(sku: str = None, product_id: str = None):
    """
    Look up ONE product together with its current stock.
    Give either a sku (like "COL-6N") or a product_id. Returns None if not found.
    """
    query = supabase.table("stock_levels").select(
        "current_qty, products!inner(id, sku, name, supplier, reorder_threshold, lead_time_days)"
    )
    if sku:
        query = query.eq("products.sku", sku)
    elif product_id:
        query = query.eq("product_id", product_id)
    else:
        raise ValueError("get_stock needs a sku or a product_id")

    rows = query.limit(1).execute().data
    if not rows:
        return None

    # Flatten the answer so it is easy to use: one dictionary, no nesting.
    product = rows[0]["products"]
    return {
        "product_id": product["id"],
        "sku": product["sku"],
        "name": product["name"],
        "supplier": product["supplier"],
        "reorder_threshold": product["reorder_threshold"],
        "lead_time_days": product["lead_time_days"],
        "current_qty": rows[0]["current_qty"],
    }


def get_sales_velocity(product_id: str, days: int = 7) -> dict:
    """
    How fast does this product go out? Looks at the last `days` days of
    stock_transactions and returns the total units and the average per day.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = (
        supabase.table("stock_transactions")
        .select("qty_change")
        .eq("product_id", product_id)
        .in_("type", CONSUMPTION_TYPES)
        .gte("created_at", since)
        .execute()
        .data
    )
    # Sales are stored as negative numbers (-2 means two units left), so use abs().
    units_sold = sum(abs(row["qty_change"]) for row in rows)
    return {
        "days": days,
        "units_sold": units_sold,
        "avg_daily_sales": units_sold / days,
    }


def get_open_purchase_order(product_id: str):
    """Return an existing open (draft, approved or ordered) purchase order for this product, or None."""
    rows = (
        supabase.table("purchase_orders")
        .select("*")
        .eq("product_id", product_id)
        .in_("status", OPEN_PO_STATUSES)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


def create_draft_po(product_id: str, qty: int, supplier: str, created_by_agent: str = "inventory_agent") -> dict:
    """Create a purchase order with status 'draft'. Nothing is ordered until a manager approves."""
    return supabase.table("purchase_orders").insert({
        "product_id": product_id,
        "qty": qty,
        "supplier": supplier,
        "status": "draft",
        "created_by_agent": created_by_agent,
    }).execute().data[0]
