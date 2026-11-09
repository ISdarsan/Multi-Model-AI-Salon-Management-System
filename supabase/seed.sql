-- =============================================================================
-- SalonOS seed.sql : demo salon (all data is fake)
-- Runs after migrations on `supabase db reset`.
--
-- Demo logins (local dev only, password for all: demo1234)
--   manager@salonos.demo   (role manager)
--   customer1@salonos.demo (role customer)
--   customer2@salonos.demo (role customer)
--
-- Appointment/feedback event triggers are switched off while seeding so that
-- historical rows do not flood the events table; the demo starts with an empty
-- event queue and scenarios are triggered live.
-- =============================================================================

-- DETERMINISM: this seed produces identical data on every run (dates are relative to the
-- run date, everything else is fixed). It uses no random(); the sources of variation were:
--   * gen_random_uuid() ids      -> replaced below by a sequence-based id default, restored at the end
--   * ties in row_number() order -> tie-broken on stable keys (never on random ids)
--   * bcrypt salt                -> fixed salt for the demo passwords (local dev only)
--   * row order within inserts   -> explicit ORDER BY, so ids are assigned in a fixed order
create sequence public.seed_id_seq;
alter table public.products           alter column id set default md5('salonos-seed-' || nextval('public.seed_id_seq'))::uuid;
alter table public.stock_levels       alter column id set default md5('salonos-seed-' || nextval('public.seed_id_seq'))::uuid;
alter table public.stock_transactions alter column id set default md5('salonos-seed-' || nextval('public.seed_id_seq'))::uuid;
alter table public.staff              alter column id set default md5('salonos-seed-' || nextval('public.seed_id_seq'))::uuid;
alter table public.services           alter column id set default md5('salonos-seed-' || nextval('public.seed_id_seq'))::uuid;
alter table public.appointments       alter column id set default md5('salonos-seed-' || nextval('public.seed_id_seq'))::uuid;
alter table public.feedback           alter column id set default md5('salonos-seed-' || nextval('public.seed_id_seq'))::uuid;
alter table public.service_log       alter column id set default md5('salonos-seed-' || nextval('public.seed_id_seq'))::uuid;
alter table public.salon_policies     alter column id set default md5('salonos-seed-' || nextval('public.seed_id_seq'))::uuid;

alter table public.appointments disable trigger appointments_emit_booking_created;
alter table public.feedback     disable trigger feedback_emit_complaint_received;

-- -----------------------------------------------------------------------------
-- Demo auth users (the on_auth_user_created trigger creates their profiles)
-- -----------------------------------------------------------------------------
insert into auth.users
  (instance_id, id, aud, role, email, encrypted_password, email_confirmed_at,
   raw_app_meta_data, raw_user_meta_data, created_at, updated_at,
   confirmation_token, recovery_token, email_change, email_change_token_new)
select
  '00000000-0000-0000-0000-000000000000', u.id, 'authenticated', 'authenticated', u.email,
  extensions.crypt('demo1234', '$2a$10$SalonOSdemoSeed000000u'), now(),
  '{"provider":"email","providers":["email"]}'::jsonb,
  jsonb_build_object('full_name', u.full_name), now(), now(), '', '', '', ''
from (values
  ('00000000-0000-4000-8000-0000000000a1'::uuid, 'manager@salonos.demo',   'Morgan Demo'),
  ('00000000-0000-4000-8000-0000000000c1'::uuid, 'customer1@salonos.demo', 'Casey Sample'),
  ('00000000-0000-4000-8000-0000000000c2'::uuid, 'customer2@salonos.demo', 'Jamie Test')
) as u(id, email, full_name);

insert into auth.identities
  (id, user_id, provider_id, provider, identity_data, last_sign_in_at, created_at, updated_at)
select md5('salonos-identity-' || u.id::text)::uuid, u.id, u.id::text, 'email',
       jsonb_build_object('sub', u.id::text, 'email', u.email), now(), now(), now()
from auth.users u
where u.email like '%@salonos.demo'
order by u.email;

update public.profiles set role = 'manager'
where id = '00000000-0000-4000-8000-0000000000a1';

-- -----------------------------------------------------------------------------
-- Products (25): sku, name, category, supplier, price, reorder_threshold, lead_time_days
-- -----------------------------------------------------------------------------
insert into public.products (sku, name, category, supplier, price, reorder_threshold, lead_time_days) values
  ('SHA-001', 'Hydra Care Shampoo 250ml',        'shampoo',     'Lumina Supply Co',   14.50,  8, 3),
  ('SHA-002', 'Volume Boost Shampoo 250ml',      'shampoo',     'Lumina Supply Co',   14.50,  8, 3),
  ('SHA-003', 'Colour Protect Shampoo 250ml',    'shampoo',     'Lumina Supply Co',   16.00,  8, 3),
  ('SHA-004', 'Scalp Calm Shampoo 250ml',        'shampoo',     'Lumina Supply Co',   17.50,  6, 3),
  ('CON-001', 'Hydra Care Conditioner 250ml',    'conditioner', 'Lumina Supply Co',   14.50,  8, 3),
  ('CON-002', 'Volume Boost Conditioner 250ml',  'conditioner', 'Lumina Supply Co',   14.50,  8, 3),
  ('CON-003', 'Deep Repair Conditioner 250ml',   'conditioner', 'Lumina Supply Co',   18.00,  6, 3),
  ('CON-004', 'Detangle Leave-in 200ml',         'conditioner', 'Lumina Supply Co',   15.50,  6, 3),
  ('SER-001', 'Shine Serum 50ml',                'serum',       'Aurelle Beauty Ltd', 22.00,  5, 5),
  ('SER-002', 'Anti-Frizz Serum 50ml',           'serum',       'Aurelle Beauty Ltd', 22.00,  5, 5),
  ('SER-003', 'Bond Repair Serum 50ml',          'serum',       'Aurelle Beauty Ltd', 29.00,  5, 5),
  ('COL-1N',  'Colour Tube 1N Black 60ml',       'colour',      'Prisma Colour Labs', 11.00, 10, 7),
  ('COL-4N',  'Colour Tube 4N Dark Brown 60ml',  'colour',      'Prisma Colour Labs', 11.00, 10, 7),
  ('COL-5N',  'Colour Tube 5N Light Brown 60ml', 'colour',      'Prisma Colour Labs', 11.00, 10, 7),
  ('COL-6N',  'Colour Tube 6N Dark Blonde 60ml', 'colour',      'Prisma Colour Labs', 11.00, 10, 7),
  ('COL-7N',  'Colour Tube 7N Blonde 60ml',      'colour',      'Prisma Colour Labs', 11.00, 10, 7),
  ('COL-8N',  'Colour Tube 8N Light Blonde 60ml','colour',      'Prisma Colour Labs', 11.00, 10, 7),
  ('COL-6R',  'Colour Tube 6R Copper Red 60ml',  'colour',      'Prisma Colour Labs', 12.00,  8, 7),
  ('COL-9V',  'Colour Tube 9V Violet Toner 60ml','colour',      'Prisma Colour Labs', 12.00,  8, 7),
  ('SKN-001', 'Gentle Foam Cleanser 150ml',      'skincare',    'Aurelle Beauty Ltd', 19.00,  4, 5),
  ('SKN-002', 'Daily Moisturiser 50ml',          'skincare',    'Aurelle Beauty Ltd', 24.00,  4, 5),
  ('SKN-003', 'Clay Face Mask 75ml',             'skincare',    'Aurelle Beauty Ltd', 21.00,  4, 5),
  ('SKN-004', 'Eye Revive Cream 15ml',           'skincare',    'Aurelle Beauty Ltd', 27.00,  3, 5),
  ('SKN-005', 'Daily SPF 30 50ml',               'skincare',    'Aurelle Beauty Ltd', 23.00,  4, 5),
  ('SKN-006', 'Smoothing Scrub 100ml',           'skincare',    'Aurelle Beauty Ltd', 18.00,  3, 5);

-- Stock is built purely from stock_transactions: the stock_transactions_apply trigger
-- (0003) maintains stock_levels.current_qty. Order matters because current_qty cannot go negative.
-- 1) every product starts with a zero stock row
insert into public.stock_levels (product_id, current_qty)
select id, 0 from public.products order by sku;

-- 2) an opening delivery, large enough that two weeks of sales never go below zero
insert into public.stock_transactions (product_id, qty_change, type, created_at)
select id, reorder_threshold * 2 + 40, 'delivery', now() - interval '14 days' from public.products order by sku;

-- 3) two weeks of sales/usage (feeds sales velocity)
insert into public.stock_transactions (product_id, qty_change, type, created_at)
select p.id,
       -(1 + (d + abs(hashtext(p.sku))) % 2),
       case when p.category = 'colour' then 'use'::public.stock_txn_type else 'sale'::public.stock_txn_type end,
       now() - make_interval(days => d)
from public.products p
cross join generate_series(1, 13) as d
where (d + abs(hashtext(p.sku))) % 3 <> 0
order by p.sku, d;

-- 4) a stocktake adjustment brings every product to its target quantity:
--    2x the reorder threshold, except three products deliberately BELOW threshold
--    (demo is never empty). Two are colour tubes used by services booked tomorrow (scenario 1).
insert into public.stock_transactions (product_id, qty_change, type, created_at)
select p.id,
       coalesce(low.qty, p.reorder_threshold * 2) - sl.current_qty,
       'adjustment',
       now() - interval '1 hour'
from public.products p
join public.stock_levels sl on sl.product_id = p.id
left join (values ('COL-6N', 2), ('COL-7N', 3), ('SHA-003', 4)) as low(sku, qty) on low.sku = p.sku
order by p.sku;

-- -----------------------------------------------------------------------------
-- Staff (5), services (8), service_products
-- -----------------------------------------------------------------------------
insert into public.staff (name, skills, shift_start, shift_end, work_days, is_available) values
  ('Aria Demo',  '{cut,styling}',    '09:00', '18:00', '{1,2,3,4,5,6}', true),
  ('Ben Sample', '{colour,cut}',     '09:00', '18:00', '{1,2,3,4,5,6}', true),
  ('Cleo Test',  '{colour,styling}', '09:00', '18:00', '{1,2,3,4,5,6}', true),
  ('Dev Mock',   '{cut,styling}',    '09:00', '18:00', '{1,2,3,4,5,6}', true),
  ('Esme Fake',  '{skincare,styling}','09:00','18:00', '{1,2,3,4,5,6}', true);

insert into public.services (name, duration_minutes, price) values
  ('Women''s Cut & Style',        60,  55.00),
  ('Men''s Cut',                  30,  28.00),
  ('Full Colour',                120, 120.00),
  ('Root Touch-up',               90,  80.00),
  ('Highlights',                 150, 160.00),
  ('Blow Dry & Style',            45,  40.00),
  ('Deep Conditioning Treatment', 45,  50.00),
  ('Facial & Skincare',           60,  70.00);

insert into public.service_products (service_id, product_id, qty_per_service)
select s.id, p.id, v.qty
from (values
  ('Women''s Cut & Style',        'SHA-001', 1), ('Women''s Cut & Style',        'CON-001', 1),
  ('Men''s Cut',                  'SHA-002', 1),
  ('Full Colour',                 'COL-6N',  2), ('Full Colour',                 'SER-003', 1),
  ('Root Touch-up',               'COL-7N',  1), ('Root Touch-up',               'COL-6N',  1),
  ('Highlights',                  'COL-8N',  2), ('Highlights',                  'COL-9V',  1),
  ('Blow Dry & Style',            'SER-001', 1), ('Blow Dry & Style',            'SER-002', 1),
  ('Deep Conditioning Treatment', 'CON-003', 2), ('Deep Conditioning Treatment', 'SER-003', 1),
  ('Facial & Skincare',           'SKN-001', 1), ('Facial & Skincare',           'SKN-002', 1),
  ('Facial & Skincare',           'SKN-003', 1)
) as v(service_name, sku, qty)
join public.services s on s.name = v.service_name
join public.products p on p.sku  = v.sku;

-- -----------------------------------------------------------------------------
-- Appointments: 14 days (7 back, 7 forward), 3 slots per stylist per day, Sundays closed.
-- Deterministic pattern; some slots left free. Tomorrow always has colour services booked
-- (Ben: Full Colour 09:30, Cleo: Highlights 12:30) for scenario 1.
-- -----------------------------------------------------------------------------
do $$
declare
  elig  jsonb := '{
    "Aria Demo":  ["Women''s Cut & Style", "Men''s Cut", "Blow Dry & Style"],
    "Ben Sample": ["Full Colour", "Root Touch-up", "Men''s Cut"],
    "Cleo Test":  ["Highlights", "Root Touch-up", "Blow Dry & Style", "Full Colour"],
    "Dev Mock":   ["Men''s Cut", "Women''s Cut & Style", "Deep Conditioning Treatment"],
    "Esme Fake":  ["Facial & Skincare", "Deep Conditioning Treatment", "Blow Dry & Style"]
  }';
  slot_starts time[] := array[time '09:30', time '12:30', time '15:30'];
  d int; k int; s_idx int; n int; h int;
  r record;
  v_service record;
  v_service_name text;
  v_start timestamptz;
  v_status public.appointment_status;
  v_customer uuid;
  customers uuid[] := array['00000000-0000-4000-8000-0000000000c1'::uuid,
                            '00000000-0000-4000-8000-0000000000c2'::uuid];
begin
  for d in -7..6 loop
    if extract(isodow from current_date + d) = 7 then continue; end if;
    s_idx := 0;
    for r in select id, name from public.staff order by name loop
      s_idx := s_idx + 1;
      n := jsonb_array_length(elig -> r.name);
      for k in 0..2 loop
        v_service_name := null;
        -- forced colour bookings for tomorrow
        if d = 1 and r.name = 'Ben Sample' and k = 0 then v_service_name := 'Full Colour'; end if;
        if d = 1 and r.name = 'Cleo Test'  and k = 1 then v_service_name := 'Highlights';  end if;

        h := (d + 7) * 7 + s_idx * 3 + k * 5;
        if v_service_name is null then
          continue when h % 4 = 0;  -- leave ~25% of slots free
          v_service_name := elig -> r.name ->> (((d + s_idx + k) % n + n) % n);
        end if;

        select id, duration_minutes into v_service from public.services where name = v_service_name;
        v_start := ((current_date + d)::timestamp + slot_starts[k + 1]) at time zone 'UTC';

        if d < 0 then
          v_status := case when h % 11 = 0 then 'cancelled' else 'completed' end;
        elsif d = 0 then
          v_status := case k when 0 then 'completed' when 1 then 'in_progress' else 'booked' end;
        else
          v_status := case when h % 13 = 0 then 'cancelled' else 'booked' end;
        end if;

        v_customer := case when h % 4 = 1 then customers[1 + ((h / 4) % 2)] else null end;  -- others are walk-ins

        insert into public.appointments (customer_id, staff_id, service_id, start_time, end_time, status)
        values (v_customer, r.id, v_service.id, v_start,
                v_start + make_interval(mins => v_service.duration_minutes), v_status);
      end loop;
    end loop;
  end loop;
end $$;

-- Service log: one row per completed appointment (Quality Agent input)
insert into public.service_log (appointment_id, readiness_notes, quality_notes, created_at)
select a.id,
       (array['Workstation clean, tools sterilised.',
              'Workstation ready; towels restocked.',
              'Colour bowl and brushes prepared.',
              'Basin and chair cleaned before client.',
              'Tools sterilised; minor delay finding clips.'])[1 + (rn % 5)],
       (array['Client happy with result.',
              'Finished on time, no issues.',
              'Colour left on about 10 minutes over target.',
              'Slight unevenness at the back, corrected before leaving.',
              'Good result, client rebooked.'])[1 + ((rn / 2) % 5)],
       a.end_time
from (
  select id, end_time, row_number() over (order by start_time, staff_id) as rn
  from public.appointments where status = 'completed'
) a
order by a.rn;

-- -----------------------------------------------------------------------------
-- Feedback (40 rows, 8 of them rating <= 2), linked to completed appointments
-- -----------------------------------------------------------------------------
insert into public.feedback (customer_id, appointment_id, text, rating, category, sentiment, created_at)
select a.customer_id, a.id, f.text, f.rating, f.category,
       case when f.rating <= 2 then 'negative' when f.rating = 3 then 'neutral' else 'positive' end::public.sentiment,
       coalesce(a.end_time + interval '1 hour', now() - make_interval(hours => f.n::int))
from (values
  ( 1, 5, 'service',  'Lovely cut, exactly what I asked for. Thank you!'),
  ( 2, 4, 'service',  'Really friendly stylist and a great blow dry.'),
  ( 3, 5, 'colour',   'Best colour I have had in years, so shiny.'),
  ( 4, 3, 'wait time','Result was fine but I waited about 20 minutes past my slot.'),
  ( 5, 1, 'colour',   'Colour came out patchy and far too orange. Very disappointed and I would like a refund.'),
  ( 6, 4, 'service',  'Quick and tidy trim, would book again.'),
  ( 7, 5, 'atmosphere','The salon smells amazing and the staff are so welcoming.'),
  ( 8, 2, 'wait time','Waited 35 minutes in the waiting area with no update. Not good.'),
  ( 9, 4, 'service',  'Great facial, skin feels fresh.'),
  (10, 5, 'service',  'Perfect highlights, natural and bright.'),
  (11, 3, 'price',    'Nice result but a bit pricey for a trim.'),
  (12, 4, 'service',  'Good deep conditioning treatment, hair feels soft.'),
  (13, 5, 'service',  'Quick, professional and friendly.'),
  (14, 1, 'service',  'Stylist was rushed and cut my fringe too short. Unhappy.'),
  (15, 4, 'colour',   'Root touch-up looks seamless.'),
  (16, 5, 'atmosphere','Clean, calm and comfortable. Lovely coffee too.'),
  (17, 3, 'wait time','Slightly late start but the stylist apologised.'),
  (18, 5, 'service',  'Brilliant blow dry for my event, lasted all night.'),
  (19, 2, 'colour',   'Colour did not match the shade we agreed on. Please call me.'),
  (20, 4, 'service',  'Very good men''s cut, clean fade.'),
  (21, 5, 'service',  'Wonderful experience from start to finish.'),
  (22, 4, 'product',  'Loved the serum they recommended, bought two.'),
  (23, 2, 'cleanliness','Basin was still wet and hair clippings on the chair when I sat down.'),
  (24, 5, 'service',  'Amazing facial, really relaxing.'),
  (25, 3, 'price',    'Good service, would like a loyalty discount.'),
  (26, 4, 'service',  'Efficient and friendly, thanks.'),
  (27, 1, 'service',  'Appointment was cancelled on me with no notice. Terrible.'),
  (28, 5, 'colour',   'Highlights are perfect, thank you!'),
  (29, 4, 'service',  'Lovely trim and style.'),
  (30, 3, 'wait time','Waited a little but worth it in the end.'),
  (31, 5, 'service',  'Excellent as always.'),
  (32, 4, 'atmosphere','Pleasant staff and a relaxing space.'),
  (33, 5, 'product',  'The shampoo they used smelled great and left my hair soft.'),
  (34, 2, 'service',  'Haircut was uneven on one side. Needs fixing.'),
  (35, 4, 'service',  'Good value deep conditioning.'),
  (36, 3, 'service',  'Okay overall, nothing special.'),
  (37, 5, 'service',  'Friendly team, great colour consultation.'),
  (38, 1, 'wait time','Waited nearly an hour and was never offered a drink or an apology.'),
  (39, 4, 'service',  'Great cut, will return.'),
  (40, 5, 'service',  'Fantastic service, highly recommend.')
) as f(n, rating, category, text)
left join (
  select id, customer_id, end_time, row_number() over (order by start_time, staff_id) as rn
  from public.appointments where status = 'completed'
) a on a.rn = f.n * 2
order by f.n;

-- Tickets for the open complaints would be created by the CX Agent, so none are seeded.

-- -----------------------------------------------------------------------------
-- Salon policies (5), used later for complaint-policy RAG
-- -----------------------------------------------------------------------------
insert into public.salon_policies (title, body) values
  ('Refund Policy',
   'A refund or re-do is offered when a service result clearly differs from what was agreed at consultation, and the client reports it within 7 days. Refunds up to 50% of the service price may be suggested by staff; any full refund or compensation over 50 requires manager approval. Retail products can be returned unopened within 14 days with a receipt.'),
  ('Delay Compensation',
   'If a client waits more than 15 minutes beyond their booked time, they are offered a complimentary drink and an apology. For waits over 30 minutes, offer 10% off the current service. For waits over 60 minutes, the manager may offer a free add-on treatment or a voucher. All compensation beyond a drink is logged and needs manager approval.'),
  ('Escalation Rules',
   'Complaints with a rating of 1 or 2 open a ticket. Mentions of allergic reaction, injury or legal action are high priority and go to the manager immediately. Complaints about colour results are checked against the service log before a reply is drafted. All replies to unhappy clients are reviewed by the manager before sending.'),
  ('Cancellation and Rescheduling',
   'Clients may cancel or reschedule free of charge up to 24 hours before the appointment. If the salon must change a booking (for example because a stylist is absent), the client is offered the nearest alternative slot with a suitably skilled stylist, or a full cancellation with no charge, and is notified as early as possible.'),
  ('Patch Test and Allergy Policy',
   'A patch test is required at least 48 hours before the first colour service for any new client. Clients with known allergies must be flagged on their booking. If a reaction is reported after a service, advise the client to seek medical advice and escalate to the manager at once.');

-- -----------------------------------------------------------------------------
-- Restore the normal random id defaults and remove the helper sequence
alter table public.products           alter column id set default gen_random_uuid();
alter table public.stock_levels       alter column id set default gen_random_uuid();
alter table public.stock_transactions alter column id set default gen_random_uuid();
alter table public.staff              alter column id set default gen_random_uuid();
alter table public.services           alter column id set default gen_random_uuid();
alter table public.appointments       alter column id set default gen_random_uuid();
alter table public.feedback           alter column id set default gen_random_uuid();
alter table public.service_log        alter column id set default gen_random_uuid();
alter table public.salon_policies     alter column id set default gen_random_uuid();
drop sequence public.seed_id_seq;

alter table public.appointments enable trigger appointments_emit_booking_created;
alter table public.feedback     enable trigger feedback_emit_complaint_received;
