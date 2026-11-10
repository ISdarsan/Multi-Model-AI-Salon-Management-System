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

**Test leftovers:** `decision_log` can never be edited or deleted, and an event that has log rows cannot be deleted either. So the test cleans up purchase orders, actions and approvals, but leaves its events (`source = 'inventory_test'`) and their log rows (the other tests leave `worker_test` and `scheduling_test` events). To remove them by hand, paste this into the Supabase SQL Editor. It switches the protection off for a moment, so only run it for test data:

```sql
begin;
alter table public.decision_log disable trigger decision_log_no_update_delete;
delete from public.decision_log
  where event_id in (select id from public.events
                    where source in ('inventory_test', 'worker_test', 'scheduling_test'));
alter table public.decision_log enable trigger decision_log_no_update_delete;
delete from public.events where source in ('inventory_test', 'worker_test', 'scheduling_test');
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

The worker prints one line per thing it does, for example `14:05:12 event stock_low (COL-6N) -> inventory_agent -> draft PO created, waiting for approval`. Which agent handles which event type is the `ROUTES` dictionary at the top of `backend/worker.py`. Event types with no agent yet stay `new` and are reported once.

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
