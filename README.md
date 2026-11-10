# SalonOS: Multi-Agent Salon Operations Command Centre

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design and [docs/SCHEMA.md](docs/SCHEMA.md) for the database.

## Run the hello agent

`backend/agents/hello_agent.py` is a tiny test agent. It asks a local Ollama model to call one tool, `get_low_stock()`, then writes one row to the `agent_actions` table.

**Before you start:** the database tables and seed data must already be in your Supabase project, and `.env` must hold `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` and `OLLAMA_MODEL` (copy `.env.example` to `.env`). [Ollama](https://ollama.com) must be installed.

Run these from the project root:

```powershell
# 1. Download the model named in OLLAMA_MODEL in .env (one time, about 5 GB)
ollama pull llama3.1:8b

# 2. Create a private Python environment (one time)
python -m venv .venv

# 3. Switch it on (every new terminal)
.venv\Scripts\Activate.ps1

# 4. Install the packages (one time)
pip install -r backend/requirements.txt

# 5. Run the agent
python backend/agents/hello_agent.py
```

On success it prints the low-stock products (with the seed data: `COL-6N`, `COL-7N`, `SHA-003`) and the `agent_actions` row it wrote. Each run adds one `events` row and one `agent_actions` row; you can see them in the Supabase Table Editor.

## Run the Inventory Agent

`backend/agents/inventory_agent.py` looks for `new` events of type `stock_low` or `shelf_observation`. For each low product it decides with plain Python rules (no AI) whether to reorder. If so, it creates a draft purchase order plus an `agent_actions` row (medium risk, `awaiting_approval`), a `pending` approval and a `decision_log` row. Ollama is only used to write a short explanation; if it fails, a template sentence is used.

**Which products are checked**
- `stock_low` event: the one product named in the payload.
- `shelf_observation` event: every product whose observed count is below its reorder threshold.

**Reorder rules** (sales speed uses `sale` and `use` transactions from the last 7 days)

| Situation | Reorder when | Quantity |
| --- | --- | --- |
| Product sold recently | days of stock left (stock / average daily sales) is less than lead time + 2 | enough for lead time + 14 days of sales, minus current stock, rounded up, at least 1 |
| No sales in 7 days, stock below the reorder threshold | always (the log says "Low-stock reorder with no recent sales") | enough to reach 2 x the reorder threshold, at least 1 |
| No sales in 7 days, stock at or above the threshold | never | none |

**Duplicate protection:** if the product already has a purchase order that is `draft`, `approved` or `ordered`, no new draft is created and the skip is written to `decision_log`. Only a `rejected` order lets the agent create a new one.

**Every event leaves a record:** a `decision_log` row for each product (order created, skipped, or no reorder needed). If anything fails, the event is marked `failed` and the reason goes into `decision_log`.

Run these from the project root, with the Python environment switched on (see above):

```powershell
# Run the agent on every new stock_low / shelf_observation event
python -m backend.agents.inventory_agent

# Run the automatic test (creates test events, approves one order, rejects another)
python -m backend.tests.test_inventory_agent
```

Managers approve or reject with `backend/approvals/approval_actions.py` (`approve(approval_id, decided_by)` and `reject(approval_id, decided_by, reason)`).

**Test leftovers:** `decision_log` can never be edited or deleted, and an event that has log rows cannot be deleted either. So the test cleans up purchase orders, actions and approvals, but leaves its events (`source = 'inventory_test'`) and their log rows (the other tests leave `worker_test`, `scheduling_test`, `orchestrator_test` and `dashboard_test` events). To remove them by hand, paste this into the Supabase SQL Editor. It switches the protection off for a moment, so only run it for test data:

```sql
begin;
alter table public.decision_log disable trigger decision_log_no_update_delete;
delete from public.decision_log
  where event_id in (select id from public.events
                    where source in ('inventory_test', 'worker_test', 'scheduling_test', 'orchestrator_test', 'dashboard_test', 'complaint_test', 'audit_queue_test', 'scenario1_test', 'scenario2_test', 'scenario3_test', 'scenario4_test', 'scenario5_test'));
alter table public.decision_log enable trigger decision_log_no_update_delete;
delete from public.events where source in ('inventory_test', 'worker_test', 'scheduling_test', 'orchestrator_test', 'dashboard_test', 'complaint_test', 'audit_queue_test', 'scenario1_test', 'scenario2_test', 'scenario3_test', 'scenario4_test', 'scenario5_test');
commit;
```

## Run the server and the worker

Events flow like this: something sends `POST /events` to the **server**, the event is saved with status `new`, and the **worker** (`backend/worker.py`) notices it within 3 seconds and gives it to the right agent. Open **two terminals**, switch the Python environment on in each (`.venv\Scripts\Activate.ps1`), and run from the project root:

```powershell
# Terminal 1: the API server (needs API_KEY in .env)
uvicorn backend.api.main:app --reload

# Terminal 2: the worker (leave it running; press Ctrl+C to stop it cleanly)
python -m backend.worker
```

The worker prints one line per thing it does, for example `14:05:12 event stock_low (COL-6N) -> orchestrator -> inventory_agent -> draft PO created, waiting for approval`. The worker only claims events and hands them to the Orchestrator. Which agent handles which event type is the `ROUTES` dictionary at the top of `backend/agents/orchestrator.py`. Event types with no agent yet stay `new` and are reported once.

Check the worker's rules without touching the database: `python -m backend.tests.test_worker`.

## Scheduling Agent

`backend/agents/scheduling_agent.py` handles `staff_absent` events (payload: `staff_id`, `from`, `to`, `reason`, see `docs/SCHEMA.md`). All decisions are plain Python rules; Ollama only writes a short explanation (a template sentence is used if it fails). **The agent never changes an appointment itself.** It only proposes; a manager approves.

For each of the absent stylist's `booked` or `held` appointments that start in the future and inside the absence window, a replacement must:
- not be the absent stylist, and have `is_available = true`;
- work that weekday, with the whole slot inside their shift (times are read in UTC);
- have the skill the service needs (see `SERVICE_SKILLS` in `backend/tools/scheduling_tools.py`, edit it when you add a service);
- have no overlapping `booked`, `held` or `in_progress` appointment.

If several fit, the stylist with the fewest appointments that day wins (earlier proposals in the same event count). For each match the agent writes an `agent_actions` row (`reassign_staff`, medium risk, `awaiting_approval`), a `pending` approval and a `decision_log` row (outcome `reassignment proposed, waiting for approval`). If nobody fits, no proposal is made and `decision_log` says `no replacement found, manager must decide` with the reason for each stylist.

`approve()` and `reject()` in `backend/approvals/approval_actions.py` work for both agents. Approving a reassignment moves the appointment to the new stylist; rejecting it changes nothing. If the appointment changed since the proposal, or the new stylist became busy, the approval is refused and stays pending.

The worker sends `staff_absent` events to this agent. Tests:

```powershell
python -m backend.tests.test_scheduling_rules      # offline, no database writes
python -m backend.tests.test_approval_actions      # offline, no database writes
python -m backend.tests.test_scheduling_agent      # live: uses your database, restores appointments afterwards
```

## Orchestrator

`backend/agents/orchestrator.py` sits between the worker and the agents. The worker claims each event and gives it to the Orchestrator, which does two things.

**1. Routing.** It looks up the event type in `ROUTES` (`stock_low` and `shelf_observation` go to the Inventory Agent, `staff_absent` to the Scheduling Agent) and runs that agent. To add an agent, register its `handle_event` function in `AGENTS` and its event types in `ROUTES`.

**2. Clash check for stock events.** After the Inventory Agent has handled a `stock_low` or `shelf_observation` event (even if it skipped because an order already exists), the Orchestrator checks each product that was looked at against upcoming bookings. Plain Python rules, no AI:

1. Take the product's `current_qty`. Incoming purchase orders are NOT counted, because they have not arrived.
2. Find `booked` appointments that start within `LOOKAHEAD_DAYS` (3) whose service uses the product (`service_products.qty_per_service`), in start-time order.
3. Walk through them with a running stock number. If the stock covers an appointment, subtract what it needs. If not, that appointment is in clash (and nothing is subtracted for it).
4. For each clash it proposes a hold: an `agent_actions` row (`orchestrator`, `hold_appointment`, medium risk, `awaiting_approval`), a `pending` approval and one `decision_log` row with the real numbers (outcome `clash found, hold proposed, waiting for approval`). **The appointment is not changed.** If that appointment already has a pending hold proposal, no second one is made (the log says `clash found, hold already proposed`).

`approve()` on a hold changes the appointment to `held` (refused if it is no longer `booked`). `reject()` changes nothing and logs the reason. Ollama only writes a short explanation after the real numbers; a template sentence is used if it fails.

If an agent crashes, the Orchestrator marks the event `failed` and writes the reason to `decision_log`. Tests:

```powershell
python -m backend.tests.test_orchestrator          # offline, no database writes
python -m backend.tests.test_orchestrator_agent    # live: uses your database, restores everything afterwards
```

## Manager dashboard

A web page where the manager signs in, sees what the agents are doing, and approves or rejects their proposals. Built with React, Vite, Tailwind and Supabase.

**Screens:** *Command Centre* (4 numbers: pending approvals, products below reorder level, unhandled events, appointments today, plus a live feed of agent activity), *Approval Inbox* (pending proposals grouped by the event that caused them, each with plain-words description, risk, reasoning and Approve / Reject; rejecting needs a reason), *Decision Log* (every decision, newest first, click a row for the reasoning, filter by outcome). Times are shown in UTC. Updates arrive live (Supabase Realtime) with a 5-second refresh as a fallback.

**How it is secured:** the browser only has the *publishable* key. It signs in with email and password and reads data through Supabase, where row-level security lets only managers see approvals, actions and the log. Approve and reject go through the server (`POST /approvals/{id}/approve` and `/reject`), which checks the sign-in token with Supabase, requires `role = manager` (401 if not signed in, 403 if not a manager), and then runs the same `approve()` / `reject()` as the tests. `POST /events` still uses the `X-API-Key` header.

**Setup (once):**

```powershell
cd frontend
copy .env.example .env     # then fill in: VITE_SUPABASE_URL, VITE_SUPABASE_ANON_KEY (the publishable key), VITE_API_URL
npm install
```

**Run it** (three terminals, each from the project root, Python environment on for the first two):

```powershell
# Terminal 1: the server (adds the approve/reject endpoints and allows the dashboard's address)
uvicorn backend.api.main:app --reload

# Terminal 2: the worker (turns new events into proposals)
python -m backend.worker

# Terminal 3: the dashboard
cd frontend
npm run dev
```

Open http://localhost:5173 and sign in with `manager@salonos.demo` / `demo1234` (demo login from the seed). A customer login such as `customer1@salonos.demo` sees "This area is for managers only". The dashboard must run on port 5173, because the server only accepts requests from `http://localhost:5173`.

Check the approve and reject endpoints (uses your real database, cleans up after itself): `python -m backend.tests.test_approval_api`. Build check: `cd frontend; npm run build`.

## CX Agent and Quality Agent (scenario 3: a customer complaint)

When a customer leaves feedback with a rating of 2 or lower, the database creates a `complaint_received` event. The Orchestrator runs the **Quality Agent**, then the **CX Agent**, which share what they find.

- **Quality Agent** (`backend/agents/quality_agent.py`): reads the appointment's service-log notes and flags a problem with keyword rules (for example "over target", "uneven", "delay"). A flag is saved as a low-risk `flag_quality_issue` action.
- **CX Agent** (`backend/agents/cx_agent.py`): classifies the complaint with keyword rules (service quality, wait time, price, product, staff behaviour), saves the category and sentiment on the feedback row, opens a ticket (rating 1 = high, 2 = medium priority), finds the best matching `salon_policies` by keywords, and drafts a reply (**medium** risk, `draft_reply`).
- **Refund or compensation:** only if the complaint looks justified (the Quality Agent flagged a problem, or the rating is 1). It suggests 50% of the service price under the Refund Policy (10% for a wait-time complaint under the Delay Compensation policy) as a **high**-risk `suggest_refund`. It never goes above the policy limit.
- **One decision_log row** per complaint (`agents_involved` = `cx_agent`, `quality_agent`) with the real details: rating, category, service, stylist, what the service log says, matching policies, and the refund decision.
- **Approving** a reply or a refund marks the ticket `resolved` (the refund amount is written in the ticket notes). **Nothing is ever sent to a customer** and no money moves: a draft is only text kept in the proposal. Rejecting changes nothing and logs the reason.

The wording of the draft reply is the only thing the AI writes (Ollama; a template sentence is used if it fails). Tests:

```powershell
python -m backend.tests.test_cx_quality      # offline, no database writes
python -m backend.tests.test_complaint_flow  # live: uses your database. Stop the worker first.
```

## Shelf audit and waiting-area scenarios (scenarios 4 and 5, plus customer notices)

**Scenario 4: shelf audit** (Inventory Agent). On every `shelf_observation` event the agent compares each product's observed count with `stock_levels.current_qty`. If they differ by **more than 1**, it proposes a **medium**-risk `raise_audit_flag` (a manager should count by hand). **The stock is never changed**; approving the flag only marks it resolved. (The reorder rules still run as before, and use the database stock.)

**Scenario 5: waiting area overloaded** (`queue_overload` event: `waiting_count`, optional `capacity`, `avg_wait_minutes`, `time`; see `docs/SCHEMA.md`).
- The **Scheduling Agent** proposes moving the next `MAX_SHIFTS` (3) booked appointments (counted from the event's `time`, or its creation time) later: **30 minutes** if 3 or more people are over capacity (default capacity 4) or the average wait is already 30 minutes or more, otherwise **15**. A shift is only proposed if the same stylist has no overlapping appointment (booked, held or in progress) and the appointment still ends within the shift. If 30 minutes does not fit it tries 15; if nothing fits, the log says `no safe shift found, manager must decide`. Medium risk, tool `shift_appointment`. Each proposal is checked against the appointments as they are now, so they can be approved in any order.
- The **CX Agent** drafts a notice (medium risk, `draft_customer_notice`) for each affected customer. Walk-ins have no customer on file, so they get none. The same notice is drafted when a stylist is absent and an appointment is proposed to move to another stylist (scenario 2).

**Approving:** a shift moves the appointment (refused if it changed since, or the stylist became busy); an audit flag or a notice is just marked approved. **Nothing is ever sent to a customer.** Rejecting changes nothing and logs the reason.

```powershell
python -m backend.tests.test_audit_queue        # offline, no database writes
python -m backend.tests.test_audit_queue_flow   # live: uses your database. Stop the worker first.
```

## Scenario test runner (the acceptance test)

`backend/scenarios/run_scenarios.py` runs all five cross-agent scenarios from `docs/ARCHITECTURE.md` (section 9) end to end on your real database, using temporary test data:

1. Colour stock low + a colour service booked: clash detected, booking held, draft order, manager approves.
2. A stylist is absent: reassignment proposed + a notice drafted for the affected customer, manager approves the reassignment.
3. A customer complaint: ticket opened, service log checked, refund suggested, manager only.
4. The shelf shows an empty slot but the database says stock exists: audit flag, no stock change.
5. The waiting area is overloaded: appointments shifted + delay notices.

For each scenario it sends the event, runs the Orchestrator (the same code the worker runs), checks the exact proposals and rows (it **fails loudly** if an expected proposal is missing), approves or rejects as the scenario says, checks the final state, measures the seconds from the event to the proposals, and cleans up everything except the append-only `decision_log` rows. It prints a PASS/FAIL table and saves it to `docs/SCENARIO_RESULTS.md`.

```powershell
# Stop the worker first (so it does not grab the test events), then from the project root:
python -m backend.scenarios.run_scenarios
```

It exits with an error code if any scenario fails. Latency includes the local AI writing the explanations and drafts, so it depends on your computer and on Ollama already having the model loaded (the first run after a restart is slower). The 10-second target is reported next to the result but does not decide PASS or FAIL.
