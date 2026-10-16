---
paths:
  - "src/backend/api/utils/rclone_tuning.py"
  - "src/frontend/js/utils/copyPerformance.js"
  - "src/frontend/js/views/Dialogs/CopyJobPerformance.jsx"
  - "test/backend/test_rclone_tuning.py"
  - "test/frontend/test_copy_performance.mjs"
---

# Jobs: rclone performance tuning

Detail for rclone performance settings and per-job overrides.

**rclone performance** (`utils/rclone_tuning.py`, README "Performance tuning"; stdlib only). Installation defaults `MOTUZ_RCLONE_{TRANSFERS,CHECKERS,MULTI_THREAD_STREAMS,MULTI_THREAD_CUTOFF,BUFFER_SIZE,S3_UPLOAD_CONCURRENCY,S3_CHUNK_SIZE,AZUREBLOB_UPLOAD_CONCURRENCY,AZUREBLOB_CHUNK_SIZE}` (unset/empty = rclone's default = no flag, so the command line is unchanged), caps `MOTUZ_RCLONE_MAX_{TRANSFERS,CHECKERS,MULTI_THREAD_STREAMS,UPLOAD_CONCURRENCY}` (64/128/32/64), `MOTUZ_RCLONE_MEMORY_BUDGET` (8G) and `MOTUZ_RCLONE_EXTRA_FLAGS` (only the flags in `EXTRA_FLAGS`, each with a validated value; no free-form flags); passed to app and celery via the `&rclone-environment` anchor in `docker-compose.yml`. `config.py` calls `rclone_tuning.settings()` at import, so an invalid value stops the app and the worker with a `TuningConfigError` naming the variable. Jobs store validated overrides in `copy_job.performance` / `hashsum_job.performance` (JSON; hashsum only `checkers`); names outside `PER_JOB` (e.g. `buffer_size`), non-integer / non-size values, values above a cap and combinations whose `estimate_memory` (`transfers × (max(1, streams) × buffer + max(upload_concurrency, streams) × chunk)`, chunk term for S3/Azure destinations only) exceeds the budget are a 400; settings for another backend than the destination's are dropped. `copy_flags` / `hashsum_flags` re-validate stored values against the current caps and emit one argv item per flag (`--transfers=32`) formatted from parsed numbers, inserted before `copyto` / `md5sum`; never build these flags from user text. `GET /api/copy-jobs/performance/?dst_cloud_id=` (`describe`) returns the fields for the destination's type, limits, defaults, `buffer_size`, the budget and the presets (`PRESETS`: Default, Many small files, Few large files, Maximum, clamped to the caps and reduced by `fit_preset` until they fit the budget). Frontend: `CopyJobPerformance.jsx` (collapsed section in `NewCopyJobDialog`), helpers with node tests in `utils/copyPerformance.js` (validation, the same estimate, preset matching); `createCopyJob` keeps the dialog open on failure and the dialog shows the error (`CREATE_COPY_JOB_FAILURE` is not in `alertReducer`); `EditCopyJobDialog` shows the settings, Retry keeps them, Check Integrity passes `checkers`. The e2e stack sets `MOTUZ_RCLONE_CHECKERS=16` and `MOTUZ_RCLONE_MAX_TRANSFERS=48` (`test/e2e/compose.yml`).
