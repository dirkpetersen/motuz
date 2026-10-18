---
paths:
  - "src/backend/api/utils/schedule.py"
  - "src/backend/api/managers/schedule_manager.py"
  - "src/backend/api/models/copy_schedule.py"
  - "src/backend/api/views/copy_schedule_views.py"
  - "src/backend/api/managers/copy_job_manager.py"
  - "bin/systemd/celery_worker.sh"
  - "deployment/docker/celery/celery-entrypoint.sh"
  - "src/frontend/js/utils/copySchedule.js"
  - "src/frontend/js/views/Dialogs/CopyJobSchedule.jsx"
  - "src/frontend/js/views/App/CopyJobSection/CopyScheduleTable.jsx"
  - "test/backend/test_schedules.py"
  - "test/frontend/test_copy_schedule.mjs"
---

# Scheduled copies

Detail for scheduled repeating copies (README "Scheduled copies"). Summary in CLAUDE.md.

- **Model** `copy_schedule` (migration `e3a5c7e9f1b2`, after `d2f4a6c8e0b1`): owner, the copy job's parameters (src/dst cloud ids and paths, copy_links, notification_email, validated `performance`), `frequency` hourly|daily|weekly|monthly, `minute` 0-59, `hour` 0-23 (daily and longer), `weekday` 0-6 = Monday-Sunday (weekly), `day_of_month` 1-28 (monthly), all UTC, unused fields NULL; `enabled`, `next_run_at` / `last_run_at` (naive UTC), `last_job_id`, `last_status` (text for the Schedules tab), `skipped_runs`, `egress_estimate`. `src_cloud`/`dst_cloud` backrefs cascade: deleting a connection deletes its schedules. `copy_job.schedule_id` (plain integer, no FK: deleting a schedule keeps its jobs).
- **Times** `utils/schedule.py` (stdlib): `validate` (ScheduleError -> 400), `next_run(spec, after)` strictly after `after`, whole minutes, month/year boundaries; `describe` ("every day at 02:30 UTC", the DTO's `schedule_text`), `iso_utc` (DTO times `...Z`), `RUNS_PER_MONTH` (730.5/30.44/4.35/1, also in `utils/copySchedule.js`). The browser converts local <-> UTC with `getTimezoneOffset()` of now (`toUtcSchedule` / `fromUtcSchedule`): weekly days shift with the time, a monthly day outside 1-28 in UTC is refused in the dialog; DST moves the local time by an hour (said in the UI).
- **Jobs** `copy_job_manager.create(data)` = `create_for(get_logged_in_user(request), data)`; the runner calls `create_for(schedule.owner, data, schedule=schedule)`: the same `owned_cloud(id, owner)` (404 via `cloud_connection_manager.owned_by`), space check, performance validation, routing (remote workers unchanged), `overwrite_policy.register`. With a schedule: `overwrite = overwrite_policy.general_overwrites()` (never the Motuz-destination rule), `egress_estimate = confirmed_copy(src, dst, schedule.egress_estimate)` + `schedule_id`. copyto never deletes (`test_copy_command_never_syncs`).
- **Runner** `schedule_manager.tick(now)`: `worker_manager.expire_leases()`, then up to `MAX_RUNS_PER_TICK` times `_claim`: `SELECT id, next_run_at ... WHERE enabled AND next_run_at <= now ORDER BY next_run_at LIMIT 1 FOR UPDATE SKIP LOCKED` (ignored on SQLite), then `UPDATE ... SET next_run_at=next_run(now), last_run_at=now WHERE id AND next_run_at=<old> AND enabled` and commit; rowcount 0 = another runner won. Then `run_claimed`: previous job (`last_job_id`) in PROGRESS -> skipped (`skipped_runs`, status); HTTP_4XX -> status "Not run at ...: <message>", and `egress_manager.CONFIRM_MESSAGE` (a fee nobody confirmed) disables the schedule; other errors (dispatch) -> status with `error_text`, `last_job_id` of the FAILED job. No catch-up: the next time is computed from now. `run_loop` ticks at each minute boundary; `manage.py schedules run --loop` is started in the background next to Celery by `deployment/docker/celery/celery-entrypoint.sh` and `bin/systemd/celery_worker.sh` (`motuz-celery.service` ExecStart, `exec celery` so the warm shutdown stays) and by `bin/dev/celery_start.sh`; one celery per installation = one runner. Not Celery beat (copy tasks occupy the worker processes for hours). `manage.py schedules tick|list`.
- **API** `views/copy_schedule_views.py` (`/api/copy-schedules/`, `/<id>` GET/PATCH `{enabled}`/DELETE; create takes `run_now`; 404 for other users' schedules and connections). With `run_now` the first job is created in the same request; a 4xx of that job rolls the schedule back.
- **AWS egress** `schedule_manager.confirmed_egress`: `egress_manager.confirmed_copy` (first run, 400 `CONFIRM_MESSAGE`) and then `repeat_confirmed: true` (400 `REPEAT_CONFIRM_MESSAGE`); stored with `egress_cost.repeated` (`new_bytes_per_run`, `runs_per_month`, `monthly_low_usd/high_usd` at the confirmed rates).
- **Frontend**: `CopyJobSchedule.jsx` (collapsed `<details>` "Scheduled repeat" in `NewCopyJobDialog`, controlled by the dialog's `schedule` state; time zone note, add-only text, egress warning with "Expected new data per run (GB)", default 10% of the measured size, and a required checkbox). `CopyJobEgress` reports `estimate` too. Submit reads "Create schedule" and posts `createCopySchedule` per source path (UTC fields, `run_now`, `egress_estimate` = `schedulePayload`); the dialog stays open on failure (`CREATE_COPY_SCHEDULE_FAILURE` not in `alertReducer`). `CopyScheduleTable.jsx` ("Schedules" tab, refetched every 30 s): repeats and next run in local time, last job and status, enabled toggle, delete. `EditCopyJobDialog` shows "Schedule". Helpers with node tests in `utils/copySchedule.js`.
- Tests: `test/backend/test_schedules.py`, `test/frontend/test_copy_schedule.mjs`, e2e `e2e_test.py` ("schedule:": a tick via `manage.py schedules tick` after `psql` makes it due; the changed file is kept, nothing deleted) and `ui/ui_test.mjs` ("copy schedule").
