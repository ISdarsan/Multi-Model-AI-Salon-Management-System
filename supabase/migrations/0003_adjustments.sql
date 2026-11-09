-- =============================================================================
-- SalonOS 0003_adjustments.sql
--  1. 'held' appointment status
--  2. busy_slots view for customers
--  3. customers lose read access to products (stock_levels was already manager-only)
--  4. stock_transactions keeps stock_levels.current_qty up to date
-- salon_policies is unchanged: still manager-only.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Held appointments
-- The exclusion constraint appointments_no_double_booking (0001) excludes only
-- status = 'cancelled' rows, so a 'held' appointment already blocks the staff slot.
-- No constraint change is needed.
-- -----------------------------------------------------------------------------
alter type public.appointment_status add value if not exists 'held' after 'booked';

comment on constraint appointments_no_double_booking on public.appointments
  is 'Exclusion constraint (btree_gist): no overlapping time ranges for the same staff member unless cancelled. Held appointments still block the slot.';

-- -----------------------------------------------------------------------------
-- 2. busy_slots: which staff slots are taken, with no customer information.
-- Runs with owner rights on purpose so customers can read it without any access
-- to the appointments of other customers.
-- -----------------------------------------------------------------------------
create view public.busy_slots as
  select staff_id, start_time, end_time
  from public.appointments
  where status <> 'cancelled';
comment on view public.busy_slots is 'Taken staff slots (booked, held, in progress, completed) for the booking page. No customer data.';
revoke all on public.busy_slots from public, anon, authenticated;
grant select on public.busy_slots to authenticated, service_role;

-- SECURITY FIX for 0001: simple views are auto-updatable and run with owner rights, and
-- Supabase's default privileges give `authenticated` INSERT/UPDATE/DELETE on new objects.
-- 0001 only revoked from public and anon, so customers could write through staff_public
-- and bypass RLS. Both views are made strictly read-only here.
revoke all on public.staff_public from public, anon, authenticated;
grant select on public.staff_public to authenticated, service_role;

-- -----------------------------------------------------------------------------
-- 3. Customers can no longer read products.
-- Managers keep access through products_manager_all. stock_levels never had a
-- customer policy, so it was already manager-only. Customers now read only:
-- services, staff_public, busy_slots (plus their own rows).
-- -----------------------------------------------------------------------------
drop policy products_authenticated_select on public.products;

-- -----------------------------------------------------------------------------
-- 4. stock_transactions -> stock_levels
-- After each inserted transaction, add qty_change to current_qty and stamp
-- last_updated. Creates the stock_levels row if the product has none yet.
-- If the change would take stock below zero, the CHECK on current_qty raises and
-- the whole transaction insert is rolled back (nothing is silently clamped).
-- Only INSERT is handled: transactions are an append-only ledger, so do not
-- update or delete them; record a correction as an 'adjustment' instead.
-- -----------------------------------------------------------------------------
create or replace function public.apply_stock_transaction()
returns trigger
language plpgsql security definer
set search_path = ''
as $$
begin
  -- UPDATE first: an INSERT ... ON CONFLICT would run the current_qty >= 0 CHECK on the
  -- proposed row (the raw qty_change) before looking at the existing row.
  update public.stock_levels
     set current_qty = current_qty + new.qty_change, last_updated = now()
   where product_id = new.product_id;

  if not found then
    insert into public.stock_levels (product_id, current_qty, last_updated)
    values (new.product_id, new.qty_change, now());
  end if;
  return new;
end;
$$;
revoke execute on function public.apply_stock_transaction() from public, anon, authenticated;

create trigger stock_transactions_apply
  after insert on public.stock_transactions
  for each row execute function public.apply_stock_transaction();

comment on table public.stock_transactions is
  'Stock movement ledger; source for sales velocity. Each insert updates stock_levels via trigger. Treat as append-only.';
comment on column public.stock_levels.current_qty is
  'Maintained by the stock_transactions_apply trigger; do not edit directly except through an adjustment transaction.';
