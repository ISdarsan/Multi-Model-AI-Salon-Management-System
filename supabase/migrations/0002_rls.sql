-- =============================================================================
-- SalonOS 0002_rls.sql : row-level security on every table
--
-- Roles:  anon (not signed in)   -> no access to anything
--         authenticated customer -> own bookings/feedback/tickets, read products + services
--         authenticated manager  -> read/write everything (decision_log: insert + read only)
--         service_role           -> backend; bypasses RLS entirely (so the append-only
--                                   trigger on decision_log, not RLS, protects that table)
-- =============================================================================

alter table public.profiles           enable row level security;
alter table public.products           enable row level security;
alter table public.stock_levels       enable row level security;
alter table public.stock_transactions enable row level security;
alter table public.staff              enable row level security;
alter table public.services           enable row level security;
alter table public.service_products   enable row level security;
alter table public.appointments       enable row level security;
alter table public.feedback           enable row level security;
alter table public.tickets            enable row level security;
alter table public.service_log        enable row level security;
alter table public.salon_policies     enable row level security;
alter table public.purchase_orders    enable row level security;
alter table public.events             enable row level security;
alter table public.agent_actions      enable row level security;
alter table public.approvals          enable row level security;
alter table public.decision_log       enable row level security;

-- -----------------------------------------------------------------------------
-- Manager: full access on all tables except decision_log (append-only)
-- -----------------------------------------------------------------------------
do $$
declare t text;
begin
  foreach t in array array[
    'profiles', 'products', 'stock_levels', 'stock_transactions', 'staff', 'services',
    'service_products', 'appointments', 'feedback', 'tickets', 'service_log',
    'salon_policies', 'purchase_orders', 'events', 'agent_actions', 'approvals'
  ] loop
    execute format(
      'create policy %I on public.%I for all to authenticated
         using (public.is_manager()) with check (public.is_manager())',
      t || '_manager_all', t);
  end loop;
end $$;

-- decision_log: managers read and append only (no UPDATE/DELETE policy exists; trigger also blocks them)
create policy decision_log_manager_select on public.decision_log
  for select to authenticated using (public.is_manager());
create policy decision_log_manager_insert on public.decision_log
  for insert to authenticated with check (public.is_manager());

-- -----------------------------------------------------------------------------
-- profiles: a user reads and edits their own row (role changes blocked by trigger)
-- -----------------------------------------------------------------------------
create policy profiles_self_select on public.profiles
  for select to authenticated using (id = (select auth.uid()));
create policy profiles_self_update on public.profiles
  for update to authenticated
  using (id = (select auth.uid())) with check (id = (select auth.uid()));

-- -----------------------------------------------------------------------------
-- Catalogue: any signed-in user can read products and services
-- (products includes supplier and reorder_threshold; see docs/SCHEMA.md note)
-- -----------------------------------------------------------------------------
create policy products_authenticated_select on public.products
  for select to authenticated using (true);
create policy services_authenticated_select on public.services
  for select to authenticated using (true);

-- -----------------------------------------------------------------------------
-- Customers: appointments
-- -----------------------------------------------------------------------------
create policy appointments_customer_select on public.appointments
  for select to authenticated using (customer_id = (select auth.uid()));
create policy appointments_customer_insert on public.appointments
  for insert to authenticated
  with check (customer_id = (select auth.uid()) and status = 'booked');

-- -----------------------------------------------------------------------------
-- Customers: feedback (sentiment is set by the CX Agent, never by the customer)
-- -----------------------------------------------------------------------------
create policy feedback_customer_select on public.feedback
  for select to authenticated using (customer_id = (select auth.uid()));
create policy feedback_customer_insert on public.feedback
  for insert to authenticated
  with check (
    customer_id = (select auth.uid())
    and sentiment is null
    and (
      appointment_id is null
      or exists (
        select 1 from public.appointments a
        where a.id = appointment_id and a.customer_id = (select auth.uid())
      )
    )
  );

-- -----------------------------------------------------------------------------
-- Customers: tickets raised from their own feedback (read only)
-- -----------------------------------------------------------------------------
create policy tickets_customer_select on public.tickets
  for select to authenticated
  using (exists (
    select 1 from public.feedback f
    where f.id = feedback_id and f.customer_id = (select auth.uid())
  ));

-- -----------------------------------------------------------------------------
-- Everything else (stock_levels, stock_transactions, staff, service_products,
-- service_log, salon_policies, purchase_orders, events, agent_actions, approvals,
-- decision_log) has only a manager policy, so customers and anon are denied.
-- Customers read staff names through the staff_public view instead.
-- -----------------------------------------------------------------------------
