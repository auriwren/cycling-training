# Cycling Training Intelligence System

An automated training analytics platform for serious endurance cyclists. Consolidates data from multiple sources into PostgreSQL, runs performance modeling, and generates a comprehensive HTML dashboard with AI coaching assessment.

![Python](https://img.shields.io/badge/python-3.10+-blue)
![PostgreSQL](https://img.shields.io/badge/database-PostgreSQL-336791)
![Chart.js](https://img.shields.io/badge/charts-Chart.js-FF6384)

## What It Does

- **Syncs training data** from TrainingPeaks (workouts, TSS, IF, NP), Whoop (recovery, HRV, sleep), and Strava (power zone distributions, club events)
- **Calculates Performance Management Chart (PMC)** with CTL, ATL, and TSB using exponential weighted averages
- **Projects FTP trajectory** based on actual normalized power trends from high-intensity workouts
- **Correlates recovery and performance** across overlapping Whoop/TrainingPeaks data to quantify how recovery metrics affect workout execution
- **Generates race plans** with pacing targets, taper protocols, and race-day projections
- **Produces an HTML dashboard** with 10+ Chart.js visualizations and an AI coaching assessment
- **Runs daily via cron** to keep everything fresh with zero manual input

## Data Sources

| Source | What It Provides | Sync Method |
|--------|-----------------|-------------|
| **TrainingPeaks** | Planned/completed workouts, TSS, IF, NP, coach comments, power zones | OAuth token exchange from cookie |
| **Whoop** | Recovery scores, HRV, resting HR, sleep duration/quality, strain | OAuth2 with token refresh |
| **Strava** | Per-activity power zone distributions (real time-in-zone from ride files), club events | OAuth2 |
| **Open-Meteo** | Weather forecasts for ride planning and race-day conditions | Public API (no auth) |

## Dashboard Sections

1. **Header KPIs** - Race countdowns, FTP, CTL/ATL/TSB at a glance
2. **This Week** - Completed + upcoming workouts, weekly progress, PMC trend, recovery
3. **Training Load** - 20-week TSS bar chart with PMC overlay
4. **Power Zone Distribution** - Donut chart from real Strava power data mapped to coach-defined zones
5. **Recovery Dashboard** - 30-day recovery, HRV, and sleep trends with correlation analysis
6. **Workout Quality** - Quality scoring by recovery bracket, trend over time
7. **FTP Trajectory** - Data-driven projection based on monthly NP trends
8. **Key Insights** - Auto-generated findings from correlation engine
9. **Race Plans** - Collapsible race-specific pacing, stops, fueling, taper (configurable)
10. **Coaching Assessment** - AI-generated narrative grounded in current data

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
│  template.html + 78 placeholders → dashboard.html│
│  Chart.js data arrays generated from SQL queries │
│  Coaching text templated with live numbers       │
├─────────────────────────────────────────────────┤
│         Upload (WebDAV / local file)             │
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
| `generate-dashboard [--upload]` | Generate HTML dashboard from current data |
| `strava-events` | Upcoming Strava club group rides |
| `weather [LOCATION]` | Weather forecast + cycling kit recommendation |

## Setup

### Requirements
- Python 3.10+
- PostgreSQL
- `psycopg2`, `requests`

### Database
Create the required tables by running the CLI for the first time (tables auto-create) or see the schema in the source.

### Credentials
Store in environment files (paths configurable):
- **Whoop**: `WHOOP_ACCESS_TOKEN`, `WHOOP_REFRESH_TOKEN`
- **TrainingPeaks**: `TP_AUTH_COOKIE`, `TP_USER_ID`
- **Strava**: `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `STRAVA_ACCESS_TOKEN`, `STRAVA_REFRESH_TOKEN`

### Configuration
Environment variables for the dashboard generator:
- `CT_DB_CONN` - PostgreSQL connection string (default: `dbname=auri_memory`)
- `CT_PROJECT_DIR` - Project directory path
- `CT_ATHLETE_NAME` - Athlete's full name (for dashboard header)
- `CT_ATHLETE_FIRST` - Athlete's first name (for coaching text)
- `CT_COACH_NAME` - Coach's full name
- `CT_COACH_FIRST` - Coach's first name

### Automation
Set up a daily cron to sync data and regenerate the dashboard:
```bash
# Example: sync at 10 PM, generate dashboard
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

### Coaching Assessment
AI-generated text using a system prompt modeled on the periodization methodologies of Hunter Allen, Joe Friel, and Andrew Coggan. Templated with 20+ live data points (CTL, completion rate, correlation values, FTP trajectory, recovery trends). Refreshed daily with current numbers.

## Project History

This project evolved through a structured review-driven development process:
- **Phase 1**: Data sync pipeline (TrainingPeaks, Whoop)
- **Phase 2**: PMC calculation, post-ride analysis, FTP projection
- **Phase 3**: Strava integration, weather, GitHub repo
- **Phase 4**: Recovery-training correlation engine
- **Phase 5**: Race planning, taper protocol, dashboard
- **Code review**: Codex (GPT-5.2) audit found and fixed 20 issues
- **UX review**: Screenshot-based UI analysis led to layout restructuring

See [CODE-REVIEW.md](CODE-REVIEW.md) and [UX-REVIEW.md](UX-REVIEW.md) for the full review artifacts.

## License

MIT
