-- Tracks whether a provisioned phone actually got USB debugging sealed shut.
--
-- `finalize_setup` broadcasts FINALIZE_SETUP, which applies DISALLOW_DEBUGGING_FEATURES on
-- the device. Until that lands, the phone is provisioned but still ADB-reachable — and
-- since AdbCommandReceiver is exported, anyone can broadcast CLEAR_OWNER and then uninstall
-- the app. The protection is trivially bypassable in that window.
--
-- The desktop app used to swallow a failed finalize entirely: no retry, no warning, no
-- record, and no way to re-send it. This column makes that state visible and recoverable —
-- the Devices page surfaces a "Finish securing" action whenever it is false.
--
-- Operationally this is the alert query:
--   select serial, model, enrolled_at from devices where not finalized;
-- Every row is a customer whose child's phone can be freed with two adb commands.

alter table devices
  add column if not exists finalized boolean not null default false;

comment on column devices.finalized is
  'True once USB debugging has been sealed via FINALIZE_SETUP. False means the phone is provisioned but still bypassable over ADB.';

-- Partial index: this is only ever queried as "which devices are still unsealed".
create index if not exists devices_not_finalized
  on devices (finalized)
  where not finalized;
