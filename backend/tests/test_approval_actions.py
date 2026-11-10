"""
test_approval_actions.py - checks approve() and reject() OFFLINE, with a tiny fake database in memory.
It never touches the real database.

It proves two things:
  - Inventory Agent approvals (create_draft_po) still work exactly as before.
  - Scheduling Agent approvals (reassign_staff) change the appointment only on approve.

Run it from the project root:   python -m backend.tests.test_approval_actions
"""

import copy

import backend.approvals.approval_actions as aa


# ----------------------------------------------------------------------------
# A very small fake database: tables are lists of dicts; supports the calls approval_actions uses.
# ----------------------------------------------------------------------------

class Result:
    def __init__(self, data):
        self.data = data


class Query:
    def __init__(self, db, table):
        self.db, self.table, self.op, self.values, self.filters = db, table, "select", None, []

    def select(self, *args, **kwargs): self.op = "select"; return self
    def update(self, values): self.op, self.values = "update", values; return self
    def eq(self, column, value): self.filters.append(lambda row: row.get(column) == value); return self
    def limit(self, n): return self
    def order(self, *args, **kwargs): return self

    def execute(self):
        matched = [row for row in self.db[self.table] if all(f(row) for f in self.filters)]
        if self.op == "update":
            for row in matched:
                row.update(self.values)
        return Result(copy.deepcopy(matched))


class FakeDB(dict):
    def table(self, name): return Query(self, name)


MANAGER, CUSTOMER = "u-manager", "u-customer"
logs = []


def fresh():
    """A new fake database with one Inventory proposal and two Scheduling proposals, all pending."""
    db = FakeDB(
        profiles=[{"id": MANAGER, "role": "manager"}, {"id": CUSTOMER, "role": "customer"}],
        purchase_orders=[{"id": "po1", "status": "draft"}],
        appointments=[{"id": "ap1", "staff_id": "aria", "status": "booked"},
                      {"id": "ap2", "staff_id": "aria", "status": "booked"}],
        agent_actions=[
            {"id": "act-inv", "event_id": "ev1", "agent_name": "inventory_agent", "tool_called": "create_draft_po",
             "status": "awaiting_approval", "input": {}, "result": {"purchase_order_id": "po1", "sku": "COL-6N", "qty": 16}},
            {"id": "act-s1", "event_id": "ev2", "agent_name": "scheduling_agent", "tool_called": "reassign_staff",
             "status": "awaiting_approval", "result": {"proposed": True, "applied": False},
             "input": {"appointment_id": "ap1", "service": "Men's Cut", "start_time": "2030-01-01T09:30:00+00:00",
                       "old_staff_id": "aria", "old_staff_name": "Aria", "new_staff_id": "ben", "new_staff_name": "Ben"}},
            {"id": "act-s2", "event_id": "ev2", "agent_name": "scheduling_agent", "tool_called": "reassign_staff",
             "status": "awaiting_approval", "result": {"proposed": True, "applied": False},
             "input": {"appointment_id": "ap2", "service": "Men's Cut", "start_time": "2030-01-01T12:30:00+00:00",
                       "old_staff_id": "aria", "old_staff_name": "Aria", "new_staff_id": "dev", "new_staff_name": "Dev"}},
        ],
        approvals=[{"id": f"apr-{n}", "agent_action_id": f"act-{n}", "status": "pending", "decided_by": None,
                    "decided_at": None, "reason": None} for n in ("inv", "s1", "s2")],
    )
    logs.clear()
    aa.supabase = db
    aa.add_decision_log = lambda event_id, agents, reasoning, outcome: (
        logs.append({"event_id": event_id, "agents": agents, "reasoning": reasoning, "outcome": outcome}) or logs[-1])
    aa.get_appointment = lambda appointment_id: next(
        (copy.deepcopy(a) for a in db["appointments"] if a["id"] == appointment_id), None)

    def fake_reassign(appointment_id, new_staff_id):
        if new_staff_id == "busy":
            raise ValueError("could not reassign: new stylist is already busy (double-booking)")
        row = next(a for a in db["appointments"] if a["id"] == appointment_id)
        row["staff_id"] = new_staff_id
        return copy.deepcopy(row)

    aa.reassign_staff = fake_reassign
    return db


def row(db, table, row_id): return next(r for r in db[table] if r["id"] == row_id)


def check(label, condition):
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    assert condition, label


def raises(exception, function, *args):
    try:
        function(*args)
    except exception as error:
        return str(error)
    return None


def main():
    print("== Inventory Agent approvals (must work exactly as before) ==")
    db = fresh()
    changes = aa.approve("apr-inv", MANAGER)
    check("approve returns the same keys as before", set(changes) == {"approval", "purchase_order", "agent_action", "decision_log"})
    check("approval 'approved' with decided_by and decided_at", row(db, "approvals", "apr-inv")["status"] == "approved"
          and row(db, "approvals", "apr-inv")["decided_by"] == MANAGER and row(db, "approvals", "apr-inv")["decided_at"])
    check("purchase order 'approved', action 'executed'", row(db, "purchase_orders", "po1")["status"] == "approved"
          and row(db, "agent_actions", "act-inv")["status"] == "executed")
    check("decision_log: 'purchase order approved'", logs[-1]["outcome"] == "purchase order approved" and "COL-6N (16 units)" in logs[-1]["reasoning"])
    db = fresh()
    changes = aa.reject("apr-inv", MANAGER, "too expensive")
    check("reject: approval 'rejected' with the reason, order 'rejected', action 'rejected'",
          row(db, "approvals", "apr-inv")["status"] == "rejected" and row(db, "approvals", "apr-inv")["reason"] == "too expensive"
          and row(db, "purchase_orders", "po1")["status"] == "rejected" and row(db, "agent_actions", "act-inv")["status"] == "rejected")
    check("decision_log: 'purchase order rejected' with the reason",
          logs[-1]["outcome"] == "purchase order rejected" and "too expensive" in logs[-1]["reasoning"])

    print("\n== Scheduling Agent approvals ==")
    db = fresh()
    changes = aa.approve("apr-s1", MANAGER)
    check("approve returns approval, appointment, agent_action, decision_log", set(changes) == {"approval", "appointment", "agent_action", "decision_log"})
    check("the appointment's stylist changed to Ben", row(db, "appointments", "ap1")["staff_id"] == "ben")
    check("approval 'approved', action 'executed' with applied = true",
          row(db, "approvals", "apr-s1")["status"] == "approved" and row(db, "agent_actions", "act-s1")["status"] == "executed"
          and row(db, "agent_actions", "act-s1")["result"]["applied"] is True)
    check("decision_log: 'reassignment approved and applied'", logs[-1]["outcome"] == "reassignment approved and applied"
          and "from Aria to Ben" in logs[-1]["reasoning"])
    check("the OTHER proposal's appointment is untouched", row(db, "appointments", "ap2")["staff_id"] == "aria")

    db = fresh()
    changes = aa.reject("apr-s2", MANAGER, "Customer wants their usual stylist")
    check("reject: the appointment is NOT changed", row(db, "appointments", "ap2")["staff_id"] == "aria"
          and changes["appointment"]["staff_id"] == "aria")
    check("approval 'rejected' with the reason, action 'rejected'", row(db, "approvals", "apr-s2")["reason"] == "Customer wants their usual stylist"
          and row(db, "agent_actions", "act-s2")["status"] == "rejected")
    check("decision_log: 'reassignment rejected' with the reason", logs[-1]["outcome"] == "reassignment rejected"
          and "usual stylist" in logs[-1]["reasoning"])

    print("\n== Safety checks ==")
    db = fresh(); aa.approve("apr-s1", MANAGER)
    check("approving the same approval twice is refused", "already approved" in (raises(ValueError, aa.approve, "apr-s1", MANAGER) or ""))
    db = fresh()
    check("a customer cannot approve", raises(PermissionError, aa.approve, "apr-s1", CUSTOMER) is not None
          and row(db, "appointments", "ap1")["staff_id"] == "aria")
    check("reject needs a reason", raises(ValueError, aa.reject, "apr-s1", MANAGER, "  ") is not None)
    db = fresh(); row(db, "appointments", "ap1")["staff_id"] = "cleo"   # someone already moved it
    message = raises(ValueError, aa.approve, "apr-s1", MANAGER)
    check("a proposal that is out of date is refused, approval stays pending",
          message and "already changed" in message and row(db, "approvals", "apr-s1")["status"] == "pending")
    db = fresh(); row(db, "agent_actions", "act-s1")["input"]["new_staff_id"] = "busy"   # new stylist became busy
    message = raises(ValueError, aa.approve, "apr-s1", MANAGER)
    check("if the new stylist became busy, nothing changes and the approval stays pending",
          message and "busy" in message and row(db, "appointments", "ap1")["staff_id"] == "aria"
          and row(db, "approvals", "apr-s1")["status"] == "pending" and row(db, "agent_actions", "act-s1")["status"] == "awaiting_approval")
    db = fresh(); row(db, "appointments", "ap1")["status"] = "cancelled"
    check("a cancelled appointment cannot be reassigned", "cancelled" in (raises(ValueError, aa.approve, "apr-s1", MANAGER) or ""))

    print("\nALL OFFLINE APPROVAL CHECKS PASSED (no database rows written)")


if __name__ == "__main__":
    main()
