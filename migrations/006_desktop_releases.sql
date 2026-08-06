-- Published Skyward Installer (desktop) builds, so installed copies can update themselves
-- and the website can link to the current download without a GitHub account in the loop.
--
-- Parallel to `app_releases`, with two deliberate differences:
--
--  * The installer bytes live in a PUBLIC `desktop-releases` bucket, not a private one.
--    The updater fetches its download URL before anyone has logged in, so a signed URL
--    would need an auth round-trip the update check doesn't have. Nothing here is secret:
--    the installers are what we hand out from the download page anyway. Integrity comes
--    from the signature below, not from the URL being hard to guess.
--
--  * One row holds every platform, in `platforms`, because that is the shape Tauri's
--    updater manifest wants and a release is only meaningful when all platforms are ready.
--
-- Same publish/rollback ergonomics as app_releases: flip `is_current`.

create table if not exists desktop_releases (
  -- Semver, and the value Tauri compares against the running app's version. The check
  -- matters: Tauri parses this strictly, and a value like '1.2' makes every client fail
  -- its update check silently rather than loudly.
  version     text primary key check (version ~ '^[0-9]+\.[0-9]+\.[0-9]+$'),

  -- Tauri's updater target keys mapped to the artifact for that platform, e.g.
  --   {
  --     "linux-x86_64":   {"url": "https://…AppImage",      "signature": "dW50cnVzdGVk…"},
  --     "windows-x86_64": {"url": "https://…x64-setup.exe", "signature": "dW50cnVzdGVk…"},
  --     "darwin-x86_64":  {"url": "https://…app.tar.gz",    "signature": "dW50cnVzdGVk…"},
  --     "darwin-aarch64": {"url": "https://…app.tar.gz",    "signature": "dW50cnVzdGVk…"}
  --   }
  -- Both darwin keys point at the same universal .app.tar.gz: clients look themselves up
  -- by OS-ARCH, and a Mac never asks for a key called "universal".
  -- The signature is the content of the .sig file Tauri emits next to each artifact,
  -- produced with the private key whose public half is pinned in tauri.conf.json. A client
  -- that can't verify it refuses the update, so a swapped binary on the CDN is inert.
  platforms   jsonb not null check (jsonb_typeof(platforms) = 'object' and platforms <> '{}'::jsonb),

  -- What the website's download buttons point at, keyed by the same target names, e.g.
  --   {"windows-x86_64": {"url": "https://…x64-setup.exe"}, …}
  --
  -- Separate from `platforms` because the two disagree on macOS: an existing install
  -- updates from a .app.tar.gz, but a first-time visitor needs the .dmg, and handing them
  -- a tarball would be a dead end. They happen to coincide on Windows and Linux.
  downloads   jsonb not null default '{}'::jsonb check (jsonb_typeof(downloads) = 'object'),

  -- Shown in the "update available" prompt. Plain text, one short paragraph.
  notes       text,

  is_current  boolean not null default false,
  released_at timestamptz not null default now()
);

-- At most one row may be current. Partial unique index rather than a constraint so
-- non-current rows aren't forced to collide with each other.
create unique index if not exists desktop_releases_one_current
  on desktop_releases (is_current) where is_current;

alter table desktop_releases enable row level security;

-- No policies, matching app_releases: reads go through the backend's service-role client,
-- which bypasses RLS. The anon key that ships in the website bundle can't enumerate
-- releases directly — it goes through /api/desktop/latest.json like everyone else.

comment on table desktop_releases is
  'Index over the public desktop-releases bucket. Flip is_current to publish or roll back.';

-- ── Publishing a build ──────────────────────────────────────────────────────
-- CI does this automatically on a tag push (see .github/workflows/release.yml in
-- SkywardDesktop). To publish or roll back by hand:
--
--   begin;
--   update desktop_releases set is_current = false where is_current;
--   update desktop_releases set is_current = true where version = '0.1.2';
--   commit;
--
-- Clearing and setting are separate statements for the same reason as app_releases: a
-- combined update can transiently collide with the partial unique index.
--
-- Rolling back has the same limit as the APK: Tauri only updates when the manifest version
-- is strictly GREATER than the installed one, so pointing is_current at an older row stops
-- new machines taking the bad build but does not recall it from those that already have it.
-- A bad release still has to be fixed forward with a higher version.
