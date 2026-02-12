# Code Review — cycling_training.py

## Summary
This file is a single, ~2500-line CLI that mixes ETL, analytics, reporting, and race planning. It works, but there are concrete maintainability, correctness, and performance issues. The largest risks are duplicated definitions that silently override earlier logic, inconsistent PMC calculations, and missing error handling around DB/API interactions. There are also edge cases around dates, None handling, and API pagination.

Below I cite line ranges based on the current file view. Line numbers are approximate because the file is long.

---

## 1) Code quality, structure, maintainability

### A. Duplicate function definitions overwrite earlier logic
- **`calc_pmc()` is defined twice**. First at ~lines 235–364, then again at ~lines 470–640. The second definition overrides the first in Python, so the earlier “two-pass anchor scaling” logic is dead code. This is a major maintainability and correctness risk.
- **`post_ride()` is defined twice**. First at ~lines 367–456, then again at ~lines 650–770. The second definition overrides the first, so the UI/logic in the first is dead.
- **`ftp_project()` is defined twice**. First at ~lines 457–523, then again at ~lines 773–842. The second wins.
- **`weekly_summary()` is defined twice**. First at ~lines 526–610, then again at ~lines 845–1006. The second wins.

**Impact:**
- Any future edits to the earlier versions will never run. This also makes it unclear which logic is the intended one.

**Fix:**
- Remove duplicates or split into modules and import explicitly.

### B. Single-file CLI is hard to maintain
The file includes ETL (Whoop/TP/Strava), analytics (PMC, correlations), forecasting (FTP), and race planning. This should be split into modules such as:
- `db.py` (connection + helpers)
- `providers/whoop.py`, `providers/trainingpeaks.py`, `providers/strava.py`
- `analytics/pmc.py`, `analytics/correlations.py`, `analytics/insights.py`
- `reports/weekly.py`, `reports/status.py`
- `race_plan.py`

This would also allow shared utilities (date parsing, formatting, error handling) to be reused.

### C. Mixed UX and data logic
Many functions do DB operations then print directly to console. This makes testing and reuse difficult.
- Example: `weekly_summary()` (~lines 845–1006) mixes SQL, calculations, and formatting.

**Fix:** return data structures and have a separate rendering layer.

---

## 2) Bug risks and edge cases

### A. Duplicate `calc_pmc()` implementations conflict
- The first `calc_pmc()` uses an “anchor scaling” dry run, then adjusts starting CTL/ATL to match an anchor (~lines 260–330). The second `calc_pmc()` uses a different anchor-forward approach (~lines 470–640) and explicitly does **not** recompute before the anchor.
- Because the second overrides the first, the anchor scaling logic never runs, and comments above the first are misleading.

### B. `sync_whoop()` and `sync_tp()` use `datetime.now()` without timezone
- `cutoff = (datetime.now() - timedelta(days=days)).strftime(...)` (~line 78)
- TrainingPeaks date uses `datetime.now()` and `today.weekday()` (~lines 144–160)
These use local server time, not user locale. Race and training dates can drift if server tz differs from desired timezone.

### C. Whoop `limit` is capped at 25
- `limit = min(days + 1, 25)` (~line 61)
If `--days` > 24, the API request does not paginate. Data for more than 25 days will be silently missing.

### D. `calc_workout_quality()` uses `if not all([...])`
- At ~lines 118–125, this returns None if any inputs are falsy. This treats valid values like `0.0` or `0` as missing.
- Example: a planned workout with `tss_planned=0` or `if_planned=0` returns None, but those should be handled explicitly.

### E. `post_ride()` second version uses global `FTP = 263`
- At ~lines 452 and 650, `FTP` is a constant used to compute IF from NP.
- But there is also a `ftp_history` table and `_get_current_ftp()` helper later. Using a hard-coded FTP can produce incorrect IF when athlete’s FTP changes.

### F. `weekly_summary()` second version: uses `training_load` without filtering by week
- It runs `SELECT * FROM training_load ORDER BY date DESC LIMIT 2` (~line 890), not constrained to the week being summarized. This is inconsistent with the first version’s `WHERE date BETWEEN week_start AND week_end` (~lines 567–576).

### G. `sync_tp()` uses `tss_actual` and `if_actual` even for uncompleted workouts
- `completed` is computed by `tss_actual is not None and tss_actual > 0` (~line 173). Planned workouts without TSS actual remain `completed=False`, but `tss_actual` might still be `0` or missing. The DB will store `tss_actual` and `if_actual` even for incomplete workouts, which can distort analytics unless filters are used everywhere.

### H. `calc_pmc()` daily TSS uses `COALESCE(SUM(tss_actual), 0)`
- If workouts with missing or 0 TSS are included, CTL/ATL are undercounted. Ideally, it should prefer planned TSS if actual is missing, or explicitly filter `completed` workouts.

### I. `populate_daily_performance()` averages IF and NP across workouts
- `AVG(if_actual)` and `AVG(np_actual)` in the aggregation (~lines 210–220). For multi-workout days, IF and NP should be weighted by duration, not simple averages. This could mislead correlations.

### J. `weekly_summary()` uses `completed_real` but counts `completed` differently
- In the second `weekly_summary()`, `completed_real` filters to workouts with `tss_actual > 0`, but `completed` was already computed earlier from `w['completed']`. Inconsistency can yield misleading completion ratios.

### K. `strava_events()` start time parsing
- `start_str = ev.get("upcoming_occurrences", [None])[0]` (~line 1050). If the API returns an empty list or unexpected format, this silently skips without logging. That is fine, but events could be dropped without notice.

---

## 3) Security concerns

### A. Token storage and file rewriting
- `strava_refresh_token()` rewrites `.env` files in-place (~lines 984–1030). If a write fails, the file can be truncated or corrupted without backup. That is a reliability risk.
- Tokens are stored unencrypted in `~/.openclaw/credentials/`. That may be acceptable locally, but worth noting. Consider file permissions or using OS keyring.

### B. SQL injection
- Most queries use parameterized SQL. That is good. However, there are a few raw string interpolations, but they do not appear to accept user input directly. Example: `pd.read_sql("SELECT ...", conn)` is safe in this context.
- No immediate SQL injection vulnerabilities found.

### C. API key exposure in logs
- `tp_get_token()` logs failures with HTTP status but does not log tokens. Good.
- `whoop_refresh()` logs errors from subprocess output; if `whoop-refresh` prints tokens on stderr, they could be exposed.

---

## 4) Performance issues

### A. Per-row inserts for Whoop and TrainingPeaks
- `sync_whoop()` and `sync_tp()` run `cur.execute(...)` per row. For large date ranges, this is slow.
- Use `psycopg2.extras.execute_values()` for batch inserts or a `COPY` approach.

### B. PMC calculation loops in Python over all dates
- `calc_pmc()` iterates day-by-day from `first_date` to `today` (~lines 520–610). For multi-year history, this is fine but could be optimized by only calculating from the last stored training_load date or using SQL window functions.

### C. Multiple DB connections in one command
- `cmd_correlate()` opens a `conn`, then opens another `conn2` for consistency check (~lines 1230–1260). This can be simplified and is minor but unnecessary overhead.

### D. Frequent API calls without caching
- Weather and Strava calls are made every invocation, without caching. That is expected, but note that Strava is rate limited; `time.sleep(0.5)` is not enough if the club list grows.

---

## 5) Python best practices (error handling, typing, logging)

### A. Missing timeouts for network requests
- Many `requests.get()` calls do not pass a timeout (Whoop, TrainingPeaks, Strava). That can hang the CLI.
- Weather and geocode do use timeouts, but Whoop and TP do not. Add `timeout=10` to all network calls.

### B. `psycopg2.connect()` without context manager
- Many functions open a DB connection and close manually. If an exception occurs, the connection can leak.
- Use `with get_db() as conn, conn.cursor() as cur:` for safe cleanup.

### C. Minimal logging
- Uses `print()` for status. That is fine for CLI, but some error prints discard stack traces. For example, `sync_whoop()` wraps all API calls in a broad `except Exception as e` (~lines 65–71) which hides context.

### D. Type hints are absent
- For complex data flows, type hints for return values and dict schemas would help maintainability.

### E. Global constants vs runtime data
- `FTP = 263` is a constant used in analytics even though FTP history is stored in DB. This is not best practice and leads to incorrect outputs if FTP changes.

---

## 6) Training science logic issues

### A. PMC calculation uses only actual TSS and no planned TSS fallback
- If actual TSS is missing for completed workouts, CTL/ATL will be underreported. Consider: `COALESCE(tss_actual, tss_planned)` or use completed-only filter.

### B. IF calculation in `post_ride()`
- Second `post_ride()` uses `IF = NP / FTP` with a hard-coded FTP (~line 690). This is incorrect when FTP changes or differs from 263. Use current FTP from `ftp_history`.

### C. Workout quality calculation
- `calc_workout_quality()` uses a linear mix of TSS adherence and IF adherence. The formula treats IF deviation as `100 - abs(if_actual - if_planned) / if_planned * 100` (~lines 121–124). If `if_actual` is 1.2 and `if_planned` is 0.7, this yields negative values and then clamps to 0, which may be too punitive. Consider ratio-based penalty and cap.

### D. Daily performance aggregation weights
- `AVG(if_actual)` and `AVG(np_actual)` in `populate_daily_performance()` are not weighted by duration, so a short interval workout can distort daily IF and NP. Duration-weighted averages are more correct.

### E. Weekly summary uses completion metric based on TSS planned
- The second `weekly_summary()` defines `total_workouts` as workouts with planned TSS (`tss_planned > 0`), but uses `completed_real` (tss_actual > 0) to compute completion. If workouts are logged with planned TSS but completed without uploading actual data, the completion percentage will undercount.

---

## Concrete fixes (short list)

1. Remove duplicated definitions (`calc_pmc`, `post_ride`, `ftp_project`, `weekly_summary`) and keep one source of truth.
2. Replace constant `FTP=263` with `_get_current_ftp()` lookup.
3. Add pagination to Whoop (`v2` endpoints support `page` or `next_token`), or loop until cutoff date.
4. Add `timeout=` to all `requests` calls.
5. Use `execute_values()` for batch inserts in `sync_whoop()` and `sync_tp()`.
6. Fix `populate_daily_performance()` to use duration-weighted averages for IF/NP, and sum durations consistently.
7. Normalize PMC input TSS: use `COALESCE(tss_actual, tss_planned)` or filter `completed` workouts.
8. Refactor into modules with data layer + analytics + CLI to improve maintainability.

---

## Notable line references (approx)
- Duplicate `calc_pmc` definitions: ~235–364 and ~470–640
- Duplicate `post_ride` definitions: ~367–456 and ~650–770
- Duplicate `ftp_project` definitions: ~457–523 and ~773–842
- Duplicate `weekly_summary` definitions: ~526–610 and ~845–1006
- `calc_workout_quality` falsy check: ~118–125
- `populate_daily_performance` aggregation: ~200–230
- `sync_whoop` limit cap: ~58–62
- `FTP = 263` global: ~452
- `weekly_summary` load query not filtered by week: ~890
