# Cycling Training Intelligence System

An automated training analytics platform for serious endurance cyclists. Consolidates data from multiple sources into PostgreSQL, runs performance modeling, and generates a comprehensive HTML dashboard with AI-powered coaching assessment.

![Python](https://img.shields.io/badge/python-3.10+-blue)
![PostgreSQL](https://img.shields.io/badge/database-PostgreSQL-336791)
![Chart.js](https://img.shields.io/badge/charts-Chart.js-FF6384)

## What It Does

- **Syncs training data** from TrainingPeaks (workouts, TSS, IF, NP), Whoop (recovery, HRV, sleep), and Strava (power zone distributions, club events)
- **Calculates Performance Management Chart (PMC)** with CTL, ATL, and TSB using exponential weighted averages
- **Projects FTP trajectory** based on actual normalized power trends from high-intensity workouts
- **Correlates recovery and performance** across overlapping Whoop/TrainingPeaks data to quantify how recovery metrics affect workout execution
- **Generates physics-based race plans** with Newton-Raphson speed calculations, segment pacing, nutrition strategy, and taper protocols
- **Produces an HTML dashboard** with 10+ Chart.js visualizations, 84 live data placeholders, and an LLM-powered coaching assessment
- **Deploys to Vercel** with edge middleware authentication, daily date-based archives, and calendar navigation
- **Runs daily via cron** to keep everything fresh with zero manual input

## Data Sources

| Source | What It Provides | Sync Method |
|--------|-----------------|-------------|
| **TrainingPeaks** | Planned/completed workouts, TSS, IF, NP, coach comments, power zones | OAuth token exchange from cookie |
| **Whoop** | Recovery scores, HRV, resting HR, sleep duration/quality, strain | OAuth2 with token refresh |
| **Strava** | Per-activity power zone distributions (real time-in-zone from ride files), club events | OAuth2 |
| **Open-Meteo** | Weather forecasts for ride planning and race-day conditions | Public API (no auth) |

## Dashboard

### Sections
1. **Date Navigation** - Calendar picker and prev/next arrows to browse daily archives
2. **Header KPIs** - Race countdowns, FTP, CTL/ATL/TSB at a glance
3. **This Week** - Completed + upcoming workouts, weekly TSS progress, PMC trend, recovery summary
4. **Training Load** - 20-week TSS bar chart with PMC overlay (CTL, ATL, TSB lines)
5. **Power Zone Distribution** - Donut chart from real Strava power data mapped to coach-defined zones
6. **Recovery Dashboard** - 30-day recovery, HRV, and sleep trends with correlation analysis
7. **Workout Quality** - Quality scoring by recovery bracket, trend over time
8. **FTP Trajectory** - Data-driven projection with training phase background bands (Base/Build/Peak)
9. **Key Insights** - Auto-generated findings from correlation engine (recovery, HRV, sleep, consistency, FTP outlook)
10. **Race Plans** - Collapsible race-specific pacing with physics-based speed calculations, stop strategy, nutrition plan, taper protocol
11. **Coaching Assessment** - LLM-generated narrative analysis grounded in current week's data

### Hosting & Auth
- **Deployed to Vercel** with automatic SSL and CDN edge caching
- **Cookie-based authentication** via Vercel Edge Middleware (HMAC-signed, 30-day persistence)
- **Daily archives** at `/cycling-dashboard/YYYY-MM-DD/` with a manifest-driven date picker
- **Configurable upload backends**: Vercel (default) or WebDAV (legacy)

### Template System
The dashboard is generated from an HTML template with **84 data placeholders** filled from SQL queries. Chart.js data arrays, coaching text, race segments, workout rows, and all KPIs are injected at generation time. The template includes responsive mobile layout with a sticky header.

## Architecture

```
┌─────────────────────────────────────────────────┐
│                 cycling-training CLI             │
├──────────┬──────────┬──────────┬────────────────┤
│ sync-    │ sync-    │ sync-    │ sync-strava-   │
│ whoop    │ tp       │ all      │ zones          │
├──────────┴──────────┴──────────┴────────────────┤
│              PostgreSQL (auri_memory)            │
│  ┌──────────────┐  ┌──────────────────────────┐ │
│  │ whoop_       │  │ training_workouts        │ │
│  │ recovery     │  │ training_load (PMC)      │ │
│  │              │  │ ftp_history              │ │
│  │ daily_       │  │ strava_power_zones       │ │
│  │ performance  │  │ strava_events            │ │
│  │              │  │ training_insights        │ │
│  └──────────────┘  └──────────────────────────┘ │
├─────────────────────────────────────────────────┤
│            Dashboard Generator                   │
│  template.html + 84 placeholders → dashboard.html│
│  Chart.js data arrays generated from SQL queries │
│  LLM coaching assessment via chat completions API│
├─────────────────────────────────────────────────┤
│     Upload: Vercel CLI deploy (or WebDAV)        │
│     + daily archive + manifest.json              │
└─────────────────────────────────────────────────┘
```

## CLI Usage

```bash
cycling-training <command> [options]
```

### Data Sync
| Command | Description |
|---------|-------------|
| `sync-whoop [--days N]` | Sync Whoop recovery/sleep data (paginated) |
| `sync-tp [--days N]` | Sync TrainingPeaks workouts (incl. planned through end of week) |
| `sync-all [--days N]` | Sync all sources + populate daily performance table |
| `sync-strava-zones [--days N]` | Sync power zone distributions from Strava activity data |

### Analysis
| Command | Description |
|---------|-------------|
| `status` | Current training status overview |
| `pmc` | Performance Management Chart (CTL/ATL/TSB) |
| `post-ride [DATE]` | Post-ride analysis with quality scoring |
| `ftp-project` | FTP trajectory projection based on NP trends |
| `weekly-summary [DATE]` | Weekly training summary (Mon-Sun) |
| `correlate` | Recovery-performance correlation analysis |
| `trends` | Multi-week training trend analysis |
| `insights` | Generate data-driven training insights |

### Race Planning
| Command | Description |
|---------|-------------|
| `race-plan` | Race pacing, power targets, and stop strategy |
| `race-weather` | Race-day weather forecast (yr.no API) |
| `taper` | Taper protocol and timeline |
| `race-countdown` | Days-to-race dashboard |

### Utilities
| Command | Description |
|---------|-------------|
| `generate-dashboard [--upload]` | Generate HTML dashboard and optionally deploy |
| `strava-events` | Upcoming Strava club group rides |
| `weather [LOCATION]` | Weather forecast + cycling kit recommendation |

## Configuration

All user-specific settings live in `config.json` (gitignored). Copy `config.example.json` to get started.

Key configuration sections:
- **database** - PostgreSQL connection string
- **credentials** - Paths to `.env` files for each service (Whoop, TP, Strava, Fastmail, Vercel)
- **dashboard** - Athlete/coach names, template paths, upload method (`vercel` or `webdav`), site directory
- **ftp** - Current FTP, target FTP, projection parameters
- **race** - Race dates, course parameters (CdA, Crr, system weight, air density), segment definitions, stop strategy
- **zones** - Power zone boundaries and labels

## Origins & Standalone Use

This tool was originally built for use with [OpenClaw](https://github.com/openclaw/openclaw), an AI assistant platform. Within OpenClaw, the daily sync and dashboard generation run as cron jobs, the coaching assessment uses OpenClaw's LLM gateway, and credentials are managed through OpenClaw's credential store.

**To run standalone (without OpenClaw):**
- All data sync, analytics, and dashboard generation work independently. No OpenClaw dependency for core functionality.
- The **coaching assessment** is generated via an LLM chat completions API. Configure the endpoint and API key in `config.json`, or skip it (the dashboard renders fine without coaching text).
- Credentials are read from simple `.env` files (key=value format). Set paths in `config.json`.
- The `--upload` flag on `generate-dashboard` deploys to Vercel by default. Set `upload_method: "webdav"` in config for legacy WebDAV upload, or skip `--upload` and serve the generated file however you like.

## Setup

### Requirements
- Python 3.10+
- PostgreSQL (any recent version)
- Python packages: `psycopg2` (or `psycopg2-binary`), `requests`
- Optional: Vercel CLI (`npm i -g vercel`) for deployment
- Optional: `openai` Python package (for coaching assessment generation)

### Database
Create the required tables by running the CLI for the first time (tables auto-create) or see the schema in the source.

### Credentials
Store in environment files (paths configurable in `config.json`):
- **Whoop**: `WHOOP_ACCESS_TOKEN`, `WHOOP_REFRESH_TOKEN`
- **TrainingPeaks**: `TP_AUTH_COOKIE`, `TP_USER_ID`
- **Strava**: `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `STRAVA_ACCESS_TOKEN`, `STRAVA_REFRESH_TOKEN`
- **Vercel**: `VERCEL_API_KEY` (for deployment)

### Automation
Set up a daily cron to sync data and regenerate the dashboard:
```bash
# Sync fresh data, recalculate PMC, generate and deploy
cycling-training sync-all --days 3
cycling-training pmc
cycling-training generate-dashboard --upload
```

## Analytics Deep Dive

### PMC (Performance Management Chart)
Uses Banister's impulse-response model with standard decay constants (CTL: 42-day, ATL: 7-day). Supports anchor values from external sources (e.g., TrainingPeaks screenshots) with forward-only calculation to prevent overwriting baselines. Uses `COALESCE(tss_actual, tss_planned)` so planned workouts contribute to load modeling.

### Power Zone Distribution
Maps actual per-second power data from Strava's activity zones API to coach-defined power zones. Strava returns 50W-wide buckets; the mapper proportionally splits time across zone boundaries. This is real time-in-zone from power files, not estimated from workout titles or overall IF.

### Recovery-Performance Correlation
Calculates Pearson correlation between Whoop recovery scores and workout quality metrics across all days with overlapping data. Separately analyzes HRV, sleep duration, and recovery bracket impacts on workout execution.

### Workout Quality Scoring
Composite metric from TSS adherence (actual vs. planned) and IF adherence (actual vs. planned), weighted and clamped to 0-100. Duration-weighted for multi-workout days.

### Race Plan Speed Calculator
Physics-based Newton-Raphson solver accounting for aerodynamic drag (CdA), rolling resistance (Crr), system weight, air density, and gradient. Applies a configurable course penalty factor and drafting benefit. All parameters externalized to `config.json`.

### Coaching Assessment
LLM-powered narrative generated via chat completions API. The data brief includes the full week's training schedule (completed and upcoming workouts with TSS), PMC status, recovery trends, FTP confidence level, and illness annotations. Uses a coaching persona grounded in periodization methodologies. When a coach is configured, frames analysis as supplementary to the coaching relationship.

## Project History

This project evolved through a structured review-driven development process:
- **Phase 1**: Data sync pipeline (TrainingPeaks, Whoop)
- **Phase 2**: PMC calculation, post-ride analysis, FTP projection
- **Phase 3**: Strava integration, weather, GitHub repo
- **Phase 4**: Recovery-training correlation engine
- **Phase 5**: Race planning with physics-based speed calculator, dashboard generation
- **Code review**: Codex (GPT-5.2) audit found and fixed 20 issues
- **UX review**: Screenshot-based UI analysis led to layout restructuring
- **Dashboard hosting**: Migrated from Fastmail WebDAV to Vercel with edge auth and daily archives

See [CODE-REVIEW.md](CODE-REVIEW.md) and [UX-REVIEW.md](UX-REVIEW.md) for the full review artifacts.

## License

MIT
