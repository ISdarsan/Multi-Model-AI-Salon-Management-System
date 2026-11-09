-- =============================================================================
-- SalonOS 0001_init.sql : schema, enums, indexes, triggers, realtime
-- Source of truth: docs/ARCHITECTURE.md sections 7 and 8.
-- RLS policies live in 0002_rls.sql.
-- =============================================================================

create extension if not exists btree_gist with schema extensions;  -- needed for the double-booking exclusion constraint

-- -----------------------------------------------------------------------------
-- Enums (fixed value sets)
-- -----------------------------------------------------------------------------
create type public.user_role          as enum ('manager', 'customer');
create type public.stock_txn_type     as enum ('sale', 'use', 'delivery', 'adjustment');
create type public.appointment_status as enum ('booked', 'in_progress', 'completed', 'cancelled');
create type public.sentiment          as enum ('positive', 'neutral', 'negative');
create type public.ticket_status      as enum ('open', 'in_progress', 'resolved', 'closed');   -- values chosen by Claude, not in spec
create type public.ticket_priority    as enum ('low', 'medium', 'high');                        -- values chosen by Claude, not in spec
create type public.po_status          as enum ('draft', 'approved', 'rejected', 'ordered');
create type public.event_type         as enum ('shelf_observation', 'stock_low', 'booking_created',
                                               'staff_absent', 'complaint_received', 'queue_overload');
create type public.event_status       as enum ('new', 'processing', 'handled', 'failed');
create type public.risk_tier          as enum ('low', 'medium', 'high');
create type public.action_status      as enum ('proposed', 'awaiting_approval', 'executed', 'rejected', 'failed'); -- values chosen by Claude, not in spec
create type public.approval_status    as enum ('pending', 'approved', 'rejected');

-- -----------------------------------------------------------------------------
-- profiles : one row per auth user, carries the role
-- -----------------------------------------------------------------------------
create table public.profiles (
  id         uuid primary key references auth.users (id) on delete cascade,
  full_name  text,
  role       public.user_role not null default 'customer',
  created_at timestamptz not null default now()
);
comment on table  public.profiles      is 'App-level user record, 1:1 with auth.users. Holds the role used by RLS.';
comment on column public.profiles.role is 'manager = full access and the only approver; customer = own bookings/feedback only. Non-managers cannot change it (guard trigger).';

-- -----------------------------------------------------------------------------
-- Catalogue and stock
-- -----------------------------------------------------------------------------
create table public.products (
  id                uuid primary key default gen_random_uuid(),
  sku               text not null unique,
  name              text not null,
  category          text not null,
  supplier          text not null,
  price             numeric(10,2) not null check (price >= 0),
  reorder_threshold integer not null check (reorder_threshold >= 0),
  lead_time_days    integer not null check (lead_time_days >= 0)
);
comment on table  public.products                   is 'Salon product catalogue (retail and back-bar).';
comment on column public.products.sku               is 'Stock keeping unit; also the class label the CV model predicts.';
comment on column public.products.reorder_threshold is 'Stock at or below this quantity should trigger a reorder (rule-based, not LLM).';
comment on column public.products.lead_time_days    is 'Days the supplier takes to deliver; used by the Inventory Agent.';

create table public.stock_levels (
  id           uuid primary key default gen_random_uuid(),
  product_id   uuid not null unique references public.products (id) on delete cascade,
  current_qty  integer not null default 0 check (current_qty >= 0),
  last_updated timestamptz not null default now()
);
comment on table  public.stock_levels             is 'Current database stock count, one row per product. Compared against CV shelf counts.';
comment on column public.stock_levels.product_id  is 'Unique: exactly one stock row per product.';

create table public.stock_transactions (
  id         uuid primary key default gen_random_uuid(),
  product_id uuid not null references public.products (id) on delete cascade,
  qty_change integer not null,
  type       public.stock_txn_type not null,
  created_at timestamptz not null default now()
);
comment on table  public.stock_transactions            is 'Stock movement history; source for sales velocity.';
comment on column public.stock_transactions.qty_change is 'Signed: negative for sale/use, positive for delivery, either sign for adjustment.';

-- -----------------------------------------------------------------------------
-- Staff, services, appointments
-- -----------------------------------------------------------------------------
create table public.staff (
  id           uuid primary key default gen_random_uuid(),
  name         text not null,
  skills       text[] not null default '{}',
  shift_start  time not null default '09:00',
  shift_end    time not null default '18:00',
  work_days    smallint[] not null default '{1,2,3,4,5,6}',
  is_available boolean not null default true
);
comment on table  public.staff              is 'Stylists. Not linked to logins. Customers see only staff_public (id, name, skills).';
comment on column public.staff.skills       is 'e.g. {cut,colour,styling,skincare}; used by the Scheduling Agent when reassigning.';
comment on column public.staff.work_days    is 'ISO weekday numbers, 1 = Monday ... 7 = Sunday.';
comment on column public.staff.is_available is 'false when marked absent (staff_absent scenario).';

create table public.services (
  id               uuid primary key default gen_random_uuid(),
  name             text not null,
  duration_minutes integer not null check (duration_minutes > 0),
  price            numeric(10,2) not null check (price >= 0)
);
comment on table public.services is 'Bookable salon services.';

create table public.service_products (
  service_id      uuid not null references public.services (id) on delete cascade,
  product_id      uuid not null references public.products (id) on delete restrict,
  qty_per_service integer not null default 1 check (qty_per_service > 0),
  primary key (service_id, product_id)
);
comment on table  public.service_products                 is 'Join table: which products a service consumes (services.products_needed in the spec).';
comment on column public.service_products.qty_per_service is 'Units of the product used per service performed.';

create table public.appointments (
  id          uuid primary key default gen_random_uuid(),
  customer_id uuid references public.profiles (id) on delete set null,
  staff_id    uuid not null references public.staff (id) on delete restrict,
  service_id  uuid not null references public.services (id) on delete restrict,
  start_time  timestamptz not null,
  end_time    timestamptz not null,
  status      public.appointment_status not null default 'booked',
  created_at  timestamptz not null default now(),
  constraint appointments_time_order check (end_time > start_time),
  -- A stylist cannot hold two overlapping non-cancelled appointments.
  constraint appointments_no_double_booking
    exclude using gist (staff_id with =, tstzrange(start_time, end_time) with &&)
    where (status <> 'cancelled')
);
comment on table  public.appointments             is 'Bookings. Inserting a row emits a booking_created event.';
comment on column public.appointments.customer_id is 'NULL for walk-ins.';
comment on constraint appointments_no_double_booking on public.appointments
  is 'Exclusion constraint (btree_gist): no overlapping time ranges for the same staff member unless cancelled.';

-- -----------------------------------------------------------------------------
-- Feedback, tickets, service log, policies
-- -----------------------------------------------------------------------------
create table public.feedback (
  id             uuid primary key default gen_random_uuid(),
  customer_id    uuid references public.profiles (id) on delete set null,
  appointment_id uuid references public.appointments (id) on delete set null,
  text           text not null,
  rating         smallint not null check (rating between 1 and 5),
  category       text,
  sentiment      public.sentiment,
  created_at     timestamptz not null default now()
);
comment on table  public.feedback           is 'Customer feedback. Rating <= 2 emits a complaint_received event.';
comment on column public.feedback.customer_id is 'NULL for anonymous feedback.';
comment on column public.feedback.category  is 'Free text (e.g. wait time, colour result); set by the CX Agent classifier.';
comment on column public.feedback.sentiment is 'Set by the CX Agent, not by the customer.';

create table public.tickets (
  id               uuid primary key default gen_random_uuid(),
  feedback_id      uuid not null references public.feedback (id) on delete cascade,
  status           public.ticket_status not null default 'open',
  priority         public.ticket_priority not null default 'medium',
  assigned_to      uuid references public.profiles (id) on delete set null,
  resolution_notes text,
  created_at       timestamptz not null default now()
);
comment on table  public.tickets             is 'Escalation tickets opened for complaints.';
comment on column public.tickets.assigned_to is 'Manager responsible for the ticket.';

create table public.service_log (
  id              uuid primary key default gen_random_uuid(),
  appointment_id  uuid not null references public.appointments (id) on delete cascade,
  readiness_notes text,
  quality_notes   text,
  created_at      timestamptz not null default now()
);
comment on table  public.service_log                 is 'Per-appointment workstation readiness and quality notes; read by the Quality Agent.';
comment on column public.service_log.readiness_notes is 'State of the workstation/tools before the service.';
comment on column public.service_log.quality_notes   is 'Observations on the finished service.';

create table public.salon_policies (
  id         uuid primary key default gen_random_uuid(),
  title      text not null,
  body       text not null,
  created_at timestamptz not null default now()
);
comment on table public.salon_policies is 'Short policy documents (refunds, delays, escalation). Later indexed for complaint-policy RAG.';

-- -----------------------------------------------------------------------------
-- Purchase orders
-- -----------------------------------------------------------------------------
create table public.purchase_orders (
  id               uuid primary key default gen_random_uuid(),
  product_id       uuid not null references public.products (id) on delete restrict,
  qty              integer not null check (qty > 0),
  supplier         text not null,
  status           public.po_status not null default 'draft',
  created_by_agent text,
  created_at       timestamptz not null default now()
);
comment on table  public.purchase_orders                  is 'Purchase orders. Agents create drafts only; a manager approves.';
comment on column public.purchase_orders.supplier         is 'Copied from the product at creation so history survives supplier changes.';
comment on column public.purchase_orders.created_by_agent is 'Agent name, e.g. inventory; NULL if created by a person.';

-- -----------------------------------------------------------------------------
-- Event bus and agent trace
-- -----------------------------------------------------------------------------
create table public.events (
  id         uuid primary key default gen_random_uuid(),
  type       public.event_type not null,
  source     text not null,
  payload    jsonb not null default '{}'::jsonb,
  status     public.event_status not null default 'new',
  created_at timestamptz not null default now()
);
comment on table  public.events         is 'The single event stream. Everything flows through here; agents react to rows.';
comment on column public.events.source  is 'Origin of the event, e.g. cv_pipeline, booking_app, feedback_form, simulator.';
comment on column public.events.payload is 'JSON shape depends on type; see docs/SCHEMA.md.';

create table public.agent_actions (
  id          uuid primary key default gen_random_uuid(),
  event_id    uuid not null references public.events (id) on delete restrict,
  agent_name  text not null,
  tool_called text not null,
  input       jsonb not null default '{}'::jsonb,
  result      jsonb,
  risk_tier   public.risk_tier not null,
  status      public.action_status not null default 'proposed',
  created_at  timestamptz not null default now()
);
comment on table  public.agent_actions            is 'What each agent proposed or did: tool, input, result, risk tier.';
comment on column public.agent_actions.result     is 'NULL until the tool has run (Medium/High wait for approval).';
comment on column public.agent_actions.risk_tier  is 'low runs automatically; medium waits for approval; high is manager only.';

create table public.approvals (
  id              uuid primary key default gen_random_uuid(),
  agent_action_id uuid not null unique references public.agent_actions (id) on delete restrict,
  status          public.approval_status not null default 'pending',
  decided_by      uuid references public.profiles (id) on delete set null,
  decided_at      timestamptz,
  reason          text,
  constraint approvals_decided_has_time check (status = 'pending' or decided_at is not null)
);
comment on table  public.approvals            is 'Approval inbox. One approval per agent action that is not low risk.';
comment on column public.approvals.decided_by is 'Manager who decided; NULL while pending.';
comment on column public.approvals.reason     is 'Manager note; required in practice when rejecting so the agent can be informed.';

create table public.decision_log (
  id              uuid primary key default gen_random_uuid(),
  event_id        uuid not null references public.events (id) on delete restrict,
  agents_involved text[] not null default '{}',
  reasoning       text not null,
  outcome         text not null,
  created_at      timestamptz not null default now()
);
comment on table public.decision_log is 'Append-only audit trail. UPDATE, DELETE and TRUNCATE are blocked by trigger for every role, including service_role.';

-- -----------------------------------------------------------------------------
-- Indexes (foreign keys and the event queue)
-- -----------------------------------------------------------------------------
create index stock_transactions_product_created_idx on public.stock_transactions (product_id, created_at desc);
create index appointments_customer_idx   on public.appointments (customer_id);
create index appointments_staff_idx      on public.appointments (staff_id);
create index appointments_service_idx    on public.appointments (service_id);
create index appointments_start_idx      on public.appointments (start_time);
create index service_products_product_idx on public.service_products (product_id);
create index feedback_customer_idx       on public.feedback (customer_id);
create index feedback_appointment_idx    on public.feedback (appointment_id);
create index tickets_feedback_idx        on public.tickets (feedback_id);
create index tickets_assigned_to_idx     on public.tickets (assigned_to);
create index service_log_appointment_idx on public.service_log (appointment_id);
create index purchase_orders_product_idx on public.purchase_orders (product_id);
create index events_status_created_idx   on public.events (status, created_at);
create index agent_actions_event_idx     on public.agent_actions (event_id);
create index approvals_decided_by_idx    on public.approvals (decided_by);
create index decision_log_event_idx      on public.decision_log (event_id);
-- stock_levels.product_id, products.sku and approvals.agent_action_id are covered by their unique constraints.

-- -----------------------------------------------------------------------------
-- Helper: is the current user a manager? (SECURITY DEFINER so RLS on profiles does not recurse)
-- -----------------------------------------------------------------------------
create or replace function public.is_manager()
returns boolean
language sql stable security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.profiles
    where id = (select auth.uid()) and role = 'manager'
  );
$$;
comment on function public.is_manager() is 'True when the calling auth user has role = manager. Used by RLS policies.';
revoke execute on function public.is_manager() from public, anon;
grant  execute on function public.is_manager() to authenticated, service_role;

-- -----------------------------------------------------------------------------
-- Trigger: new auth user -> profiles row (role = customer)
-- -----------------------------------------------------------------------------
create or replace function public.handle_new_user()
returns trigger
language plpgsql security definer
set search_path = ''
as $$
begin
  insert into public.profiles (id, full_name, role)
  values (new.id, new.raw_user_meta_data ->> 'full_name', 'customer');
  return new;
end;
$$;
revoke execute on function public.handle_new_user() from public, anon, authenticated;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- -----------------------------------------------------------------------------
-- Trigger: only managers (or the service role) may change a profile's role
-- -----------------------------------------------------------------------------
create or replace function public.guard_profile_role()
returns trigger
language plpgsql security definer
set search_path = ''
as $$
begin
  -- auth.uid() is NULL for the service role / direct DB access, which is allowed.
  if new.role is distinct from old.role
     and (select auth.uid()) is not null
     and not public.is_manager() then
    raise exception 'only a manager can change a role';
  end if;
  return new;
end;
$$;
revoke execute on function public.guard_profile_role() from public, anon, authenticated;

create trigger profiles_guard_role
  before update on public.profiles
  for each row execute function public.guard_profile_role();

-- -----------------------------------------------------------------------------
-- Trigger: appointment inserted -> booking_created event
-- SECURITY DEFINER because customers have no INSERT right on events.
-- -----------------------------------------------------------------------------
create or replace function public.emit_booking_created()
returns trigger
language plpgsql security definer
set search_path = ''
as $$
begin
  insert into public.events (type, source, payload)
  values ('booking_created', 'booking_app', jsonb_build_object(
    'appointment_id', new.id,
    'customer_id',    new.customer_id,
    'staff_id',       new.staff_id,
    'service_id',     new.service_id,
    'start_time',     new.start_time,
    'end_time',       new.end_time
  ));
  return new;
end;
$$;
revoke execute on function public.emit_booking_created() from public, anon, authenticated;

create trigger appointments_emit_booking_created
  after insert on public.appointments
  for each row execute function public.emit_booking_created();

-- -----------------------------------------------------------------------------
-- Trigger: feedback with rating <= 2 -> complaint_received event
-- -----------------------------------------------------------------------------
create or replace function public.emit_complaint_received()
returns trigger
language plpgsql security definer
set search_path = ''
as $$
begin
  if new.rating <= 2 then
    insert into public.events (type, source, payload)
    values ('complaint_received', 'feedback_form', jsonb_build_object(
      'feedback_id',    new.id,
      'appointment_id', new.appointment_id,
      'customer_id',    new.customer_id,
      'rating',         new.rating
    ));
  end if;
  return new;
end;
$$;
revoke execute on function public.emit_complaint_received() from public, anon, authenticated;

create trigger feedback_emit_complaint_received
  after insert on public.feedback
  for each row execute function public.emit_complaint_received();

-- -----------------------------------------------------------------------------
-- decision_log is append-only for everyone, including service_role
-- -----------------------------------------------------------------------------
create or replace function public.decision_log_block_changes()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception 'decision_log is append-only (% blocked)', tg_op;
end;
$$;

create trigger decision_log_no_update_delete
  before update or delete on public.decision_log
  for each row execute function public.decision_log_block_changes();

create trigger decision_log_no_truncate
  before truncate on public.decision_log
  for each statement execute function public.decision_log_block_changes();

revoke update, delete, truncate on public.decision_log from anon, authenticated;

-- -----------------------------------------------------------------------------
-- Customer-safe staff view (names only). Runs with owner rights on purpose, so
-- customers can read it without any access to the staff table itself.
-- -----------------------------------------------------------------------------
create view public.staff_public as
  select id, name, skills from public.staff;
comment on view public.staff_public is 'Customer-visible staff fields only (id, name, skills). No shift or availability data.';
revoke all on public.staff_public from public, anon;
grant select on public.staff_public to authenticated, service_role;

-- -----------------------------------------------------------------------------
-- Realtime
-- -----------------------------------------------------------------------------
do $$
declare t text;
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    foreach t in array array['events', 'approvals', 'agent_actions', 'appointments'] loop
      execute format('alter publication supabase_realtime add table public.%I', t);
    end loop;
  end if;
end $$;
