# SalonOS database schema

Postgres on Supabase. Files: `supabase/migrations/0001_init.sql` (schema), `0002_rls.sql` (row-level security), `0003_adjustments.sql` (held status, busy_slots, tighter customer access, stock trigger), `supabase/seed.sql` (demo data). Source of truth for scope: [ARCHITECTURE.md](ARCHITECTURE.md) sections 7 and 8.

## Backend access and RLS

**The backend must use the Supabase service role key.** The service role bypasses RLS, so agents, the CV emitter and the approval executor can read and write everything. Never ship that key to the browser. The React app uses the anon key plus the signed-in user's JWT, and RLS applies to it.

Because the service role skips RLS, `decision_log` is protected by triggers as well as by policy. `UPDATE`, `DELETE` and `TRUNCATE` raise an error for every role, including `service_role`.

| Role | Access |
| --- | --- |
| anon (not signed in) | Nothing |
| customer | Create and read own `appointments` and `feedback`; read own `tickets`; read `services`, and the read-only views `staff_public` (id, name, skills) and `busy_slots` (staff_id, start_time, end_time); read and edit own `profiles` row (cannot change `role`). No access to `products`, `stock_levels` or `salon_policies` |
| manager | Read and write everything. `decision_log` is insert and read only. Managers are the only role that can update `approvals` |
| service_role | Bypasses RLS |

`products`, `events`, `agent_actions`, `approvals`, `decision_log`, `stock_*`, `staff`, `service_products`, `service_log`, `salon_policies` and `purchase_orders` are manager-only. `salon_policies` stays manager-only on purpose.

## Tables

All primary keys are `uuid` (`gen_random_uuid()`), all timestamps are `timestamptz`.

| Table | Purpose and key columns |
| --- | --- |
| `profiles` | 1:1 with `auth.users`. `full_name`, `role` (manager/customer). Row auto-created on signup with role `customer`. |
| `products` | `sku` (unique), `name`, `category`, `supplier`, `price`, `reorder_threshold`, `lead_time_days`. |
| `stock_levels` | One row per product (`product_id` unique): `current_qty`, `last_updated`. Maintained by a trigger on `stock_transactions`. |
| `stock_transactions` | `product_id`, signed `qty_change`, `type` (sale/use/delivery/adjustment), `created_at`. Append-only ledger and source for sales velocity. Each insert updates `stock_levels`. |
| `staff` | `name`, `skills text[]`, `shift_start`, `shift_end`, `work_days smallint[]` (ISO 1-7), `is_available`. Not linked to logins. |
| `services` | `name`, `duration_minutes`, `price`. |
| `service_products` | Join table: which products a service uses, with `qty_per_service`. |
| `appointments` | `customer_id` (null = walk-in), `staff_id`, `service_id`, `start_time`, `end_time`, `status` (booked/held/in_progress/completed/cancelled). `held` is a temporary hold set by an agent. Exclusion constraint blocks overlapping bookings for one stylist unless cancelled, so a held appointment still blocks the slot. |
| `feedback` | `customer_id` (nullable), `appointment_id` (nullable), `text`, `rating` 1-5, `category`, `sentiment` (set by the CX Agent). |
| `tickets` | `feedback_id`, `status`, `priority`, `assigned_to`, `resolution_notes`. |
| `service_log` | `appointment_id`, `readiness_notes`, `quality_notes`. Quality Agent input. |
| `salon_policies` | `title`, `body`. Later indexed for complaint-policy RAG. |
| `purchase_orders` | `product_id`, `qty`, `supplier`, `status` (draft/approved/rejected/ordered), `created_by_agent`. |
| `events` | `type`, `source`, `payload jsonb`, `status` (new/processing/handled/failed). Index on `(status, created_at)`. |
| `agent_actions` | `event_id`, `agent_name`, `tool_called`, `input`, `result`, `risk_tier`, `status`. |
| `approvals` | `agent_action_id` (unique), `status` (pending/approved/rejected), `decided_by`, `decided_at`, `reason`. |
| `decision_log` | `event_id`, `agents_involved text[]`, `reasoning`, `outcome`. Append-only. |

Views (read-only; `select` is the only privilege granted to `authenticated`):
- `staff_public`: `id`, `name`, `skills` only, so customers see stylist names without shift or availability data.
- `busy_slots`: `staff_id`, `start_time`, `end_time` for every non-cancelled appointment (held ones included), with no customer information. The booking page uses it to grey out taken slots.

Both views run with owner rights on purpose. Because they are auto-updatable, 0003 revokes every privilege except `select` from `authenticated`; without that, a customer could write through them and bypass RLS.

### Triggers

| Trigger | Effect |
| --- | --- |
| `on_auth_user_created` | New auth user gets a `profiles` row with role `customer` |
| `appointments_emit_booking_created` | Insert into `appointments` adds a `booking_created` event |
| `feedback_emit_complaint_received` | Insert into `feedback` with `rating <= 2` adds a `complaint_received` event |
| `stock_transactions_apply` | Each `stock_transactions` insert adds `qty_change` to `stock_levels.current_qty` and sets `last_updated` (creates the row if missing). A change that would make stock negative raises an error and the transaction insert is rolled back. Updates and deletes of transactions are not handled; correct mistakes with an `adjustment` row |
| `profiles_guard_role` | A signed-in non-manager cannot change a role |
| `decision_log_no_update_delete`, `decision_log_no_truncate` | Make `decision_log` append-only |

Realtime is enabled on `events`, `approvals`, `agent_actions` and `appointments` (Realtime respects RLS, so customers only receive their own appointments).

## Event types and payloads

Enum `event_type`: `shelf_observation`, `stock_low`, `booking_created`, `staff_absent`, `complaint_received`, `queue_overload`.

**These payload shapes are frozen.** Two are produced by DB triggers; the other four are the contract for the CV pipeline, agents and simulator. The database does not validate `payload`, so code must follow these shapes exactly. Changing one means updating this document first.

**`booking_created`** (source `booking_app`, emitted by trigger)
```json
{ "appointment_id": "uuid", "customer_id": "uuid|null", "staff_id": "uuid",
  "service_id": "uuid", "start_time": "ISO8601", "end_time": "ISO8601" }
```

**`complaint_received`** (source `feedback_form`, emitted by trigger when rating <= 2)
```json
{ "feedback_id": "uuid", "appointment_id": "uuid|null", "customer_id": "uuid|null", "rating": 1 }
```

**`shelf_observation`** (source `cv_pipeline`)
```json
{ "image_path": "storage path", "model": "yolo11n", "inference_ms": 180,
  "observations": [ { "sku": "COL-6N", "observed_count": 2, "empty_slots": 3, "confidence": 0.91 } ] }
```

**`stock_low`** (source `inventory_agent` or `simulator`)
```json
{ "product_id": "uuid", "sku": "COL-6N", "current_qty": 2, "reorder_threshold": 10 }
```

**`staff_absent`** (source `manager_dashboard` or `simulator`)
```json
{ "staff_id": "uuid", "from": "ISO8601", "to": "ISO8601", "reason": "sick" }
```

**`queue_overload`** (source `simulator`)
```json
{ "waiting_count": 7, "capacity": 4, "avg_wait_minutes": 28 }
```

## Seed data

25 products (shampoo 4, conditioner 4, serum 3, colour 8, skincare 6), 5 staff, 8 services with `service_products`, 134 appointments over 14 days (7 back, 7 forward, Sundays closed), 63 service-log rows, 40 feedback rows (8 with rating <= 2), 5 policies and 267 stock transactions. No seeded appointment is `held`; the Scheduling Agent creates holds live.

Stock is built only from `stock_transactions`: a zero row per product, an opening delivery, 13 days of sales/use, then a stocktake `adjustment` per product to its target (2x threshold, or the low value below). This exercises the stock trigger on every seed.

Three products are below their reorder threshold: `COL-6N` (2/10), `COL-7N` (3/10), `SHA-003` (4/8). Tomorrow always has Full Colour, Root Touch-up and Highlights booked, so scenario 1 works out of the box.

The seed is deterministic: every run produces the same rows and the same ids. Only dates differ, because they are relative to the day you run it. The seed uses no `random()`. Ids come from a temporary sequence-based default (restored to `gen_random_uuid()` at the end), row order is fixed with `ORDER BY`, ties are broken on stable keys, and the demo password hash uses a fixed salt (local dev only). In the seed, customer1 owns 23 appointments and 4 feedback rows.

The event triggers are disabled while seeding, so `events` starts empty. Scenarios are triggered live.

Demo logins (local only, password `demo1234`): `manager@salonos.demo`, `customer1@salonos.demo`, `customer2@salonos.demo`.

## Testing RLS

Run in the Supabase SQL editor or `psql`. Replace the UUIDs with real `auth.users` ids if you changed the seed.

**As a customer** (`customer1`):
```sql
begin;
set local role authenticated;
select set_config('request.jwt.claim.sub', '00000000-0000-4000-8000-0000000000c1', true);

select count(*) from appointments;        -- only customer1's own rows (23 in the seed)
select count(*) from events;              -- 0 (manager only)
select name, skills from staff_public;    -- works
select count(*) from staff;               -- 0 rows
select count(*) from products;            -- 0 rows (manager only)
select count(*) from busy_slots;          -- all non-cancelled slots, no customer columns
update staff_public set name = 'x';       -- ERROR: permission denied (read-only view)
update profiles set role = 'manager'
  where id = '00000000-0000-4000-8000-0000000000c1';   -- ERROR: only a manager can change a role
rollback;
```

**As a manager:**
```sql
begin;
set local role authenticated;
select set_config('request.jwt.claim.sub', '00000000-0000-4000-8000-0000000000a1', true);

select count(*) from appointments;        -- all rows (134)
select count(*) from events;              -- readable
update approvals set status = 'approved', decided_by = auth.uid(), decided_at = now()
  where status = 'pending';               -- allowed
update decision_log set outcome = 'x';    -- ERROR: permission denied (append-only)
rollback;
```

## How this was tested

No Docker or Supabase CLI was available on the build machine. All four SQL files (three migrations plus the seed) were executed in order in PGlite (Postgres in WASM, with `btree_gist` and `pgcrypto`) against a stub of the Supabase `auth` schema, `auth.uid()` and the three roles.

Checks that passed:
- **Determinism:** two fresh databases built from the same files give identical rows and ids in every seeded table (compared by hash, with timestamps reduced to the date).
- **Seed:** loads cleanly, row counts match, and exactly 3 products sit below threshold (`COL-6N` 2/10, `COL-7N` 3/10, `SHA-003` 4/8; minimum stock is 2).
- **Customer isolation:** a customer sees only their own appointments, feedback and tickets, and gets zero rows from `products`, `stock_levels`, `salon_policies`, `staff` and the manager-only tables.
- **Customer reads:** `services`, `staff_public` and `busy_slots` work. `busy_slots` has only `staff_id`, `start_time`, `end_time`, shows held slots and hides cancelled ones.
- **Customer writes:** booking as another user, inserting `held` directly, inserting events, setting `sentiment`, and self-promoting to manager are all rejected. Insert, update and delete through `staff_public` or `busy_slots` are rejected.
- **Double-booking:** a held appointment blocks an overlapping booking, and cancelling it frees the slot.
- **Stock trigger:** a sale lowers `current_qty`; an oversell is rejected and leaves stock unchanged; a product with no stock row gets one on its first transaction.
- **Events and log:** both event triggers fire; `decision_log` blocks update, delete and truncate, including for the owner role; anon sees nothing.

**Not tested:** `supabase db reset` itself; the real `auth.users` and `auth.identities` columns used by the seed's demo logins; the `supabase_realtime` publication (skipped if absent); real JWT parsing in `auth.uid()`; the service-role path. Run `supabase db reset` once locally and sign in as each demo user to close these.
