"""
test_approval_actions.py - checks approve() and reject() OFFLINE, with a tiny fake database in memory.
It never touches the real database.

It proves two things:
  - Inventory Agent approvals (create_draft_po) still work exactly as before.
  - Scheduling Agent approvals (reassign_staff) change the appointment only on approve.
  - Orchestrator approvals (hold_appointment) make the appointment 'held' only on approve.

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
        tickets=[{"id": "tk1", "status": "open", "resolution_notes": None}],
        appointments=[{"id": "ap1", "staff_id": "aria", "status": "booked"},
                      {"id": "ap2", "staff_id": "aria", "status": "booked"},
                      {"id": "ap3", "staff_id": "ben", "status": "booked"},
                      {"id": "ap4", "staff_id": "ben", "status": "booked",
                       "start_time": "2030-01-01T10:00:00+00:00", "end_time": "2030-01-01T10:30:00+00:00"}],
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
            {"id": "act-h1", "event_id": "ev3", "agent_name": "orchestrator", "tool_called": "hold_appointment",
             "status": "awaiting_approval", "result": {"proposed": True, "applied": False, "new_status": "held"},
             "input": {"appointment_id": "ap3", "service": "Full Colour", "start_time": "2030-01-01T09:30:00+00:00",
                       "sku": "COL-6N", "current_status": "booked"}},
            {"id": "act-a1", "event_id": "ev5", "agent_name": "inventory_agent", "tool_called": "raise_audit_flag",
             "status": "awaiting_approval", "result": {"proposed": True, "stock_changed": False},
             "input": {"sku": "SKN-001", "observed_count": 0, "db_qty": 8, "difference": -8}},
            {"id": "act-sh1", "event_id": "ev6", "agent_name": "scheduling_agent", "tool_called": "shift_appointment",
             "status": "awaiting_approval", "result": {"proposed": True, "applied": False},
             "input": {"appointment_id": "ap4", "service": "Men's Cut", "staff_name": "Ben", "minutes": 30,
                       "old_start": "2030-01-01T10:00:00+00:00", "old_end": "2030-01-01T10:30:00+00:00",
                       "new_start": "2030-01-01T10:30:00+00:00", "new_end": "2030-01-01T11:00:00+00:00"}},
            {"id": "act-n1", "event_id": "ev6", "agent_name": "cx_agent", "tool_called": "draft_customer_notice",
             "status": "awaiting_approval", "result": {"draft_text": "We are sorry, your appointment moved.", "sent": False},
             "input": {"appointment_id": "ap4", "customer_id": "c1", "kind": "delay", "service": "Men's Cut"}},
            {"id": "act-r1", "event_id": "ev4", "agent_name": "cx_agent", "tool_called": "draft_reply",
             "status": "awaiting_approval", "result": {"draft_text": "We are sorry. A manager will contact you.", "sent": False},
             "input": {"ticket_id": "tk1", "feedback_id": "fb1", "service": "Full Colour"}},
            {"id": "act-f1", "event_id": "ev4", "agent_name": "cx_agent", "tool_called": "suggest_refund",
             "status": "awaiting_approval", "result": {"suggested_amount": 60.0, "applied": False},
             "input": {"ticket_id": "tk1", "feedback_id": "fb1", "service": "Full Colour", "percent": 50, "basis": "Refund Policy"}},
        ],
        approvals=[{"id": f"apr-{n}", "agent_action_id": f"act-{n}", "status": "pending", "decided_by": None,
                    "decided_at": None, "reason": None} for n in ("inv", "s1", "s2", "h1", "r1", "f1", "a1", "sh1", "n1")],
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

    def fake_hold(appointment_id):
        row = next(a for a in db["appointments"] if a["id"] == appointment_id)
        if row["status"] != "booked":
            raise ValueError(f"appointment {appointment_id} is no longer 'booked', so it cannot be put on hold")
        row["status"] = "held"
        return copy.deepcopy(row)

    def fake_release(appointment_id):
        next(a for a in db["appointments"] if a["id"] == appointment_id)["status"] = "booked"

    aa.hold_appointment, aa.release_hold = fake_hold, fake_release

    def fake_get_ticket(ticket_id):
        return next((copy.deepcopy(t) for t in db["tickets"] if t["id"] == ticket_id), None)

    def fake_resolve(ticket_id, notes):
        ticket = next(t for t in db["tickets"] if t["id"] == ticket_id)
        ticket.update(status="resolved", resolution_notes=notes)
        return copy.deepcopy(ticket)

    def fake_restore(ticket_id, status, notes):
        next(t for t in db["tickets"] if t["id"] == ticket_id).update(status=status, resolution_notes=notes)

    aa.get_ticket, aa.resolve_ticket, aa.restore_ticket = fake_get_ticket, fake_resolve, fake_restore

    def fake_shift(appointment_id, new_start, new_end):
        if new_start == "BUSY":
            raise ValueError("could not shift: the stylist is already busy (double-booking)")
        row = next(a for a in db["appointments"] if a["id"] == appointment_id)
        row["start_time"], row["end_time"] = new_start, new_end
        return copy.deepcopy(row)

    aa.shift_appointment = fake_shift
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

    print("\n== Orchestrator approvals (hold_appointment) ==")
    db = fresh()
    changes = aa.approve("apr-h1", MANAGER)
    check("approve returns approval, appointment, agent_action, decision_log", set(changes) == {"approval", "appointment", "agent_action", "decision_log"})
    check("the appointment became 'held'", row(db, "appointments", "ap3")["status"] == "held" and changes["appointment"]["status"] == "held")
    check("approval 'approved', action 'executed' with applied = true",
          row(db, "approvals", "apr-h1")["status"] == "approved" and row(db, "agent_actions", "act-h1")["status"] == "executed"
          and row(db, "agent_actions", "act-h1")["result"]["applied"] is True)
    check("decision_log: 'hold approved and applied'", logs[-1]["outcome"] == "hold approved and applied"
          and logs[-1]["agents"] == ["orchestrator"] and "COL-6N" in logs[-1]["reasoning"])
    check("other appointments are untouched", row(db, "appointments", "ap1")["status"] == "booked")

    db = fresh()
    changes = aa.reject("apr-h1", MANAGER, "Supplier delivers tomorrow, keep the booking")
    check("reject: the appointment is NOT changed", row(db, "appointments", "ap3")["status"] == "booked"
          and changes["appointment"]["status"] == "booked")
    check("approval 'rejected' with the reason, action 'rejected'",
          row(db, "approvals", "apr-h1")["reason"] == "Supplier delivers tomorrow, keep the booking"
          and row(db, "agent_actions", "act-h1")["status"] == "rejected")
    check("decision_log: 'hold rejected' with the reason", logs[-1]["outcome"] == "hold rejected" and "keep the booking" in logs[-1]["reasoning"])

    for status in ("held", "cancelled", "completed", "in_progress"):
        db = fresh(); row(db, "appointments", "ap3")["status"] = status
        message = raises(ValueError, aa.approve, "apr-h1", MANAGER)
        check(f"approval is REFUSED when the appointment is already '{status}' (stays pending, nothing changes)",
              message and "not 'booked'" in message and row(db, "approvals", "apr-h1")["status"] == "pending"
              and row(db, "appointments", "ap3")["status"] == status)
    db = fresh()
    check("a customer cannot approve a hold", raises(PermissionError, aa.approve, "apr-h1", CUSTOMER) is not None
          and row(db, "appointments", "ap3")["status"] == "booked")
    db = fresh(); aa.approve("apr-h1", MANAGER)
    check("approving a hold twice is refused", "already approved" in (raises(ValueError, aa.approve, "apr-h1", MANAGER) or ""))

    print("\n== CX Agent approvals (draft_reply and suggest_refund) ==")
    db = fresh()
    changes = aa.approve("apr-r1", MANAGER)
    check("approve draft_reply returns approval, agent_action, decision_log, ticket", set(changes) == {"approval", "agent_action", "decision_log", "ticket"})
    check("the ticket is now 'resolved' and its notes keep the approved draft", row(db, "tickets", "tk1")["status"] == "resolved"
          and "We are sorry" in row(db, "tickets", "tk1")["resolution_notes"])
    check("approval 'approved', action 'executed', applied = true, still NOT sent",
          row(db, "approvals", "apr-r1")["status"] == "approved" and row(db, "agent_actions", "act-r1")["status"] == "executed"
          and row(db, "agent_actions", "act-r1")["result"]["applied"] is True and row(db, "agent_actions", "act-r1")["result"]["sent"] is False)
    check("decision_log: 'reply approved, ticket resolved' and it says nothing was sent",
          logs[-1]["outcome"] == "reply approved, ticket resolved" and "Nothing was sent" in logs[-1]["reasoning"]
          and logs[-1]["agents"] == ["cx_agent"])
    db = fresh()
    changes = aa.approve("apr-f1", MANAGER)
    check("approve suggest_refund resolves the ticket and records the amount",
          row(db, "tickets", "tk1")["status"] == "resolved" and "60.0" in row(db, "tickets", "tk1")["resolution_notes"]
          and "50% refund" in row(db, "tickets", "tk1")["resolution_notes"])
    check("decision_log: 'refund approved, ticket resolved'", logs[-1]["outcome"] == "refund approved, ticket resolved")
    db = fresh()
    changes = aa.reject("apr-f1", MANAGER, "Not justified, the colour was as agreed")
    check("reject suggest_refund: the ticket is NOT changed (still open)", row(db, "tickets", "tk1")["status"] == "open"
          and changes["ticket"]["status"] == "open")
    check("reject: approval 'rejected' with the reason, action 'rejected', outcome 'refund rejected'",
          row(db, "approvals", "apr-f1")["reason"] == "Not justified, the colour was as agreed"
          and row(db, "agent_actions", "act-f1")["status"] == "rejected" and logs[-1]["outcome"] == "refund rejected"
          and "as agreed" in logs[-1]["reasoning"])
    db = fresh()
    aa.reject("apr-r1", MANAGER, "Wording is too cold")
    check("reject draft_reply: outcome 'reply rejected', ticket unchanged", logs[-1]["outcome"] == "reply rejected"
          and row(db, "tickets", "tk1")["status"] == "open")
    db = fresh(); db["tickets"].clear()
    message = raises(ValueError, aa.approve, "apr-f1", MANAGER)
    check("a missing ticket refuses the approval (it stays pending)", message and "not found" in message
          and row(db, "approvals", "apr-f1")["status"] == "pending")
    db = fresh(); aa.approve("apr-f1", MANAGER)
    check("approving a refund twice is refused", "already approved" in (raises(ValueError, aa.approve, "apr-f1", MANAGER) or ""))
    db = fresh()
    check("a customer cannot approve a refund", raises(PermissionError, aa.approve, "apr-f1", CUSTOMER) is not None
          and row(db, "tickets", "tk1")["status"] == "open")
    db = fresh()
    original_decide = aa._decide
    aa._decide = lambda *a, **k: (_ for _ in ()).throw(ValueError("decided by someone else"))
    raises(ValueError, aa.approve, "apr-f1", MANAGER)
    aa._decide = original_decide
    check("if the approval fails half way, the ticket is put back (open)", row(db, "tickets", "tk1")["status"] == "open"
          and row(db, "tickets", "tk1")["resolution_notes"] is None)

    print("\n== Inventory audit flag (raise_audit_flag) ==")
    db = fresh()
    changes = aa.approve("apr-a1", MANAGER)
    check("approve marks the flag resolved: approval 'approved', action 'executed', outcome 'audit flag approved and resolved'",
          row(db, "approvals", "apr-a1")["status"] == "approved" and row(db, "agent_actions", "act-a1")["status"] == "executed"
          and logs[-1]["outcome"] == "audit flag approved and resolved" and "NOT changed" in logs[-1]["reasoning"])
    check("the stock was not touched (no stock tables exist in this fake database and nothing else changed)",
          set(db.keys()) >= {"appointments"} and row(db, "appointments", "ap4")["start_time"] == "2030-01-01T10:00:00+00:00")
    db = fresh()
    aa.reject("apr-a1", MANAGER, "Counted by hand: the database is right")
    check("reject: outcome 'audit flag rejected', reason logged, action 'rejected'", logs[-1]["outcome"] == "audit flag rejected"
          and "database is right" in logs[-1]["reasoning"] and row(db, "agent_actions", "act-a1")["status"] == "rejected")

    print("\n== Scheduling shift (shift_appointment) ==")
    db = fresh()
    changes = aa.approve("apr-sh1", MANAGER)
    check("approve returns approval, appointment, agent_action, decision_log", set(changes) == {"approval", "appointment", "agent_action", "decision_log"})
    check("the appointment moved 30 minutes later (10:30-11:00)", row(db, "appointments", "ap4")["start_time"] == "2030-01-01T10:30:00+00:00"
          and row(db, "appointments", "ap4")["end_time"] == "2030-01-01T11:00:00+00:00")
    check("approval 'approved', action 'executed' with applied = true, outcome 'shift approved and applied'",
          row(db, "approvals", "apr-sh1")["status"] == "approved" and row(db, "agent_actions", "act-sh1")["result"]["applied"] is True
          and logs[-1]["outcome"] == "shift approved and applied")
    db = fresh()
    changes = aa.reject("apr-sh1", MANAGER, "The customer is already in the chair")
    check("reject: the appointment is NOT changed, reason logged, outcome 'shift rejected'",
          row(db, "appointments", "ap4")["start_time"] == "2030-01-01T10:00:00+00:00" and logs[-1]["outcome"] == "shift rejected"
          and "already in the chair" in logs[-1]["reasoning"] and row(db, "agent_actions", "act-sh1")["status"] == "rejected")
    db = fresh(); row(db, "appointments", "ap4")["start_time"] = "2030-01-01T10:15:00+00:00"
    message = raises(ValueError, aa.approve, "apr-sh1", MANAGER)
    check("a proposal that is out of date (the time already changed) is refused, approval stays pending",
          message and "already changed" in message and row(db, "approvals", "apr-sh1")["status"] == "pending")
    db = fresh(); row(db, "agent_actions", "act-sh1")["input"]["new_start"] = "BUSY"
    message = raises(ValueError, aa.approve, "apr-sh1", MANAGER)
    check("if the stylist became busy, the shift is refused: nothing changes and the approval stays pending",
          message and "busy" in message and row(db, "appointments", "ap4")["start_time"] == "2030-01-01T10:00:00+00:00"
          and row(db, "approvals", "apr-sh1")["status"] == "pending")
    for status in ("cancelled", "completed", "in_progress"):
        db = fresh(); row(db, "appointments", "ap4")["status"] = status
        check(f"a '{status}' appointment cannot be moved", "cannot be moved" in (raises(ValueError, aa.approve, "apr-sh1", MANAGER) or ""))
    db = fresh()
    original_decide = aa._decide
    aa._decide = lambda *a, **k: (_ for _ in ()).throw(ValueError("decided by someone else"))
    raises(ValueError, aa.approve, "apr-sh1", MANAGER)
    aa._decide = original_decide
    check("if the approval fails half way, the appointment is moved back", row(db, "appointments", "ap4")["start_time"] == "2030-01-01T10:00:00+00:00")
    db = fresh()
    check("a customer cannot approve a shift", raises(PermissionError, aa.approve, "apr-sh1", CUSTOMER) is not None)

    print("\n== Customer notice (draft_customer_notice) ==")
    db = fresh()
    changes = aa.approve("apr-n1", MANAGER)
    check("approve: action 'executed', the draft is still NOT sent, outcome 'customer notice approved (not sent)'",
          row(db, "agent_actions", "act-n1")["status"] == "executed" and row(db, "agent_actions", "act-n1")["result"]["sent"] is False
          and logs[-1]["outcome"] == "customer notice approved (not sent)" and "Nothing was sent" in logs[-1]["reasoning"])
    db = fresh()
    aa.reject("apr-n1", MANAGER, "Too long")
    check("reject: outcome 'customer notice rejected', reason logged", logs[-1]["outcome"] == "customer notice rejected" and "Too long" in logs[-1]["reasoning"])

    print("\nALL OFFLINE APPROVAL CHECKS PASSED (no database rows written)")


if __name__ == "__main__":
    main()
