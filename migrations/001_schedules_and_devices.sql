-- Schedules and devices for the desktop companion.
--
-- Both tables carry a UNIQUE index on user_id: that is what actually enforces the product
-- rule of one schedule and one device per account. The UI must not be the only guard.
--
-- Schedules are immutable by design — there is deliberately no update or delete endpoint.
-- When a parent emails support asking for a change, support edits the row directly here.

alter table profiles
  add column if not exists remove_enabled boolean not null default false;

comment on column profiles.remove_enabled is
  'Unlocks the Remove App section in the desktop companion. Flipped by support on request.';

create table if not exists schedules (
  id                uuid primary key default gen_random_uuid(),
  user_id           uuid not null references auth.users(id) on delete cascade,

  lock_start_hour   smallint not null check (lock_start_hour between 0 and 23),
  lock_start_minute smallint not null check (lock_start_minute between 0 and 59),
  lock_end_hour     smallint not null check (lock_end_hour between 0 and 23),
  lock_end_minute   smallint not null check (lock_end_minute between 0 and 59),

  -- Bit 0 = Sunday … bit 6 = Saturday. At least one day must be selected.
  days_mask         smallint not null check (days_mask between 1 and 127),

  active_from       timestamptz not null,
  active_until      timestamptz not null,
  timezone_id       text not null,

  status            text not null default 'created' check (status in ('created', 'pushed')),
  pushed_at         timestamptz,
  created_at        timestamptz not null default now(),

  constraint schedules_period_ordered check (active_until > active_from)
);

create unique index if not exists schedules_one_per_user on schedules (user_id);

create table if not exists devices (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references auth.users(id) on delete cascade,
  serial      text not null,
  model       text,
  enrolled_at timestamptz not null default now()
);

create unique index if not exists devices_one_per_user on devices (user_id);

-- RLS: the backend uses the service key and bypasses these, but they matter if the website
-- or any client ever reads these tables directly with a user token.
alter table schedules enable row level security;
alter table devices   enable row level security;

drop policy if exists schedules_owner_read on schedules;
create policy schedules_owner_read on schedules
  for select using (auth.uid() = user_id);

drop policy if exists devices_owner_read on devices;
create policy devices_owner_read on devices
  for select using (auth.uid() = user_id);
