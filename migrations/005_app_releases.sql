-- Published SkywardBlocker builds, so the desktop companion can fetch the APK itself
-- instead of asking the parent to type a file path.
--
-- The APK bytes live in the private `app-releases` Storage bucket; this table is the
-- index over them. Rows are append-only in practice: `version_code` is the primary key
-- and objects in the bucket are never overwritten, so an older release stays installable
-- forever. Rolling back is therefore a flag flip, not a re-upload.
--
-- No policies, deliberately. Reads go through the backend's service-role client (the same
-- one `schedules` and `profiles` use), which bypasses RLS — so with RLS on and no policy,
-- the anon key that ships publicly in the website bundle can't enumerate releases, and the
-- bucket itself stays private behind short-lived signed URLs.

create table if not exists app_releases (
  -- Android's own upgrade comparison key, and ours. Must strictly increase per release:
  -- Android refuses to install an APK whose versionCode is <= the one on the device.
  version_code  integer primary key,
  -- Cosmetic, shown to the parent (e.g. "1.2"). Never compared.
  version_name  text not null,
  -- Object name within the `app-releases` bucket, e.g. 'skywardblocker-2.apk'.
  storage_path  text not null unique,
  -- Lowercase hex SHA-256 of the exact APK bytes. The desktop app verifies the download
  -- against this before letting it near `adb install`, so a truncated or swapped file
  -- fails loudly rather than halfway through provisioning a phone.
  sha256        text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  size_bytes    bigint not null check (size_bytes > 0),
  -- Shown on the update screen. Plain text, one short paragraph.
  release_notes text,
  is_current    boolean not null default false,
  released_at   timestamptz not null default now()
);

-- At most one row may be current. A partial unique index rather than a constraint so
-- non-current rows aren't forced to collide with each other.
create unique index if not exists app_releases_one_current
  on app_releases (is_current) where is_current;

alter table app_releases enable row level security;

comment on table app_releases is
  'Index over the private app-releases Storage bucket. Flip is_current to publish or roll back.';

-- ── Publishing a build ──────────────────────────────────────────────────────
-- 1. Upload the APK to the `app-releases` bucket as skywardblocker-<version_code>.apk
-- 2. Insert its row and move the flag, in one transaction:
--
--   begin;
--   insert into app_releases (version_code, version_name, storage_path, sha256, size_bytes, release_notes)
--   values (3, '1.3', 'skywardblocker-3.apk', '<sha256sum>', <bytes>, 'What changed.');
--   update app_releases set is_current = false where is_current;
--   update app_releases set is_current = true where version_code = 3;
--   commit;
--
-- Clearing and setting are separate statements on purpose. A single
-- `set is_current = (version_code = 3)` would be tidier, but Postgres enforces a unique
-- index per row as the update proceeds — so if it happened to write the new current row
-- before clearing the old one, the statement would fail on a collision that doesn't exist
-- by the time it commits. Clearing first can never collide.
--
-- Rolling back is the same two statements pointed at an older version_code. The old APK is
-- still in the bucket, so nothing needs re-uploading — but note that phones already on the
-- newer build stay there: Android refuses to install a lower versionCode. A rollback only
-- protects devices that haven't updated yet; a bad release still has to be fixed forward.

begin;

insert into app_releases (version_code, version_name, storage_path, sha256, size_bytes, release_notes)
values (
  2,
  '1.2',
  'skywardblocker-2.apk',
  'fd7ed17268e81ba03cb876f1b1a1c1417416e824b0ade20d90b81112c6b5e0f2',
  9225084,
  'First release-signed build, delivered automatically by the desktop app.'
)
on conflict (version_code) do nothing;

update app_releases set is_current = false where is_current;
update app_releases set is_current = true where version_code = 2;

commit;
