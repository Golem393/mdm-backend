-- Removes the finalized column added in 003_devices_finalized.sql.
--
-- It tracked whether USB debugging had been sealed via the FINALIZE_SETUP broadcast.
-- That whole flow (dev-mode login + 10-minute timer to temporarily reopen debugging,
-- then reseal it on exit) has been removed — USB debugging is left enabled on the
-- device permanently now, so there is nothing left for this column to track.

drop index if exists devices_not_finalized;

alter table devices
  drop column if exists finalized;
