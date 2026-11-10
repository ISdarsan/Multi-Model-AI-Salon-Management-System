# Scenario test results

Run on 2026-11-10 19:29 UTC by `python -m backend.scenarios.run_scenarios` against the real database, with temporary test data.
The five cross-agent scenarios are the acceptance test from `docs/ARCHITECTURE.md` (section 9).

**5 of 5 scenarios passed.**

| # | Scenario | Agents | Result | Checks | Latency (s) | Under 10 s target |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Colour stock low + colour service booked: clash, hold, draft order | Inventory, Scheduling, Orchestrator | **PASS** | 10/10 | 16.3 | no |
| 2 | Stylist absent: reassignment + customer notice | Scheduling, CX | **PASS** | 8/8 | 13.0 | no |
| 3 | Customer complaint: ticket, service log, refund (manager only) | CX, Quality | **PASS** | 9/9 | 7.2 | yes |
| 4 | Shelf says empty, database says stock: audit flag | Inventory | **PASS** | 7/7 | 8.7 | yes |
| 5 | Waiting area overloaded: shifts + delay notices | Scheduling, CX | **PASS** | 8/8 | 21.8 | no |

Latency is the time from sending the event to all proposals being written (it includes the AI writing the reasons, drafts and notices with the local Ollama model). The 10-second target is a proposed KPI, so it is reported separately and does not decide PASS or FAIL.

## Details

### Scenario 1: Colour stock low + colour service booked: clash, hold, draft order

- PASS: the event was handled (not failed)
- PASS: Inventory Agent: a draft purchase order for COL-7N was proposed (medium risk) with a pending approval
- PASS: Orchestrator: clash detected, 1 hold proposal(s) for exactly the earliest clashing bookings
- PASS: decision_log: one row per hold, naming orchestrator, inventory_agent and scheduling_agent, with the real numbers
- PASS: decision_log: the Inventory Agent's row for the draft order
- PASS: nothing has changed yet: the clashing bookings are still 'booked'
- PASS: manager approves the order: purchase order 'approved', action 'executed'
- PASS: manager approves the hold: the booking becomes 'held'
- PASS: the other proposed holds are untouched (still 'booked')
- PASS: the stock level was not changed

_Cleanup:_ appointments restored: 1, actions (with approvals) deleted: 2, purchase orders deleted: 1, feedback rows deleted: 0, temporary appointments deleted: 2, events deleted: 2, events kept (they have append-only log rows): 1

### Scenario 2: Stylist absent: reassignment + customer notice

- PASS: the event was handled (not failed)
- PASS: Scheduling Agent: one reassignment proposal (medium risk) for the booking, pending approval
- PASS: the replacement (Ben Sample) has the 'cut' skill
- PASS: CX Agent: a notice was drafted for the affected customer (medium risk, NOT sent)
- PASS: decision_log rows for the reassignment and the notice
- PASS: nothing has changed yet: the appointment is still with Aria
- PASS: manager approves the reassignment: the appointment moves to the new stylist
- PASS: the notice is still only a draft (nothing was sent)

_Cleanup:_ appointments restored: 0, actions (with approvals) deleted: 2, purchase orders deleted: 0, feedback rows deleted: 0, temporary appointments deleted: 1, events deleted: 1, events kept (they have append-only log rows): 1

### Scenario 3: Customer complaint: ticket, service log, refund (manager only)

- PASS: the database trigger created the complaint_received event and it was handled
- PASS: CX Agent: classified 'service quality' / 'negative' on the feedback row
- PASS: CX Agent: a HIGH-priority ticket was opened (rating 1)
- PASS: Quality Agent: the service log was checked and a problem was flagged (low risk, executed)
- PASS: CX Agent: a reply was drafted (medium risk, NOT sent), pending approval
- PASS: a refund of 60.00 (50% of 120, the policy limit) was suggested as HIGH risk, pending approval
- PASS: decision_log: ONE row with agents_involved ['cx_agent','quality_agent'] and the real details
- PASS: a customer account CANNOT approve the refund (manager only)
- PASS: the manager approves the refund: ticket 'resolved', action 'executed'

_Cleanup:_ appointments restored: 0, actions (with approvals) deleted: 5, purchase orders deleted: 0, feedback rows deleted: 1, temporary appointments deleted: 1, events deleted: 1, events kept (they have append-only log rows): 1

### Scenario 4: Shelf says empty, database says stock: audit flag

- PASS: the event was handled (not failed)
- PASS: Inventory Agent: an audit flag (medium risk) with observed 0 vs database count, pending approval
- PASS: no purchase order was created for a mismatch
- PASS: decision_log: a row for the audit flag with the real numbers
- PASS: the stock was NOT changed before the manager reviews
- PASS: the manager approves: the flag is resolved (action 'executed')
- PASS: the stock is STILL unchanged (no stock adjustment without a separate decision)

_Cleanup:_ appointments restored: 0, actions (with approvals) deleted: 1, purchase orders deleted: 0, feedback rows deleted: 0, temporary appointments deleted: 0, events deleted: 0, events kept (they have append-only log rows): 1

### Scenario 5: Waiting area overloaded: shifts + delay notices

- PASS: the event was handled (not failed)
- PASS: Scheduling Agent: 3 shift proposals (medium risk), each 30 minutes later, for exactly the 3 next bookings
- PASS: every shift keeps the same stylist free (no overlap) and is inside their shift
- PASS: CX Agent: delay notices (medium risk, NOT sent) for the two customers; the walk-in gets none
- PASS: decision_log: 3 shift rows, 2 notice rows and one walk-in row
- PASS: nothing has changed yet: all 3 bookings are still at their original time
- PASS: manager approves Aria's shift: the appointment moves from 10:00 to 10:30
- PASS: manager rejects Ben's shift: that appointment does NOT move

_Cleanup:_ appointments restored: 0, actions (with approvals) deleted: 5, purchase orders deleted: 0, feedback rows deleted: 0, temporary appointments deleted: 3, events deleted: 3, events kept (they have append-only log rows): 1

The decision_log rows written by these runs stay in the database (the log is append-only); their events are marked with a source such as `scenario1_test`.
