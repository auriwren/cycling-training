# cycling-training

AI-assisted cycling training data pipeline and analysis. Syncs data from Whoop, TrainingPeaks, and Strava into PostgreSQL for unified training insights.

## Setup

**Requirements:**
- Python 3.10+
- `psycopg2`, `requests`
- PostgreSQL database `auri_memory`

**Credentials** (in `~/.openclaw/credentials/`):
- `whoop.env` — WHOOP_ACCESS_TOKEN, WHOOP_REFRESH_TOKEN
- `trainingpeaks.env` — TP_AUTH_COOKIE, TP_USER_ID
- `strava.env` — STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_ACCESS_TOKEN

## CLI Usage

```
cycling-training <command> [options]
```

| Command | Description |
|---------|-------------|
| `sync-whoop [--days N]` | Sync Whoop recovery/sleep/strain data |
| `sync-tp [--days N]` | Sync TrainingPeaks workouts |
| `sync-all [--days N]` | Sync all sources + populate daily performance |
| `status` | Current training status dashboard |
| `pmc` | Performance Management Chart (CTL/ATL/TSB) |
| `post-ride [DATE]` | Post-ride analysis for a date |
| `ftp-project` | FTP trajectory projection toward 300W |
| `weekly-summary [DATE]` | Weekly training summary (Mon-Sun) |
| `strava-events` | Upcoming Strava club group events |
| `weather [LOCATION]` | Weather forecast + cycling kit recommendation |

## Database Tables

- `whoop_recovery` — Daily recovery, sleep, strain from Whoop
- `training_workouts` — Workouts from TrainingPeaks
- `daily_performance` — Joined Whoop + TP daily view
- `training_load` — CTL/ATL/TSB time series
- `ftp_history` — FTP test results
- `strava_events` — Cached Strava club events
