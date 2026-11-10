"""
test_approval_api.py - tests POST /approvals/{id}/approve and /reject on your REAL database.

What it does:
  1. Starts the API server on port 8767 (a throwaway copy, so it does not clash with yours).
  2. Signs in as manager@salonos.demo and customer1@salonos.demo (password demo1234) using the
     PUBLISHABLE key from frontend/.env (the same key the browser uses). The key is never printed.
  3. Creates two temporary draft purchase orders with a pending approval each.
  4. Checks: no token -> 401, bad token -> 401, customer -> 403, manager approve -> 200, manager reject -> 200,
     reject without a reason -> 422, approving twice -> 409, unknown id -> 404, CORS for localhost:5173 only.
  5. Cleans up the orders, actions and approvals it created.

If signing in fails, the sign-in part is SKIPPED and the script says so.

Run it from the project root:   python -m backend.tests.test_approval_api

About cleanup: decision_log is append-only, so the test event (source = 'dashboard_test') and its
decision_log rows stay. Everything else is removed.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

from dotenv import dotenv_values
from supabase import create_client

from backend.db import SUPABASE_URL, supabase
from backend.tools.inventory_tools import create_draft_po, get_open_purchase_order, get_stock

PORT = 8767
BASE = f"http://127.0.0.1:{PORT}"
TEST_SOURCE = "dashboard_test"
created = {"approvals": [], "actions": [], "pos": []}


def check(label, condition):
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    assert condition, label


def call(method, path, token=None, body=None, headers=None):
    """Call the test server. Returns (status code, parsed JSON or text, response headers)."""
    all_headers = {"Content-Type": "application/json", **(headers or {})}
    if token:
        all_headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method, headers=all_headers)
    try:
        response = urllib.request.urlopen(request, timeout=30)
        raw, status, response_headers = response.read(), response.status, response.headers
    except urllib.error.HTTPError as error:
        raw, status, response_headers = error.read(), error.code, error.headers
    try:
        return status, json.loads(raw), response_headers
    except ValueError:
        return status, raw.decode(errors="replace"), response_headers


def sign_in(anon_key, email):
    """Sign in with email + password and return the access token (or None if it fails)."""
    try:
        client = create_client(SUPABASE_URL, anon_key)
        result = client.auth.sign_in_with_password({"email": email, "password": "demo1234"})
        return result.session.access_token
    except Exception as error:
        print(f"      (sign-in for {email} failed: {type(error).__name__}: {error})")
        return None


def make_pending_approval(sku, qty):
    """Create a draft purchase order and a pending approval for it, like the Inventory Agent does."""
    stock = get_stock(sku=sku)
    assert not get_open_purchase_order(stock["product_id"]), f"{sku} already has an open purchase order"
    event = supabase.table("events").insert({
        "type": "stock_low", "source": TEST_SOURCE, "status": "handled",
        "payload": {"sku": sku},
    }).execute().data[0]
    po = create_draft_po(stock["product_id"], qty, stock["supplier"], "inventory_agent")
    action = supabase.table("agent_actions").insert({
        "event_id": event["id"], "agent_name": "inventory_agent", "tool_called": "create_draft_po",
        "input": {"sku": sku}, "result": {"purchase_order_id": po["id"], "sku": sku, "qty": qty,
                                          "supplier": stock["supplier"], "status": "draft"},
        "risk_tier": "medium", "status": "awaiting_approval",
    }).execute().data[0]
    approval = supabase.table("approvals").insert({"agent_action_id": action["id"], "status": "pending"}).execute().data[0]
    created["pos"].append(po["id"]); created["actions"].append(action["id"]); created["approvals"].append(approval["id"])
    return approval["id"], action["id"], po["id"]


def row(table, row_id):
    return supabase.table(table).select("*").eq("id", row_id).execute().data[0]


def main():
    # The browser's key (publishable) lives in frontend/.env. We only read it here, never print it.
    frontend_env = dotenv_values(os.path.join(os.path.dirname(__file__), "..", "..", "frontend", ".env"))
    anon_key = frontend_env.get("VITE_SUPABASE_ANON_KEY")
    if not anon_key:
        raise SystemExit("frontend/.env has no VITE_SUPABASE_ANON_KEY")

    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.api.main:app", "--port", str(PORT)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(BASE + "/health", timeout=1)
                break
            except Exception:
                time.sleep(0.5)

        print("== 1. basics ==")
        check("GET /health -> 200", call("GET", "/health")[0] == 200)
        check("POST /events still needs the X-API-Key (no key -> 401)",
              call("POST", "/events", body={"type": "stock_low", "source": "x", "payload": {}})[0] == 401)

        approval_a, action_a, po_a = make_pending_approval("COL-6N", 11)
        approval_b, action_b, po_b = make_pending_approval("COL-7N", 12)
        print(f"   created 2 temporary pending approvals ({approval_a[:8]}, {approval_b[:8]})")

        print("\n== 2. no token / bad token ==")
        status, body, _ = call("POST", f"/approvals/{approval_a}/approve")
        check("approve with NO token -> 401", status == 401)
        print("      message:", body["detail"])
        check("reject with NO token -> 401", call("POST", f"/approvals/{approval_a}/reject", body={"reason": "x"})[0] == 401)
        check("approve with a garbage token -> 401", call("POST", f"/approvals/{approval_a}/approve", token="not-a-real-token")[0] == 401)
        check("a header that is not 'Bearer ...' -> 401",
              call("POST", f"/approvals/{approval_a}/approve", headers={"Authorization": "Basic abc"})[0] == 401)
        check("nothing changed: approval A is still pending", row("approvals", approval_a)["status"] == "pending")

        print("\n== 3. CORS (only the dashboard's address may call from a browser) ==")
        preflight = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"}
        status, _, headers = call("OPTIONS", f"/approvals/{approval_a}/approve", headers={"Origin": "http://localhost:5173", **preflight})
        check("origin http://localhost:5173 is allowed", status == 200 and headers.get("access-control-allow-origin") == "http://localhost:5173")
        status, _, headers = call("OPTIONS", f"/approvals/{approval_a}/approve", headers={"Origin": "http://evil.example", **preflight})
        check("another origin is NOT allowed", headers.get("access-control-allow-origin") is None)

        print("\n== 4. signing in ==")
        manager_token = sign_in(anon_key, "manager@salonos.demo")
        customer_token = sign_in(anon_key, "customer1@salonos.demo")
        if not manager_token:
            print("SKIPPED: could not sign in as manager@salonos.demo, so the manager and customer checks were not run.")
            return
        print("   signed in as manager" + (" and customer1" if customer_token else " (customer1 sign-in failed)"))

        if customer_token:
            print("\n== 5. a customer is refused ==")
            status, body, _ = call("POST", f"/approvals/{approval_a}/approve", token=customer_token)
            check("customer approve -> 403", status == 403)
            print("      message:", body["detail"])
            check("customer reject -> 403", call("POST", f"/approvals/{approval_a}/reject", token=customer_token, body={"reason": "x"})[0] == 403)
            check("nothing changed: approval A is still pending", row("approvals", approval_a)["status"] == "pending")
        else:
            print("SKIPPED: customer checks (customer1 could not sign in)")

        print("\n== 6. manager: reject needs a reason ==")
        check("reject with NO body -> 422", call("POST", f"/approvals/{approval_b}/reject", token=manager_token)[0] == 422)
        check("reject with an empty reason -> 422", call("POST", f"/approvals/{approval_b}/reject", token=manager_token, body={"reason": ""})[0] == 422)
        status, body, _ = call("POST", f"/approvals/{approval_b}/reject", token=manager_token, body={"reason": "   "})
        check("reject with a blank reason -> 422", status == 422)
        print("      message:", body["detail"])
        check("approval B is still pending", row("approvals", approval_b)["status"] == "pending")

        print("\n== 7. manager approves A and rejects B ==")
        status, body, _ = call("POST", f"/approvals/{approval_a}/approve", token=manager_token)
        print("      response:", body["outcome"] if status == 200 else body)
        manager_id = row("approvals", approval_a)["decided_by"]
        check("approve -> 200 with outcome 'purchase order approved'", status == 200 and body["outcome"] == "purchase order approved")
        check("approval 'approved', decided_by is the manager's user id (from the token)",
              row("approvals", approval_a)["status"] == "approved" and manager_id == supabase.table("profiles").select("id").eq("role", "manager").limit(1).execute().data[0]["id"])
        check("purchase order 'approved', action 'executed'",
              row("purchase_orders", po_a)["status"] == "approved" and row("agent_actions", action_a)["status"] == "executed")

        status, body, _ = call("POST", f"/approvals/{approval_b}/reject", token=manager_token, body={"reason": "Too expensive this week"})
        print("      response:", body["outcome"] if status == 200 else body)
        check("reject -> 200 with outcome 'purchase order rejected'", status == 200 and body["outcome"] == "purchase order rejected")
        check("approval 'rejected' with the reason, order 'rejected', action 'rejected'",
              row("approvals", approval_b)["status"] == "rejected" and row("approvals", approval_b)["reason"] == "Too expensive this week"
              and row("purchase_orders", po_b)["status"] == "rejected" and row("agent_actions", action_b)["status"] == "rejected")

        print("\n== 8. clear errors ==")
        status, body, _ = call("POST", f"/approvals/{approval_a}/approve", token=manager_token)
        check("approving the same approval again -> 409", status == 409)
        print("      message:", body["detail"])
        status, body, _ = call("POST", f"/approvals/{uuid.uuid4()}/approve", token=manager_token)
        check("an approval that does not exist -> 404", status == 404)
        print("      message:", body["detail"])
        check("a path that is not a uuid -> 422", call("POST", "/approvals/not-a-uuid/approve", token=manager_token)[0] == 422)

        print("\nALL CHECKS PASSED")
    finally:
        server.terminate()
        print("\n== CLEANUP ==")
        for table, ids in (("approvals", created["approvals"]), ("agent_actions", created["actions"]),
                           ("purchase_orders", created["pos"])):
            for row_id in ids:
                supabase.table(table).delete().eq("id", row_id).execute()
            print(f"  deleted {len(ids)} row(s) from {table}")
        print("  KEPT the test event(s) (source = 'dashboard_test') and decision_log rows: decision_log is append-only.")


if __name__ == "__main__":
    main()
