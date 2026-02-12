#!/usr/bin/env python3
"""
Cycling Training CLI - Phase 1
Syncs Whoop recovery and TrainingPeaks workout data into PostgreSQL.

Usage:
    cycling-training sync-whoop [--days N]
    cycling-training sync-tp [--days N]
    cycling-training sync-all [--days N]
    cycling-training status
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import warnings
warnings.filterwarnings("ignore", message=".*pandas only supports SQLAlchemy.*")

import psycopg2
import psycopg2.extras
import requests

DB_CONN = "dbname=auri_memory user=openclaw"
WHOOP_ENV = Path.home() / ".openclaw/credentials/whoop.env"
TP_ENV = Path.home() / ".openclaw/credentials/trainingpeaks.env"
STRAVA_ENV = Path.home() / ".openclaw/credentials/strava.env"
TP_TOKEN_CACHE = Path.home() / ".openclaw/cache/tp-token.json"

# Strava clubs to check for events (skip Zwift, Road Cycling Academy)
STRAVA_CLUBS = {
    1209: "New York Cycle Club (NYCC)",
    228861: "Redbeard Bikes",
    770232: "Bistro Cycling Club",
    840835: "Century Plus Crew",
    121422: "Rapha New York",
    1489429: "The Rogue Group Cycling Club",
}


def get_db():
    return psycopg2.connect(DB_CONN)


def load_env(path):
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"')
    return env


# ── Whoop ──────────────────────────────────────────────────────────

def whoop_refresh():
    """Run whoop-refresh to get fresh tokens."""
    r = subprocess.run(["whoop-refresh"], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"⚠️  whoop-refresh failed: {r.stderr.strip()}")
        return False
    return True


def whoop_api(endpoint, token):
    """Call Whoop API, retry once on 401."""
    url = f"https://api.prod.whoop.com/developer{endpoint}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code == 401:
        if whoop_refresh():
            token = load_env(WHOOP_ENV).get("WHOOP_ACCESS_TOKEN", "")
            resp = requests.get(url, headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    return resp.json()


def sync_whoop(days=7):
    """Sync Whoop recovery/sleep/strain data."""
    print(f"🔄 Syncing Whoop data (last {days} days)...")
    whoop_refresh()
    env = load_env(WHOOP_ENV)
    token = env.get("WHOOP_ACCESS_TOKEN", "")

    limit = min(days + 1, 25)

    # Fetch all three datasets using v2 pagination
    try:
        recovery_data = whoop_api(f"/v2/recovery?limit={limit}", token)
        sleep_data = whoop_api(f"/v2/activity/sleep?limit={limit}", token)
        cycle_data = whoop_api(f"/v2/cycle?limit={limit}", token)
    except Exception as e:
        print(f"❌ Whoop API error: {e}")
        return False

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    # Index sleep by sleep_id and cycle by cycle_id
    sleep_by_id = {}
    for s in sleep_data.get("records", []):
        sid = s.get("id")
        if sid:
            sleep_by_id[sid] = s

    cycle_by_id = {}
    for c in cycle_data.get("records", []):
        cid = c.get("id")
        if cid:
            cycle_by_id[cid] = c

    conn = get_db()
    cur = conn.cursor()
    count = 0

    for rec in recovery_data.get("records", []):
        score = rec.get("score", {})
        created = rec.get("created_at", "")[:10]
        if not created or created < cutoff:
            continue

        cycle_id = rec.get("cycle_id")
        sleep_id = rec.get("sleep_id")
        sleep = sleep_by_id.get(sleep_id, {})
        cycle = cycle_by_id.get(cycle_id, {})
        sleep_score_data = sleep.get("score", {})
        cycle_score = cycle.get("score", {})
        stage = sleep_score_data.get("stage_summary", {})

        row = {
            "date": created,
            "recovery_score": score.get("recovery_score"),
            "hrv_rmssd": score.get("hrv_rmssd_milli"),
            "resting_hr": score.get("resting_heart_rate"),
            "skin_temp": score.get("skin_temp_celsius"),
            "spo2": score.get("spo2_percentage"),
            "respiratory_rate": score.get("respiratory_rate"),
            "sleep_score": sleep_score_data.get("sleep_performance_percentage"),
            "sleep_duration_min": int(stage.get("total_in_bed_time_milli", 0) / 60000) if stage.get("total_in_bed_time_milli") else None,
            "sleep_efficiency": sleep_score_data.get("sleep_efficiency_percentage"),
            "rem_min": int(stage.get("total_rem_sleep_time_milli", 0) / 60000) if stage.get("total_rem_sleep_time_milli") else None,
            "sws_min": int(stage.get("total_slow_wave_sleep_time_milli", 0) / 60000) if stage.get("total_slow_wave_sleep_time_milli") else None,
            "light_min": int(stage.get("total_light_sleep_time_milli", 0) / 60000) if stage.get("total_light_sleep_time_milli") else None,
            "awake_min": int(stage.get("total_awake_time_milli", 0) / 60000) if stage.get("total_awake_time_milli") else None,
            "strain_score": cycle_score.get("strain"),
        }

        cur.execute("""
            INSERT INTO whoop_recovery (date, recovery_score, hrv_rmssd, resting_hr, skin_temp,
                spo2, respiratory_rate, sleep_score, sleep_duration_min, sleep_efficiency,
                rem_min, sws_min, light_min, awake_min, strain_score)
            VALUES (%(date)s, %(recovery_score)s, %(hrv_rmssd)s, %(resting_hr)s, %(skin_temp)s,
                %(spo2)s, %(respiratory_rate)s, %(sleep_score)s, %(sleep_duration_min)s, %(sleep_efficiency)s,
                %(rem_min)s, %(sws_min)s, %(light_min)s, %(awake_min)s, %(strain_score)s)
            ON CONFLICT (date) DO UPDATE SET
                recovery_score = EXCLUDED.recovery_score, hrv_rmssd = EXCLUDED.hrv_rmssd,
                resting_hr = EXCLUDED.resting_hr, skin_temp = EXCLUDED.skin_temp,
                spo2 = EXCLUDED.spo2, respiratory_rate = EXCLUDED.respiratory_rate,
                sleep_score = EXCLUDED.sleep_score, sleep_duration_min = EXCLUDED.sleep_duration_min,
                sleep_efficiency = EXCLUDED.sleep_efficiency, rem_min = EXCLUDED.rem_min,
                sws_min = EXCLUDED.sws_min, light_min = EXCLUDED.light_min,
                awake_min = EXCLUDED.awake_min, strain_score = EXCLUDED.strain_score
        """, row)
        count += 1

    conn.commit()
    cur.close()
    conn.close()
    print(f"✅ Whoop: upserted {count} days of recovery data")
    return True


# ── TrainingPeaks ──────────────────────────────────────────────────

def tp_get_token():
    """Get TP OAuth token using cookie -> token exchange pattern."""
    # Check cache
    if TP_TOKEN_CACHE.exists():
        try:
            cache = json.loads(TP_TOKEN_CACHE.read_text())
            if cache.get("expires_at", 0) > time.time() + 300:
                return cache["access_token"]
        except (json.JSONDecodeError, KeyError):
            pass

    env = load_env(TP_ENV)
    cookie = env.get("TP_AUTH_COOKIE", "")
    if not cookie:
        print("❌ No TP_AUTH_COOKIE found")
        return None

    resp = requests.get(
        "https://tpapi.trainingpeaks.com/users/v3/token",
        headers={"Cookie": f"Production_tpAuth={cookie}", "Accept": "application/json"},
    )
    if resp.status_code != 200:
        print(f"❌ TP token exchange failed: HTTP {resp.status_code}")
        return None

    data = resp.json()
    if data.get("success") is False:
        print("❌ TP cookie expired")
        return None

    token_obj = data.get("token", {})
    access_token = token_obj.get("access_token") if isinstance(token_obj, dict) else data.get("access_token")
    if not access_token:
        print("❌ Could not extract TP access token")
        return None

    # Cache it
    TP_TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    expires_in = token_obj.get("expires_in", 3600) if isinstance(token_obj, dict) else 3600
    TP_TOKEN_CACHE.write_text(json.dumps({
        "access_token": access_token,
        "expires_at": time.time() + expires_in - 300,
    }))
    return access_token


def calc_workout_quality(tss_planned, tss_actual, if_planned, if_actual):
    """Calculate workout quality score (0-100)."""
    if not all([tss_planned, tss_actual, if_planned, if_actual]):
        return None
    if tss_planned == 0 or if_planned == 0:
        return None
    tss_adherence = min(tss_actual / tss_planned, 1.2) / 1.2 * 100
    if_adherence = 100 - abs(if_actual - if_planned) / if_planned * 100
    return max(0, min(100, tss_adherence * 0.5 + if_adherence * 0.5))


def sync_tp(days=7):
    """Sync TrainingPeaks workout data."""
    print(f"🔄 Syncing TrainingPeaks data (last {days} days)...")
    token = tp_get_token()
    if not token:
        return False

    env = load_env(TP_ENV)
    user_id = env.get("TP_USER_ID", "2100281")

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    url = f"https://tpapi.trainingpeaks.com/fitness/v6/athletes/{user_id}/workouts/{start_date}/{end_date}"

    resp = requests.get(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    if resp.status_code == 401:
        # Clear cache and retry
        TP_TOKEN_CACHE.unlink(missing_ok=True)
        token = tp_get_token()
        if not token:
            return False
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})

    if resp.status_code != 200:
        print(f"❌ TP API error: HTTP {resp.status_code}")
        return False

    workouts = resp.json()
    if isinstance(workouts, dict) and "error" in workouts:
        print(f"❌ TP API error: {workouts['error']}")
        return False

    conn = get_db()
    cur = conn.cursor()
    count = 0

    for w in workouts:
        ext_id = str(w.get("workoutId", w.get("id", "")))
        date = w.get("workoutDay", "")[:10]
        if not date:
            continue

        tss_planned = w.get("tssPlanned")
        if_planned = w.get("ifPlanned")
        tss_actual = w.get("tssActual")
        if_actual = w.get("if") or w.get("ifActual")
        duration_planned = None
        if w.get("totalTimePlanned"):
            duration_planned = int(w["totalTimePlanned"] * 60)

        completed = w.get("completed", False) or (tss_actual is not None and tss_actual > 0)
        quality = calc_workout_quality(tss_planned, tss_actual, if_planned, if_actual)

        row = {
            "date": date,
            "source": "trainingpeaks",
            "external_id": ext_id,
            "title": w.get("title", ""),
            "workout_type": w.get("workoutTypeValueId", w.get("workoutType", "")),
            "tss_planned": tss_planned,
            "if_planned": if_planned,
            "duration_planned_min": duration_planned,
            "tss_actual": tss_actual,
            "if_actual": if_actual,
            "np_actual": w.get("normalizedPowerActual"),
            "avg_power": w.get("averagePowerActual"),
            "max_power": w.get("maxPowerActual"),
            "avg_hr": w.get("heartRateAverage"),
            "max_hr": w.get("heartRateMaximum"),
            "duration_actual_min": int(w["totalTimeActual"] * 60) if w.get("totalTimeActual") else None,
            "efficiency_factor": w.get("efficiencyFactor"),
            "workout_quality": quality,
            "completed": completed,
            "notes": w.get("description"),
        }

        cur.execute("""
            INSERT INTO training_workouts (date, source, external_id, title, workout_type,
                tss_planned, if_planned, duration_planned_min, tss_actual, if_actual,
                np_actual, avg_power, max_power, avg_hr, max_hr, duration_actual_min,
                efficiency_factor, workout_quality, completed, notes)
            VALUES (%(date)s, %(source)s, %(external_id)s, %(title)s, %(workout_type)s,
                %(tss_planned)s, %(if_planned)s, %(duration_planned_min)s, %(tss_actual)s, %(if_actual)s,
                %(np_actual)s, %(avg_power)s, %(max_power)s, %(avg_hr)s, %(max_hr)s, %(duration_actual_min)s,
                %(efficiency_factor)s, %(workout_quality)s, %(completed)s, %(notes)s)
            ON CONFLICT ON CONSTRAINT training_workouts_date_ext_id
                DO UPDATE SET title = EXCLUDED.title, tss_planned = EXCLUDED.tss_planned,
                    if_planned = EXCLUDED.if_planned, tss_actual = EXCLUDED.tss_actual,
                    if_actual = EXCLUDED.if_actual, np_actual = EXCLUDED.np_actual,
                    avg_power = EXCLUDED.avg_power, max_power = EXCLUDED.max_power,
                    avg_hr = EXCLUDED.avg_hr, max_hr = EXCLUDED.max_hr,
                    duration_actual_min = EXCLUDED.duration_actual_min,
                    workout_quality = EXCLUDED.workout_quality, completed = EXCLUDED.completed
        """, row)
        count += 1

    conn.commit()
    cur.close()
    conn.close()
    print(f"✅ TrainingPeaks: upserted {count} workouts")
    return True


# ── Daily Performance ─────────────────────────────────────────────

def populate_daily_performance(days=7):
    """Join Whoop + TP data into daily_performance."""
    conn = get_db()
    cur = conn.cursor()
    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    cur.execute("""
        INSERT INTO daily_performance (date, recovery_score, hrv_rmssd, sleep_hours, sleep_score,
            strain, resting_hr, tss_planned, tss_actual, if_actual, np_actual, duration_min,
            workout_type, workout_quality)
        SELECT
            COALESCE(w.date, t.date) as date,
            w.recovery_score, w.hrv_rmssd,
            CASE WHEN w.sleep_duration_min IS NOT NULL THEN w.sleep_duration_min / 60.0 END,
            w.sleep_score, w.strain_score, w.resting_hr,
            t.tss_planned, t.tss_actual, t.if_actual, t.np_actual,
            t.duration_actual_min, t.workout_type, t.workout_quality
        FROM whoop_recovery w
        FULL OUTER JOIN (
            SELECT date, SUM(tss_planned) as tss_planned, SUM(tss_actual) as tss_actual,
                AVG(if_actual) as if_actual, AVG(np_actual)::int as np_actual,
                SUM(duration_actual_min) as duration_actual_min,
                MAX(workout_type) as workout_type, AVG(workout_quality) as workout_quality
            FROM training_workouts WHERE date >= %s GROUP BY date
        ) t ON w.date = t.date
        WHERE COALESCE(w.date, t.date) >= %s
        ON CONFLICT (date) DO UPDATE SET
            recovery_score = EXCLUDED.recovery_score, hrv_rmssd = EXCLUDED.hrv_rmssd,
            sleep_hours = EXCLUDED.sleep_hours, sleep_score = EXCLUDED.sleep_score,
            strain = EXCLUDED.strain, resting_hr = EXCLUDED.resting_hr,
            tss_planned = EXCLUDED.tss_planned, tss_actual = EXCLUDED.tss_actual,
            if_actual = EXCLUDED.if_actual, np_actual = EXCLUDED.np_actual,
            duration_min = EXCLUDED.duration_min, workout_type = EXCLUDED.workout_type,
            workout_quality = EXCLUDED.workout_quality
    """, (start, start))

    conn.commit()
    rows = cur.rowcount
    cur.close()
    conn.close()
    print(f"✅ Daily performance: updated {rows} days")


# ── Sync All ──────────────────────────────────────────────────────

def sync_all(days=7):
    sync_whoop(days)
    sync_tp(days)
    populate_daily_performance(days)


# ── PMC (Performance Management Chart) ────────────────────────────

def calc_pmc():
    """Calculate CTL/ATL/TSB from all historical TSS data."""
    conn = get_db()
    cur = conn.cursor()

    # Get all daily TSS (sum per day for multi-workout days)
    cur.execute("""
        SELECT date, COALESCE(SUM(tss_actual), 0) as daily_tss
        FROM training_workouts
        GROUP BY date ORDER BY date
    """)
    rows = cur.fetchall()
    if not rows:
        print("No workout data found.")
        conn.close()
        return

    # Fill gaps between first and last date
    first_date = rows[0][0]
    last_date = rows[-1][0]
    tss_by_date = {r[0]: float(r[1]) for r in rows}

    # Check for a manually anchored baseline (e.g. from TrainingPeaks screenshot)
    cur.execute("SELECT ctl, atl FROM training_load WHERE ctl IS NOT NULL ORDER BY date DESC LIMIT 1")
    anchor = cur.fetchone()
    # Use anchor if our calculated values would be far off (first run after seeding)
    ctl, atl = 0.0, 0.0
    ctl_tau, atl_tau = 42, 7
    
    # If we have an anchor and this is a recalc, scale our starting point
    # so that the most recent day lands close to the anchor
    # We do a two-pass approach: first pass to see where we'd end up, 
    # then adjust starting point
    if anchor and anchor[0] and anchor[0] > 10:
        # Do a dry run to find final CTL/ATL
        dry_ctl, dry_atl = 0.0, 0.0
        d = first_date
        while d <= last_date:
            tss = tss_by_date.get(d, 0.0)
            dry_ctl = dry_ctl + (tss - dry_ctl) / ctl_tau
            dry_atl = dry_atl + (tss - dry_atl) / atl_tau
            d += timedelta(days=1)
        # Scale starting CTL/ATL so final values match anchor
        if dry_ctl > 0:
            ctl = max(0, float(anchor[0]) - dry_ctl)
            atl = max(0, float(anchor[1]) - dry_atl) if anchor[1] else 0.0

    results = []
    d = first_date
    while d <= last_date:
        tss = tss_by_date.get(d, 0.0)
        ctl = ctl + (tss - ctl) / ctl_tau
        atl = atl + (tss - atl) / atl_tau
        tsb = ctl - atl
        results.append((d, tss, round(ctl, 2), round(atl, 2), round(tsb, 2)))
        d += timedelta(days=1)

    # Upsert into training_load
    for r in results:
        cur.execute("""
            INSERT INTO training_load (date, daily_tss, ctl, atl, tsb)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (date) DO UPDATE SET
                daily_tss = EXCLUDED.daily_tss, ctl = EXCLUDED.ctl,
                atl = EXCLUDED.atl, tsb = EXCLUDED.tsb
        """, r)
    conn.commit()

    # Display
    latest = results[-1]
    prev = results[-2] if len(results) > 1 else latest
    ctl_delta = latest[2] - prev[2]
    atl_delta = latest[3] - prev[3]
    tsb_delta = latest[4] - prev[4]

    def trend(v):
        return "↑" if v > 0.1 else ("↓" if v < -0.1 else "→")

    print("═" * 50)
    print("    📈 PERFORMANCE MANAGEMENT CHART")
    print("═" * 50)
    print(f"\n  Date:    {latest[0]}")
    print(f"  CTL:     {latest[2]:6.1f}  {trend(ctl_delta)} ({ctl_delta:+.1f})")
    print(f"  ATL:     {latest[3]:6.1f}  {trend(atl_delta)} ({atl_delta:+.1f})")
    print(f"  TSB:     {latest[4]:6.1f}  {trend(tsb_delta)} ({tsb_delta:+.1f})")
    print(f"\n  Last 7 days:")
    for r in results[-7:]:
        print(f"    {r[0]}  TSS:{r[1]:5.0f}  CTL:{r[2]:5.1f}  ATL:{r[3]:5.1f}  TSB:{r[4]:+5.1f}")
    print("═" * 50)

    cur.close()
    conn.close()


# ── Post-Ride Analysis ────────────────────────────────────────────

def post_ride(target_date=None):
    """Show post-ride analysis for a given date."""
    if target_date is None:
        target_date = date.today().isoformat()
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    cur.execute("""
        SELECT * FROM training_workouts
        WHERE date = %s AND completed = true
        ORDER BY tss_actual DESC NULLS LAST
    """, (target_date,))
    workouts = cur.fetchall()

    if not workouts:
        print(f"No completed workouts found for {target_date}")
        cur.close()
        conn.close()
        return

    for w in workouts:
        title = w["title"] or "Untitled"
        dur = w["duration_actual_min"]
        dur_str = f"{dur // 60}:{dur % 60:02d}:00" if dur else "N/A"
        np_val = w["np_actual"]
        avg_pwr = w["avg_power"]
        avg_hr = w["avg_hr"]

        vi = round(float(np_val) / float(avg_pwr), 2) if np_val and avg_pwr and float(avg_pwr) > 0 else None
        ef = round(float(np_val) / float(avg_hr), 2) if np_val and avg_hr and float(avg_hr) > 0 else None
        quality = w["workout_quality"]
        quality_str = f"{quality:.0f}%" if quality else "N/A"

        tss_p = w["tss_planned"]
        tss_a = w["tss_actual"]
        if_p = w["if_planned"]
        if_a = w["if_actual"]

        print(f"🚴 **Post-Ride: {title}**")
        print(f"Duration: {dur_str} | TSS: {tss_p or 'N/A'}→{tss_a or 'N/A'} | IF: {if_p or 'N/A'}→{if_a or 'N/A'}")
        print(f"NP: {np_val or 'N/A'}W | Avg Power: {avg_pwr or 'N/A'}W | VI: {vi or 'N/A'}")
        print(f"Avg HR: {avg_hr or 'N/A'} | EF: {ef or 'N/A'}")
        print(f"Quality Score: {quality_str}")
        if tss_p and tss_a:
            pct = (float(tss_a) / float(tss_p) - 1) * 100
            emoji = "✅" if abs(pct) < 15 else ("⚠️" if abs(pct) < 30 else "❌")
            print(f"TSS Adherence: {pct:+.0f}% {emoji}")
        print()

    cur.close()
    conn.close()


# ── FTP Projection ────────────────────────────────────────────────

def ftp_project():
    """Project FTP trajectory toward 300W target."""
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT test_date, ftp_watts FROM ftp_history ORDER BY test_date")
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if len(rows) < 1:
        print("No FTP history found.")
        return

    current_ftp = rows[-1][1]
    current_date = rows[-1][0]
    target_w = 300
    target_date = date(2026, 12, 31)
    vattern_date = date(2026, 6, 12)

    weeks_to_target = max(1, (target_date - date.today()).days / 7)
    weeks_to_vattern = max(1, (vattern_date - date.today()).days / 7)
    weekly_gain = (target_w - current_ftp) / weeks_to_target

    # Linear projection
    if len(rows) >= 2:
        base = rows[0][0]
        days = [(r[0] - base).days for r in rows]
        ftps = [r[1] for r in rows]
        # Simple linear regression
        n = len(days)
        sx = sum(days)
        sy = sum(ftps)
        sxx = sum(d * d for d in days)
        sxy = sum(d * f for d, f in zip(days, ftps))
        slope = (n * sxy - sx * sy) / (n * sxx - sx * sx) if (n * sxx - sx * sx) != 0 else 0
        intercept = (sy - slope * sx) / n

        vattern_day = (vattern_date - base).days
        target_day = (target_date - base).days
        vattern_proj = round(slope * vattern_day + intercept)
        dec_proj = round(slope * target_day + intercept)
    else:
        # Single data point - use required rate
        vattern_proj = round(current_ftp + weekly_gain * weeks_to_vattern)
        dec_proj = target_w

    print("═" * 50)
    print("    ⚡ FTP TRAJECTORY")
    print("═" * 50)
    print(f"\n  Current FTP:     {current_ftp}W (as of {current_date})")
    print(f"  Target:          {target_w}W by {target_date}")
    print(f"  Gap:             {target_w - current_ftp}W")
    print(f"\n  Weeks to target: {weeks_to_target:.0f}")
    print(f"  Required gain:   {weekly_gain:.2f} W/week")
    print(f"\n  Projected at Vatternrundan ({vattern_date}): ~{vattern_proj}W")
    print(f"  Projected at Dec 31:                       ~{dec_proj}W")

    on_track = dec_proj >= target_w
    print(f"\n  Status: {'✅ On track' if on_track else '⚠️ Behind pace'}")
    print("═" * 50)


# ── Weekly Summary ────────────────────────────────────────────────

def weekly_summary(ref_date=None):
    """Generate weekly training summary (Mon-Sun)."""
    if ref_date is None:
        ref_date = date.today().isoformat()
    ref = datetime.strptime(ref_date, "%Y-%m-%d").date()
    # Monday of that week
    week_start = ref - timedelta(days=ref.weekday())
    week_end = week_start + timedelta(days=6)

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # Workouts
    cur.execute("""
        SELECT * FROM training_workouts
        WHERE date BETWEEN %s AND %s ORDER BY date
    """, (week_start, week_end))
    workouts = cur.fetchall()

    tss_planned = sum(float(w["tss_planned"] or 0) for w in workouts)
    tss_actual = sum(float(w["tss_actual"] or 0) for w in workouts)
    total_min = sum(int(w["duration_actual_min"] or 0) for w in workouts)
    completed = sum(1 for w in workouts if w["completed"])
    total = len(workouts)
    hours = total_min / 60

    # Whoop averages
    cur.execute("""
        SELECT AVG(recovery_score) as avg_rec, AVG(hrv_rmssd) as avg_hrv,
               AVG(sleep_duration_min) as avg_sleep, AVG(sleep_score) as avg_sleep_score
        FROM whoop_recovery WHERE date BETWEEN %s AND %s
    """, (week_start, week_end))
    whoop = cur.fetchone()

    # PMC (latest in week)
    cur.execute("""
        SELECT * FROM training_load WHERE date BETWEEN %s AND %s
        ORDER BY date DESC LIMIT 1
    """, (week_start, week_end))
    pmc = cur.fetchone()

    # FTP
    cur.execute("SELECT ftp_watts, test_date FROM ftp_history ORDER BY test_date DESC LIMIT 1")
    ftp_row = cur.fetchone()

    cur.close()
    conn.close()

    tss_pct = ((tss_actual / tss_planned - 1) * 100) if tss_planned > 0 else 0
    tss_emoji = "✅" if abs(tss_pct) < 15 else "⚠️"

    avg_rec = float(whoop["avg_rec"]) if whoop and whoop["avg_rec"] else 0
    avg_hrv = float(whoop["avg_hrv"]) if whoop and whoop["avg_hrv"] else 0
    avg_sleep_min = float(whoop["avg_sleep"]) if whoop and whoop["avg_sleep"] else 0
    avg_sleep_hrs = avg_sleep_min / 60

    print(f"📊 **Week of {week_start.strftime('%b %d')} - {week_end.strftime('%b %d, %Y')}**")
    print()
    print("**TRAINING LOAD**")
    print(f"  TSS Planned: {tss_planned:.0f} | Actual: {tss_actual:.0f} ({tss_pct:+.0f}%) {tss_emoji}")
    print(f"  Hours: {hours:.1f} | Workouts: {completed}/{total} completed")
    print()
    if pmc:
        ctl = float(pmc["ctl"] or 0)
        atl = float(pmc["atl"] or 0)
        tsb = float(pmc["tsb"] or 0)
        print("**FITNESS / FATIGUE / FORM**")
        print(f"  CTL: {ctl:.1f} | ATL: {atl:.1f} | TSB: {tsb:+.1f}")
        print()
    print("**RECOVERY (Whoop avg)**")
    rec_emoji = "🟢" if avg_rec >= 67 else ("🟡" if avg_rec >= 34 else "🔴")
    print(f"  Recovery: {avg_rec:.0f}% {rec_emoji} | HRV: {avg_hrv:.0f}ms")
    print(f"  Sleep: {avg_sleep_hrs:.1f} hrs avg")
    print()
    if ftp_row:
        ftp_w = ftp_row["ftp_watts"]
        gap = 300 - ftp_w
        weeks_left = max(1, (date(2026, 12, 31) - date.today()).days / 7)
        print("**FTP TRAJECTORY**")
        print(f"  Current: {ftp_w}W → Target: 300W by end of 2026")
        print(f"  Gap: {gap}W | Need: {gap/weeks_left:.2f} W/week")
    print()


# ── Status ────────────────────────────────────────────────────────

def show_status():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    print("═" * 50)
    print("    🚴 CYCLING TRAINING STATUS")
    print("═" * 50)

    # Latest Whoop recovery
    cur.execute("SELECT * FROM whoop_recovery ORDER BY date DESC LIMIT 1")
    row = cur.fetchone()
    if row:
        score = row["recovery_score"]
        emoji = "🟢" if score and score >= 67 else ("🟡" if score and score >= 34 else "🔴")
        print(f"\n{emoji} Recovery ({row['date']}): {score}%")
        print(f"   HRV: {row['hrv_rmssd']}ms | RHR: {row['resting_hr']}bpm | Strain: {row['strain_score']}")
        if row["sleep_score"]:
            print(f"   Sleep: {row['sleep_score']}% ({row['sleep_duration_min']}min)")
    else:
        print("\n⚪ No Whoop data yet")

    # Today's workouts
    today = datetime.now().strftime("%Y-%m-%d")
    cur.execute("SELECT * FROM training_workouts WHERE date = %s", (today,))
    workouts = cur.fetchall()
    if workouts:
        print(f"\n📋 Today's workouts:")
        for w in workouts:
            status = "✅" if w["completed"] else "📌"
            tss = f"TSS {w['tss_planned']}" if w["tss_planned"] else ""
            print(f"   {status} {w['title']} {tss}")
    else:
        print(f"\n📋 No workouts scheduled for today")

    # Current FTP
    cur.execute("SELECT * FROM ftp_history ORDER BY test_date DESC LIMIT 1")
    ftp = cur.fetchone()
    if ftp:
        print(f"\n⚡ FTP: {ftp['ftp_watts']}W (as of {ftp['test_date']}, {ftp['confidence']})")

    # CTL/ATL/TSB
    cur.execute("SELECT * FROM training_load ORDER BY date DESC LIMIT 1")
    load = cur.fetchone()
    if load:
        tsb = float(load['tsb'])
        trend = "Fresh ✅" if tsb > 0 else ("Recovering" if tsb > -10 else "Loading 💪")
        print(f"\n📊 Training Load ({load['date']}):")
        print(f"   CTL: {load['ctl']} | ATL: {load['atl']} | TSB: {load['tsb']} — {trend}")
    else:
        print(f"\n📊 No training load data yet — run `cycling-training pmc` to calculate")

    # Latest workout quality
    cur.execute("""
        SELECT date, title, workout_quality FROM training_workouts
        WHERE workout_quality IS NOT NULL ORDER BY date DESC LIMIT 1
    """)
    wq = cur.fetchone()
    if wq:
        q = float(wq['workout_quality'])
        q_emoji = "🟢" if q >= 80 else ("🟡" if q >= 60 else "🔴")
        print(f"\n🏋️ Last Workout Quality: {q:.0f}/100 {q_emoji} ({wq['title']}, {wq['date']})")

    # Top insight
    insight = get_top_insight()
    if insight:
        print(f"\n💡 Latest insight: {insight[1][:120]}...")

    print("\n" + "═" * 50)
    cur.close()
    conn.close()


# ── PMC (CTL/ATL/TSB) ─────────────────────────────────────────────

FTP = 263

def calc_pmc():
    """Calculate Performance Management Chart: CTL (42d), ATL (7d), TSB.
    
    Uses anchor-forward approach: finds the latest manually-set anchor point
    in training_load (seeded from TrainingPeaks screenshot), then only calculates
    forward from that anchor using new TSS data. Never overwrites the anchor or
    days before it.
    """
    conn = get_db()
    cur = conn.cursor()

    # Find the anchor: the latest row in training_load that was manually set or previously calculated
    cur.execute("SELECT date, ctl, atl FROM training_load ORDER BY date DESC LIMIT 1")
    anchor = cur.fetchone()

    # Get all daily TSS from training_workouts
    cur.execute("""
        SELECT date, COALESCE(SUM(tss_actual), 0) as tss
        FROM training_workouts
        GROUP BY date ORDER BY date
    """)
    rows = cur.fetchall()
    if not rows:
        print("❌ No workout data found")
        cur.close(); conn.close()
        return

    today = date.today()
    tss_by_date = {r[0]: float(r[1]) for r in rows}

    if anchor and anchor[0] and float(anchor[1]) > 10:
        # Start from anchor and calculate forward for any new days
        anchor_date, anchor_ctl, anchor_atl = anchor[0], float(anchor[1]), float(anchor[2])
        ctl, atl = anchor_ctl, anchor_atl
        start = anchor_date + timedelta(days=1)
        
        if start > today:
            # No new days to calculate, just display
            # Pull last 7 days for display
            cur.execute("SELECT date, daily_tss, ctl, atl, tsb FROM training_load ORDER BY date DESC LIMIT 7")
            display_rows = list(reversed(cur.fetchall()))
        else:
            # Calculate forward from anchor
            results = []
            d = start
            while d <= today:
                tss = tss_by_date.get(d, 0.0)
                ctl = ctl + (tss - ctl) / 42.0
                atl = atl + (tss - atl) / 7.0
                tsb = ctl - atl
                results.append((d, tss, round(ctl, 2), round(atl, 2), round(tsb, 2)))
                d += timedelta(days=1)

            if results:
                psycopg2.extras.execute_values(cur, """
                    INSERT INTO training_load (date, daily_tss, ctl, atl, tsb)
                    VALUES %s
                    ON CONFLICT (date) DO UPDATE SET
                        daily_tss = EXCLUDED.daily_tss, ctl = EXCLUDED.ctl,
                        atl = EXCLUDED.atl, tsb = EXCLUDED.tsb
                """, results)
                conn.commit()

            # Pull last 7 days for display (including anchor and before)
            cur.execute("SELECT date, daily_tss, ctl, atl, tsb FROM training_load ORDER BY date DESC LIMIT 7")
            display_rows = list(reversed(cur.fetchall()))
    else:
        # No anchor: full calculation from scratch
        start = rows[0][0]
        ctl, atl = 0.0, 0.0
        results = []
        d = start
        while d <= today:
            tss = tss_by_date.get(d, 0.0)
            ctl = ctl + (tss - ctl) / 42.0
            atl = atl + (tss - atl) / 7.0
            tsb = ctl - atl
            results.append((d, tss, round(ctl, 2), round(atl, 2), round(tsb, 2)))
            d += timedelta(days=1)

        psycopg2.extras.execute_values(cur, """
            INSERT INTO training_load (date, daily_tss, ctl, atl, tsb)
            VALUES %s
            ON CONFLICT (date) DO UPDATE SET
                daily_tss = EXCLUDED.daily_tss, ctl = EXCLUDED.ctl,
                atl = EXCLUDED.atl, tsb = EXCLUDED.tsb
        """, results)
        conn.commit()
        display_rows = [(r[0], r[1], r[2], r[3], r[4]) for r in results[-7:]]

    # Display
    print("═" * 50)
    print("    📈 PERFORMANCE MANAGEMENT CHART")
    print("═" * 50)
    print(f"\n{'Date':>12}  {'TSS':>5}  {'CTL':>6}  {'ATL':>6}  {'TSB':>6}")
    print("-" * 42)
    for row in display_rows:
        d, tss, ctl_v, atl_v, tsb_v = row
        tss_val = float(tss) if tss else 0
        print(f"{str(d):>12}  {tss_val:>5.0f}  {float(ctl_v):>6.1f}  {float(atl_v):>6.1f}  {float(tsb_v):>6.1f}")

    # Latest values
    last = display_rows[-1] if display_rows else None
    if last:
        tsb_v = float(last[4])
        trend = "Fresh ✅" if tsb_v > 0 else ("Recovering" if tsb_v > -10 else "Loading 💪")
        print(f"\nCurrent: CTL {float(last[2]):.1f} | ATL {float(last[3]):.1f} | TSB {float(last[4]):.1f} — {trend}")
    
    cur.close(); conn.close()


# ── Post-Ride Analysis ────────────────────────────────────────────

def post_ride(target_date=None):
    """Post-ride analysis for a specific date."""
    if target_date is None:
        target_date = date.today().isoformat()

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    cur.execute("SELECT * FROM training_workouts WHERE date = %s AND completed = true", (target_date,))
    workouts = cur.fetchall()
    if not workouts:
        print(f"❌ No completed workouts found for {target_date}")
        cur.close(); conn.close()
        return

    cur.execute("SELECT * FROM whoop_recovery WHERE date = %s", (target_date,))
    recovery = cur.fetchone()

    print("═" * 50)
    print(f"    🚴 POST-RIDE ANALYSIS — {target_date}")
    print("═" * 50)

    if recovery:
        score = recovery["recovery_score"]
        emoji = "🟢" if score and float(score) >= 67 else ("🟡" if score and float(score) >= 34 else "🔴")
        print(f"\n{emoji} Morning Recovery: {score}% | HRV: {recovery['hrv_rmssd']}ms | Sleep: {recovery['sleep_duration_min']}min")

    for w in workouts:
        print(f"\n📋 {w['title']}")
        print("-" * 40)

        np_val = float(w['np_actual']) if w['np_actual'] else None
        avg_p = float(w['avg_power']) if w['avg_power'] else None
        avg_hr = float(w['avg_hr']) if w['avg_hr'] else None
        tss_p = float(w['tss_planned']) if w['tss_planned'] else None
        tss_a = float(w['tss_actual']) if w['tss_actual'] else None
        if_p = float(w['if_planned']) if w['if_planned'] else None
        if_a = float(w['if_actual']) if w['if_actual'] else None

        # Core metrics
        if np_val:
            if_calc = np_val / FTP
            print(f"  NP: {np_val:.0f}W | IF: {if_calc:.3f}")
        if tss_p and tss_a:
            diff_pct = (tss_a - tss_p) / tss_p * 100
            sign = "+" if diff_pct >= 0 else ""
            emoji = "✅" if abs(diff_pct) < 15 else "⚠️"
            print(f"  TSS: {tss_a:.0f} (planned {tss_p:.0f}, {sign}{diff_pct:.0f}%) {emoji}")

        # Efficiency Factor & Variability Index
        if np_val and avg_hr:
            ef = np_val / avg_hr
            print(f"  Efficiency Factor: {ef:.2f} (NP/AvgHR)")
        if np_val and avg_p:
            vi = np_val / avg_p
            print(f"  Variability Index: {vi:.2f} (NP/AvgPower)")
        elif np_val and if_a:
            # estimate avg power from IF relationship
            pass

        if avg_hr:
            print(f"  Avg HR: {avg_hr:.0f}bpm", end="")
            if w['max_hr']:
                print(f" | Max HR: {float(w['max_hr']):.0f}bpm", end="")
            print()

        if w['duration_actual_min']:
            dur = int(w['duration_actual_min'])
            print(f"  Duration: {dur // 60}h{dur % 60:02d}m")

        # Workout quality
        quality = float(w['workout_quality']) if w['workout_quality'] else None
        if quality:
            q_emoji = "🟢" if quality >= 80 else ("🟡" if quality >= 60 else "🔴")
            print(f"  Quality Score: {quality:.0f}/100 {q_emoji}")

        # Plan adherence
        if if_p and if_a:
            if_diff = abs(float(if_a) - float(if_p)) / float(if_p) * 100
            adherence = "Excellent" if if_diff < 5 else ("Good" if if_diff < 10 else "Deviated")
            print(f"  Plan Adherence: {adherence} (IF diff: {if_diff:.1f}%)")

    print("\n" + "═" * 50)
    cur.close(); conn.close()


# ── FTP Projection ────────────────────────────────────────────────

def ftp_project():
    """Project FTP trajectory toward 300W target."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT test_date, ftp_watts FROM ftp_history ORDER BY test_date")
    rows = cur.fetchall()
    cur.close(); conn.close()

    if not rows:
        print("❌ No FTP history found")
        return

    current_ftp = rows[-1][1]
    current_date = rows[-1][0]
    target_ftp = 300
    target_date = date(2026, 12, 31)
    vattern_date = date(2026, 6, 12)
    next_test = date(2026, 2, 26)

    weeks_to_target = max(1, (target_date - date.today()).days / 7)
    weekly_gain = (target_ftp - current_ftp) / weeks_to_target

    # Linear projection at key dates
    weeks_to_vattern = max(0, (vattern_date - date.today()).days / 7)
    ftp_at_vattern = current_ftp + weekly_gain * weeks_to_vattern
    weeks_to_eoy = max(0, (target_date - date.today()).days / 7)
    ftp_at_eoy = current_ftp + weekly_gain * weeks_to_eoy

    weeks_to_next = max(0, (next_test - date.today()).days / 7)
    ftp_at_next = current_ftp + weekly_gain * weeks_to_next

    print("═" * 50)
    print("    ⚡ FTP TRAJECTORY PROJECTION")
    print("═" * 50)
    print(f"\n  Current FTP: {current_ftp}W (as of {current_date})")
    print(f"  Target: {target_ftp}W by {target_date}")
    print(f"  Gap: {target_ftp - current_ftp}W over {weeks_to_target:.0f} weeks")
    print(f"  Required gain: {weekly_gain:.2f}W/week")

    print(f"\n  📅 Key Projections (linear):")
    print(f"     Next FTP test (~{next_test}): ~{ftp_at_next:.0f}W")
    print(f"     Vatternrundan ({vattern_date}): ~{ftp_at_vattern:.0f}W")
    print(f"     End of 2026 ({target_date}): ~{ftp_at_eoy:.0f}W")

    if len(rows) >= 2:
        # Historical rate
        first = rows[0]
        elapsed_weeks = max(1, (current_date - first[0]).days / 7)
        hist_rate = (current_ftp - first[1]) / elapsed_weeks
        print(f"\n  📊 Historical rate: {hist_rate:+.2f}W/week (from {first[1]}W on {first[0]})")

    on_track = "✅ On track" if weekly_gain <= 1.0 else ("⚠️ Aggressive but achievable" if weekly_gain <= 1.5 else "🔴 Very aggressive")
    print(f"\n  Status: {on_track}")
    print("═" * 50)


# ── Weekly Summary ────────────────────────────────────────────────

def weekly_summary(target_date=None):
    """Generate weekly training summary."""
    if target_date:
        d = date.fromisoformat(target_date)
    else:
        d = date.today()

    # Find Monday of that week
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # Workouts
    cur.execute("""
        SELECT * FROM training_workouts
        WHERE date BETWEEN %s AND %s ORDER BY date
    """, (monday, sunday))
    workouts = cur.fetchall()

    # Whoop
    cur.execute("""
        SELECT * FROM whoop_recovery
        WHERE date BETWEEN %s AND %s ORDER BY date
    """, (monday, sunday))
    recoveries = cur.fetchall()

    # PMC (latest)
    cur.execute("SELECT * FROM training_load ORDER BY date DESC LIMIT 2")
    load_rows = cur.fetchall()

    # FTP
    cur.execute("SELECT * FROM ftp_history ORDER BY test_date DESC LIMIT 1")
    ftp_row = cur.fetchone()

    cur.close(); conn.close()

    # Calculate training metrics
    tss_planned = sum(float(w['tss_planned'] or 0) for w in workouts)
    tss_actual = sum(float(w['tss_actual'] or 0) for w in workouts)
    completed = [w for w in workouts if w['completed']]
    total_workouts = [w for w in workouts if w['tss_planned'] and float(w['tss_planned']) > 0]
    completed_real = [w for w in completed if w['tss_actual'] and float(w['tss_actual']) > 0]

    total_min = sum(int(w['duration_actual_min'] or w['duration_planned_min'] or 0) for w in completed)
    hours = total_min / 60

    # IF stats
    ifs = [float(w['if_actual']) for w in completed if w['if_actual']]
    avg_if = sum(ifs) / len(ifs) if ifs else 0
    peak_if = max(ifs) if ifs else 0
    peak_if_workout = None
    for w in completed:
        if w['if_actual'] and float(w['if_actual']) == peak_if:
            peak_if_workout = w['title']

    # Recovery stats
    avg_recovery = sum(float(r['recovery_score'] or 0) for r in recoveries) / len(recoveries) if recoveries else 0
    avg_hrv = sum(float(r['hrv_rmssd'] or 0) for r in recoveries) / len(recoveries) if recoveries else 0
    avg_sleep = sum(float(r['sleep_duration_min'] or 0) for r in recoveries) / len(recoveries) / 60 if recoveries else 0

    recovery_color = "green" if avg_recovery >= 67 else ("yellow" if avg_recovery >= 34 else "red")

    # Build output
    tss_diff_pct = ((tss_actual - tss_planned) / tss_planned * 100) if tss_planned > 0 else 0
    tss_sign = "+" if tss_diff_pct >= 0 else ""
    tss_emoji = "✅" if abs(tss_diff_pct) < 15 else "⚠️"

    out = []
    out.append(f"📊 **Week of {monday.strftime('%b %d')}–{sunday.strftime('%b %d, %Y')}**")
    out.append("")
    out.append("**TRAINING LOAD**")
    out.append(f"  TSS Planned: {tss_planned:.0f} | Actual: {tss_actual:.0f} ({tss_sign}{tss_diff_pct:.0f}%) {tss_emoji}")
    out.append(f"  Hours: {hours:.1f}")
    out.append(f"  Workouts: {len(completed_real)}/{len(total_workouts)} completed")

    out.append("")
    out.append("**POWER**")
    current_ftp = ftp_row['ftp_watts'] if ftp_row else FTP
    out.append(f"  Current FTP: {current_ftp}W")
    if ifs:
        out.append(f"  Week Avg IF: {avg_if:.2f} | Peak IF: {peak_if:.2f} ({peak_if_workout})")

    out.append("")
    out.append("**FITNESS / FATIGUE / FORM**")
    if load_rows:
        l = load_rows[0]
        ctl_val = float(l['ctl'])
        # CTL change
        if len(load_rows) > 1:
            prev_ctl = float(load_rows[1]['ctl'])
            ctl_change = ctl_val - prev_ctl
            out.append(f"  CTL: {ctl_val:.1f} ({ctl_change:+.1f}) | ATL: {float(l['atl']):.1f} | TSB: {float(l['tsb']):.1f}")
        else:
            out.append(f"  CTL: {ctl_val:.1f} | ATL: {float(l['atl']):.1f} | TSB: {float(l['tsb']):.1f}")
        tsb = float(l['tsb'])
        trend = "Fresh, ready to load" if tsb > 0 else ("Absorbing load well" if tsb > -15 else "Heavy loading, watch fatigue")
        out.append(f"  Trend: {trend}")
    else:
        out.append("  Run `cycling-training pmc` to populate")

    out.append("")
    out.append("**RECOVERY (Whoop avg)**")
    out.append(f"  Recovery Score: {avg_recovery:.0f}% ({recovery_color} avg)")
    out.append(f"  HRV: {avg_hrv:.0f}ms")
    out.append(f"  Sleep: {avg_sleep:.1f} hrs avg")

    # Key highlights
    out.append("")
    out.append("**KEY HIGHLIGHTS**")
    for w in completed_real:
        q = float(w['workout_quality']) if w['workout_quality'] else None
        emoji = "✅" if q and q >= 70 else "⚠️"
        np_str = f", NP {w['np_actual']}W" if w['np_actual'] else ""
        out.append(f"  {emoji} {w['date'].strftime('%a')}: {w['title']}{np_str}")

    # FTP trajectory
    out.append("")
    out.append("**FTP TRAJECTORY**")
    target_ftp = 300
    target_date_ftp = date(2026, 12, 31)
    weeks_left = max(1, (target_date_ftp - date.today()).days / 7)
    gain_needed = (target_ftp - current_ftp) / weeks_left
    vattern_date = date(2026, 6, 12)
    weeks_to_v = max(0, (vattern_date - date.today()).days / 7)
    ftp_at_v = current_ftp + gain_needed * weeks_to_v
    out.append(f"  Current: {current_ftp}W -> Target: {target_ftp}W by end of 2026")
    out.append(f"  Weeks remaining: {weeks_left:.0f} | Required: {gain_needed:.2f}W/week")
    out.append(f"  Projected at Vatternrundan ({vattern_date}): ~{ftp_at_v:.0f}W")
    out.append(f"  Vatternrundan goal: 315km sub-10 hours")

    print("\n".join(out))


# ── Updated Status ────────────────────────────────────────────────

# (status is defined above, already reads training_load)


# ── Strava ─────────────────────────────────────────────────────────

def strava_refresh_token():
    """Refresh Strava OAuth2 token. Returns new access token or None."""
    env = load_env(STRAVA_ENV)
    refresh = env.get("STRAVA_REFRESH_TOKEN")
    if not refresh:
        return None  # Legacy token, no refresh possible

    resp = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": env.get("STRAVA_CLIENT_ID"),
        "client_secret": env.get("STRAVA_CLIENT_SECRET"),
        "grant_type": "refresh_token",
        "refresh_token": refresh,
    })
    if resp.status_code != 200:
        print(f"⚠️  Strava token refresh failed: {resp.status_code}")
        return None

    data = resp.json()
    new_access = data.get("access_token")
    new_refresh = data.get("refresh_token", refresh)

    # Update env file
    lines = STRAVA_ENV.read_text().splitlines()
    new_lines = []
    has_refresh = False
    for line in lines:
        if line.startswith("STRAVA_ACCESS_TOKEN="):
            new_lines.append(f"STRAVA_ACCESS_TOKEN={new_access}")
        elif line.startswith("STRAVA_REFRESH_TOKEN="):
            new_lines.append(f"STRAVA_REFRESH_TOKEN={new_refresh}")
            has_refresh = True
        else:
            new_lines.append(line)
    if not has_refresh:
        new_lines.append(f"STRAVA_REFRESH_TOKEN={new_refresh}")
    STRAVA_ENV.write_text("\n".join(new_lines) + "\n")
    return new_access


def strava_api(endpoint, token):
    """Call Strava API with auto-retry on 401."""
    url = f"https://www.strava.com/api/v3{endpoint}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code == 401:
        new_token = strava_refresh_token()
        if new_token:
            resp = requests.get(url, headers={"Authorization": f"Bearer {new_token}"})
        else:
            print("❌ Strava auth failed. Token may be expired with no refresh token.")
            return None
    if resp.status_code == 404:
        return []  # Club may not have events endpoint
    if resp.status_code != 200:
        print(f"⚠️  Strava API {endpoint}: HTTP {resp.status_code}")
        return None
    return resp.json()


def strava_events():
    """Fetch and display upcoming Strava club events."""
    env = load_env(STRAVA_ENV)
    token = env.get("STRAVA_ACCESS_TOKEN", "")
    if not token:
        print("❌ No Strava access token found")
        return

    now = datetime.now(timezone.utc)
    all_events = []
    conn = get_db()
    cur = conn.cursor()

    for club_id, club_name in STRAVA_CLUBS.items():
        data = strava_api(f"/clubs/{club_id}/group_events", token)
        if not data:
            continue
        for ev in data:
            event_id = ev.get("id")
            title = ev.get("title", "Untitled")
            desc = ev.get("description", "")
            start_str = ev.get("upcoming_occurrences", [None])[0] if ev.get("upcoming_occurrences") else ev.get("start_datetime")
            if not start_str:
                continue

            try:
                start_time = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue

            if start_time < now:
                continue

            joined = ev.get("joined", False)
            route_id = ev.get("route_id")

            # Upsert to DB
            cur.execute("""
                INSERT INTO strava_events (event_id, club_id, title, description, start_time, route_id, joined)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_id) DO UPDATE SET
                    title = EXCLUDED.title, description = EXCLUDED.description,
                    start_time = EXCLUDED.start_time, joined = EXCLUDED.joined
            """, (event_id, club_id, title, desc[:500], start_time, route_id, joined))

            all_events.append({
                "club": club_name,
                "title": title,
                "description": desc[:120],
                "start_time": start_time,
                "joined": joined,
            })
        time.sleep(0.5)  # Rate limit courtesy

    conn.commit()
    cur.close()
    conn.close()

    # Display
    all_events.sort(key=lambda e: e["start_time"])

    if not all_events:
        print("📅 No upcoming club events found.")
        return

    print("═" * 55)
    print("    📅 UPCOMING STRAVA CLUB EVENTS")
    print("═" * 55)
    for ev in all_events:
        local = ev["start_time"].strftime("%a %b %d, %I:%M %p")
        joined = " ✅ Joined" if ev["joined"] else ""
        print(f"\n  {local}{joined}")
        print(f"  🚴 {ev['title']}")
        print(f"  📍 {ev['club']}")
        if ev["description"]:
            print(f"  📝 {ev['description']}")
    print("\n" + "═" * 55)


# ── Weather ────────────────────────────────────────────────────────

def get_kit_recommendation(temp_f):
    """Return kit recommendation based on temperature."""
    if temp_f < 30:
        return "❄️ Below 30F -- indoor ride recommended. Don't ride outdoors."
    elif temp_f < 40:
        return "🥶 Full winter kit: thermal bibs, winter jacket, shoe covers, heavy gloves, balaclava"
    elif temp_f < 50:
        return "🧥 Cold weather: thermal jersey, leg warmers, wind vest, medium gloves, ear cover"
    elif temp_f < 60:
        return "🧤 Cool weather: long sleeve jersey, arm warmers, knee warmers, light gloves"
    elif temp_f < 70:
        return "👕 Mild: short sleeve jersey, bibs, light arm warmers optional"
    else:
        return "☀️ Summer kit: short sleeve jersey, bibs, sunscreen"


GEOCODE_CACHE = {
    "brooklyn, ny": (40.6782, -73.9442),
    "new york": (40.7128, -74.0060),
    "manhattan": (40.7831, -73.9712),
    "central park": (40.7829, -73.9654),
}

WMO_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Foggy", 48: "Rime fog", 51: "Light drizzle", 53: "Drizzle",
    55: "Heavy drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light showers", 81: "Showers", 82: "Heavy showers",
    85: "Light snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Thunderstorm w/ heavy hail",
}


def geocode(location):
    """Get lat/lon for a location. Uses cache or Open-Meteo geocoding."""
    key = location.lower().strip()
    if key in GEOCODE_CACHE:
        return GEOCODE_CACHE[key]
    try:
        resp = requests.get(
            f"https://geocoding-api.open-meteo.com/v1/search?name={location}&count=1&language=en&format=json",
            timeout=10,
        )
        results = resp.json().get("results", [])
        if results:
            return (results[0]["latitude"], results[0]["longitude"])
    except Exception:
        pass
    return (40.6782, -73.9442)  # Default Brooklyn


def c_to_f(c):
    return round(c * 9 / 5 + 32)


def weather(location="Brooklyn, NY"):
    """Show weather and ride kit recommendation using Open-Meteo."""
    lat, lon = geocode(location)
    try:
        resp = requests.get(
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,apparent_temperature,wind_speed_10m,wind_direction_10m,relative_humidity_2m,weather_code"
            f"&daily=temperature_2m_max,temperature_2m_min,weather_code"
            f"&temperature_unit=fahrenheit&wind_speed_unit=mph&timezone=America/New_York&forecast_days=3",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"❌ Weather fetch failed: {e}")
        return

    cur = data.get("current", {})
    temp_f = round(cur.get("temperature_2m", 0))
    feels_f = round(cur.get("apparent_temperature", 0))
    wind_mph = round(cur.get("wind_speed_10m", 0))
    humidity = cur.get("relative_humidity_2m", 0)
    wcode = cur.get("weather_code", 0)
    desc = WMO_CODES.get(wcode, "Unknown")

    print("═" * 55)
    print(f"    🌤️  WEATHER — {location}")
    print("═" * 55)

    print(f"\n  NOW: {temp_f}F (feels like {feels_f}F)")
    print(f"  Conditions: {desc}")
    print(f"  Wind: {wind_mph} mph")
    print(f"  Humidity: {humidity}%")

    print(f"\n  KIT: {get_kit_recommendation(feels_f)}")

    daily = data.get("daily", {})
    dates = daily.get("time", [])
    highs = daily.get("temperature_2m_max", [])
    lows = daily.get("temperature_2m_min", [])
    codes = daily.get("weather_code", [])

    if dates:
        print(f"\n  {'Date':<12} {'High':>5} {'Low':>5} {'Conditions'}")
        print("  " + "-" * 45)
        for i in range(min(3, len(dates))):
            hi = round(highs[i])
            lo = round(lows[i])
            cond = WMO_CODES.get(codes[i], "")
            rideable = "✅" if hi >= 30 else "❄️"
            print(f"  {dates[i]:<12} {hi:>4}F {lo:>4}F {cond} {rideable}")

    print("\n" + "═" * 55)


# ── Correlation Engine ─────────────────────────────────────────────

def cmd_correlate():
    """Pattern discovery: recovery vs workout quality correlations."""
    import pandas as pd
    import numpy as np

    conn = get_db()
    df = pd.read_sql("""
        SELECT dp.*, 
               LAG(dp.strain, 1) OVER (ORDER BY dp.date) as prev_strain,
               LAG(dp.sleep_hours, 1) OVER (ORDER BY dp.date) as prev_sleep_hours,
               LAG(dp.sleep_score, 1) OVER (ORDER BY dp.date) as prev_sleep_score
        FROM daily_performance dp
        ORDER BY dp.date
    """, conn)
    conn.close()

    # Filter to days with both recovery and workout data
    both = df.dropna(subset=['recovery_score', 'workout_quality']).copy()
    both['recovery_score'] = both['recovery_score'].astype(float)
    both['workout_quality'] = both['workout_quality'].astype(float)
    both['hrv_rmssd'] = both['hrv_rmssd'].astype(float)
    both['sleep_hours'] = both['sleep_hours'].astype(float)
    both['sleep_score'] = both['sleep_score'].astype(float)
    both['strain'] = both['strain'].astype(float)
    both['prev_strain'] = pd.to_numeric(both['prev_strain'], errors='coerce')
    both['prev_sleep_hours'] = pd.to_numeric(both['prev_sleep_hours'], errors='coerce')
    both['prev_sleep_score'] = pd.to_numeric(both['prev_sleep_score'], errors='coerce')
    both['dow'] = pd.to_datetime(both['date']).dt.day_name()

    n = len(both)
    print("═" * 55)
    print("    🔬 RECOVERY-TRAINING CORRELATION ANALYSIS")
    print("═" * 55)
    print(f"\n  Data: {n} days with both recovery and workout data")

    # 1. Recovery vs workout quality by bracket
    print(f"\n{'─'*55}")
    print("  📊 RECOVERY SCORE vs WORKOUT QUALITY")
    print(f"{'─'*55}")
    brackets = [
        ('🔴 Red (<33)', both[both['recovery_score'] < 33]),
        ('🟡 Yellow (33-66)', both[(both['recovery_score'] >= 33) & (both['recovery_score'] <= 66)]),
        ('🟢 Green (>66)', both[both['recovery_score'] > 66]),
    ]
    for label, subset in brackets:
        if len(subset) > 0:
            avg_q = subset['workout_quality'].mean()
            std_q = subset['workout_quality'].std()
            print(f"  {label}: avg quality {avg_q:.1f} ± {std_q:.1f} (n={len(subset)})")
        else:
            print(f"  {label}: no data")

    # Correlation coefficient
    corr_rq = both[['recovery_score', 'workout_quality']].corr().iloc[0, 1]
    print(f"\n  Correlation (recovery vs quality): r = {corr_rq:.3f}")
    strength = "strong" if abs(corr_rq) > 0.5 else ("moderate" if abs(corr_rq) > 0.3 else "weak")
    print(f"  Interpretation: {strength} {'positive' if corr_rq > 0 else 'negative'} relationship")

    # 2. HRV threshold
    print(f"\n{'─'*55}")
    print("  💓 HRV THRESHOLD ANALYSIS")
    print(f"{'─'*55}")
    good = both[both['workout_quality'] >= 80]
    poor = both[both['workout_quality'] < 80]
    if len(good) >= 5 and len(poor) >= 5:
        good_hrv = good['hrv_rmssd'].dropna()
        poor_hrv = poor['hrv_rmssd'].dropna()
        print(f"  Good workouts (quality >= 80): avg HRV {good_hrv.mean():.1f}ms (n={len(good_hrv)})")
        print(f"  Other workouts (quality < 80): avg HRV {poor_hrv.mean():.1f}ms (n={len(poor_hrv)})")
        threshold = good_hrv.quantile(0.25)
        print(f"  Suggested HRV threshold: ~{threshold:.0f}ms (25th pctile of good workouts)")
    else:
        print(f"  Insufficient data (good: n={len(good)}, poor: n={len(poor)})")

    # 3. Sleep impact
    print(f"\n{'─'*55}")
    print("  😴 SLEEP IMPACT ON WORKOUT QUALITY")
    print(f"{'─'*55}")
    sleep_data = both.dropna(subset=['sleep_hours'])
    if len(sleep_data) >= 10:
        corr_sleep = sleep_data[['sleep_hours', 'workout_quality']].corr().iloc[0, 1]
        print(f"  Sleep hours vs quality: r = {corr_sleep:.3f} (n={len(sleep_data)})")
        # Bucket by sleep hours
        low_sleep = sleep_data[sleep_data['sleep_hours'] < 6]
        mid_sleep = sleep_data[(sleep_data['sleep_hours'] >= 6) & (sleep_data['sleep_hours'] < 7.5)]
        high_sleep = sleep_data[sleep_data['sleep_hours'] >= 7.5]
        for label, subset in [('<6 hrs', low_sleep), ('6-7.5 hrs', mid_sleep), ('7.5+ hrs', high_sleep)]:
            if len(subset) > 0:
                print(f"  Sleep {label}: avg quality {subset['workout_quality'].mean():.1f} (n={len(subset)})")

    sleep_score_data = both.dropna(subset=['sleep_score'])
    if len(sleep_score_data) >= 10:
        corr_ss = sleep_score_data[['sleep_score', 'workout_quality']].corr().iloc[0, 1]
        print(f"  Sleep score vs quality: r = {corr_ss:.3f} (n={len(sleep_score_data)})")

    # 4. Strain accumulation
    print(f"\n{'─'*55}")
    print("  🔥 PREVIOUS DAY STRAIN vs WORKOUT QUALITY")
    print(f"{'─'*55}")
    strain_data = both.dropna(subset=['prev_strain'])
    if len(strain_data) >= 10:
        corr_strain = strain_data[['prev_strain', 'workout_quality']].corr().iloc[0, 1]
        print(f"  Prior day strain vs quality: r = {corr_strain:.3f} (n={len(strain_data)})")
        low_strain = strain_data[strain_data['prev_strain'] < 10]
        high_strain = strain_data[strain_data['prev_strain'] >= 14]
        mid_strain = strain_data[(strain_data['prev_strain'] >= 10) & (strain_data['prev_strain'] < 14)]
        for label, subset in [('Low (<10)', low_strain), ('Medium (10-14)', mid_strain), ('High (14+)', high_strain)]:
            if len(subset) > 0:
                print(f"  Prior strain {label}: avg quality {subset['workout_quality'].mean():.1f} (n={len(subset)})")

    # 5. Best workout conditions
    print(f"\n{'─'*55}")
    print("  🏆 BEST WORKOUT CONDITIONS")
    print(f"{'─'*55}")
    top = both.nlargest(20, 'workout_quality')
    if len(top) >= 5:
        print(f"  Top 20 workouts (quality avg {top['workout_quality'].mean():.1f}):")
        print(f"    Avg recovery: {top['recovery_score'].mean():.0f}%")
        print(f"    Avg HRV: {top['hrv_rmssd'].mean():.0f}ms")
        print(f"    Avg sleep: {top['sleep_hours'].mean():.1f} hrs")
        print(f"    Avg sleep score: {top['sleep_score'].mean():.0f}%")

    # 6. Weekly pattern
    print(f"\n{'─'*55}")
    print("  📅 DAY OF WEEK PATTERNS")
    print(f"{'─'*55}")
    day_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    dow_quality = both.groupby('dow')['workout_quality'].agg(['mean', 'count'])
    dow_strain = both.groupby('dow')['strain'].agg(['mean', 'count'])
    
    print("  Workout Quality by Day:")
    for day in day_order:
        if day in dow_quality.index:
            row = dow_quality.loc[day]
            bar = "█" * int(row['mean'] / 5)
            print(f"    {day:<10} {row['mean']:5.1f} {bar} (n={int(row['count'])})")

    print("\n  Strain by Day:")
    strain_dow = df.dropna(subset=['strain']).copy()
    strain_dow['dow'] = pd.to_datetime(strain_dow['date']).dt.day_name()
    dow_s = strain_dow.groupby('dow')['strain'].agg(['mean', 'count'])
    for day in day_order:
        if day in dow_s.index:
            row = dow_s.loc[day]
            print(f"    {day:<10} {float(row['mean']):5.1f} (n={int(row['count'])})")

    # 7. Consistency metric
    print(f"\n{'─'*55}")
    print("  ✅ WORKOUT CONSISTENCY")
    print(f"{'─'*55}")
    conn2 = get_db()
    cur = conn2.cursor()
    cur.execute("SELECT COUNT(*) FROM training_workouts WHERE tss_planned > 0")
    total_planned = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM training_workouts WHERE tss_planned > 0 AND completed = true")
    total_completed = cur.fetchone()[0]
    cur.close(); conn2.close()
    pct = (total_completed / total_planned * 100) if total_planned > 0 else 0
    print(f"  Planned workouts: {total_planned}")
    print(f"  Completed: {total_completed} ({pct:.1f}%)")

    print("\n" + "═" * 55)


def cmd_trends():
    """Long-term trend analysis."""
    import pandas as pd
    import numpy as np

    conn = get_db()

    print("═" * 55)
    print("    📈 LONG-TERM TRAINING TRENDS")
    print("═" * 55)

    # 1. FTP progression
    print(f"\n{'─'*55}")
    print("  ⚡ FTP PROGRESSION")
    print(f"{'─'*55}")
    ftp_df = pd.read_sql("SELECT test_date, ftp_watts, test_protocol, confidence FROM ftp_history ORDER BY test_date", conn)
    if len(ftp_df) > 0:
        for _, row in ftp_df.iterrows():
            proto = f" ({row['test_protocol']})" if row['test_protocol'] else ""
            conf = f" [{row['confidence']}]" if row['confidence'] else ""
            print(f"    {row['test_date']}  {row['ftp_watts']}W{proto}{conf}")
        if len(ftp_df) >= 2:
            first, last = ftp_df.iloc[0], ftp_df.iloc[-1]
            days = (last['test_date'] - first['test_date']).days
            gain = last['ftp_watts'] - first['ftp_watts']
            if days > 0:
                rate = gain / (days / 7)
                print(f"\n    Overall: {first['ftp_watts']}W -> {last['ftp_watts']}W ({gain:+d}W over {days} days, {rate:+.2f}W/week)")

    # Infer from NP trends
    np_df = pd.read_sql("""
        SELECT date, AVG(np_actual) as avg_np FROM training_workouts 
        WHERE np_actual IS NOT NULL AND completed = true
        GROUP BY date ORDER BY date
    """, conn)
    if len(np_df) >= 14:
        np_df['np_7d'] = np_df['avg_np'].astype(float).rolling(7, min_periods=3).mean()
        recent = np_df.tail(30)
        if len(recent) >= 2:
            first_np = recent['np_7d'].dropna().iloc[0]
            last_np = recent['np_7d'].dropna().iloc[-1]
            trend = "↑ trending up" if last_np > first_np + 2 else ("↓ trending down" if last_np < first_np - 2 else "→ stable")
            print(f"    NP trend (30d rolling): {first_np:.0f}W -> {last_np:.0f}W {trend}")

    # 2. Training volume (weekly TSS, last 3 months)
    print(f"\n{'─'*55}")
    print("  📊 WEEKLY TRAINING VOLUME (last 12 weeks)")
    print(f"{'─'*55}")
    tss_df = pd.read_sql("""
        SELECT date, COALESCE(SUM(tss_actual), 0) as tss, COUNT(*) FILTER (WHERE completed) as workouts
        FROM training_workouts
        WHERE date >= CURRENT_DATE - INTERVAL '84 days'
        GROUP BY date ORDER BY date
    """, conn)
    if len(tss_df) > 0:
        tss_df['date'] = pd.to_datetime(tss_df['date'])
        tss_df['week'] = tss_df['date'].dt.isocalendar().week.astype(int)
        tss_df['year'] = tss_df['date'].dt.isocalendar().year.astype(int)
        weekly = tss_df.groupby(['year', 'week']).agg(
            tss=('tss', 'sum'),
            workouts=('workouts', 'sum'),
            start=('date', 'min')
        ).reset_index()
        for _, row in weekly.iterrows():
            tss_val = float(row['tss'])
            bar = "█" * int(tss_val / 30)
            print(f"    {row['start'].strftime('%b %d')}  TSS: {tss_val:5.0f} {bar} ({int(row['workouts'])} rides)")
        avg_tss = float(weekly['tss'].mean())
        print(f"\n    Avg weekly TSS: {avg_tss:.0f}")

    # 3. Recovery trend
    print(f"\n{'─'*55}")
    print("  💚 RECOVERY TREND")
    print(f"{'─'*55}")
    rec_df = pd.read_sql("""
        SELECT date, recovery_score, hrv_rmssd FROM whoop_recovery 
        WHERE recovery_score IS NOT NULL ORDER BY date
    """, conn)
    if len(rec_df) >= 7:
        rec_df['recovery_score'] = rec_df['recovery_score'].astype(float)
        rec_df['hrv_rmssd'] = rec_df['hrv_rmssd'].astype(float)
        rec_df['rec_7d'] = rec_df['recovery_score'].rolling(7, min_periods=3).mean()
        rec_df['rec_30d'] = rec_df['recovery_score'].rolling(30, min_periods=7).mean()

        recent = rec_df.tail(1).iloc[0]
        month_ago = rec_df.iloc[-30] if len(rec_df) >= 30 else rec_df.iloc[0]
        print(f"    Current 7-day avg: {recent['rec_7d']:.0f}%")
        print(f"    Current 30-day avg: {recent['rec_30d']:.0f}%")
        delta = recent['rec_7d'] - float(month_ago['rec_7d']) if pd.notna(month_ago['rec_7d']) else 0
        trend = "↑ improving" if delta > 3 else ("↓ declining" if delta < -3 else "→ stable")
        print(f"    30-day change: {delta:+.0f}% {trend}")

    # 4. HRV trend
    print(f"\n{'─'*55}")
    print("  💓 HRV TREND")
    print(f"{'─'*55}")
    if len(rec_df) >= 7:
        rec_df['hrv_7d'] = rec_df['hrv_rmssd'].rolling(7, min_periods=3).mean()
        rec_df['hrv_30d'] = rec_df['hrv_rmssd'].rolling(30, min_periods=7).mean()
        recent_hrv = rec_df.tail(1).iloc[0]
        month_ago_hrv = rec_df.iloc[-30] if len(rec_df) >= 30 else rec_df.iloc[0]
        print(f"    Current 7-day avg: {recent_hrv['hrv_7d']:.1f}ms")
        print(f"    Current 30-day avg: {recent_hrv['hrv_30d']:.1f}ms")
        hrv_delta = recent_hrv['hrv_7d'] - float(month_ago_hrv['hrv_7d']) if pd.notna(month_ago_hrv['hrv_7d']) else 0
        trend = "↑ improving" if hrv_delta > 3 else ("↓ declining" if hrv_delta < -3 else "→ stable")
        print(f"    30-day change: {hrv_delta:+.1f}ms {trend}")

    # 5. Sleep trend
    print(f"\n{'─'*55}")
    print("  😴 SLEEP TREND")
    print(f"{'─'*55}")
    sleep_df = pd.read_sql("""
        SELECT date, sleep_duration_min, sleep_score FROM whoop_recovery 
        WHERE sleep_duration_min IS NOT NULL ORDER BY date
    """, conn)
    if len(sleep_df) >= 7:
        sleep_df['hours'] = sleep_df['sleep_duration_min'].astype(float) / 60
        sleep_df['hrs_7d'] = sleep_df['hours'].rolling(7, min_periods=3).mean()
        sleep_df['score_7d'] = sleep_df['sleep_score'].astype(float).rolling(7, min_periods=3).mean()
        recent_s = sleep_df.tail(1).iloc[0]
        print(f"    7-day avg sleep: {recent_s['hrs_7d']:.1f} hrs")
        print(f"    7-day avg sleep score: {recent_s['score_7d']:.0f}%")
        overall_avg = sleep_df['hours'].mean()
        print(f"    Overall avg: {overall_avg:.1f} hrs")

    # 6. Workout adherence trend
    print(f"\n{'─'*55}")
    print("  ✅ WORKOUT ADHERENCE (last 12 weeks)")
    print(f"{'─'*55}")
    adh_df = pd.read_sql("""
        SELECT date, 
               CASE WHEN tss_planned > 0 THEN 1 ELSE 0 END as planned,
               CASE WHEN tss_planned > 0 AND completed THEN 1 ELSE 0 END as done
        FROM training_workouts
        WHERE date >= CURRENT_DATE - INTERVAL '84 days'
        ORDER BY date
    """, conn)
    if len(adh_df) > 0:
        adh_df['date'] = pd.to_datetime(adh_df['date'])
        adh_df['week'] = adh_df['date'].dt.isocalendar().week.astype(int)
        adh_df['year'] = adh_df['date'].dt.isocalendar().year.astype(int)
        weekly_adh = adh_df.groupby(['year', 'week']).agg(
            planned=('planned', 'sum'),
            done=('done', 'sum'),
            start=('date', 'min')
        ).reset_index()
        for _, row in weekly_adh.iterrows():
            pct = (row['done'] / row['planned'] * 100) if row['planned'] > 0 else 0
            emoji = "✅" if pct >= 80 else ("⚠️" if pct >= 50 else "❌")
            print(f"    {row['start'].strftime('%b %d')}  {int(row['done'])}/{int(row['planned'])} ({pct:.0f}%) {emoji}")

    conn.close()
    print("\n" + "═" * 55)


def cmd_insights():
    """Generate and store AI-driven insights from correlation and trend data."""
    import pandas as pd
    import numpy as np

    conn = get_db()
    cur = conn.cursor()

    # Create insights table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS training_insights (
            id SERIAL PRIMARY KEY,
            date DATE NOT NULL,
            insight_type VARCHAR(50),
            insight_text TEXT,
            confidence VARCHAR(10),
            data_points INTEGER,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    conn.commit()

    # Load data
    df = pd.read_sql("""
        SELECT dp.*, 
               LAG(dp.strain, 1) OVER (ORDER BY dp.date) as prev_strain
        FROM daily_performance dp ORDER BY dp.date
    """, conn)

    both = df.dropna(subset=['recovery_score', 'workout_quality']).copy()
    for col in ['recovery_score', 'workout_quality', 'hrv_rmssd', 'sleep_hours', 'sleep_score', 'strain']:
        both[col] = pd.to_numeric(both[col], errors='coerce')
    both['prev_strain'] = pd.to_numeric(both['prev_strain'], errors='coerce')

    n = len(both)
    today = date.today().isoformat()
    insights = []

    # 1. Recovery-quality relationship
    if n >= 20:
        green = both[both['recovery_score'] > 66]
        red = both[both['recovery_score'] < 33]
        yellow = both[(both['recovery_score'] >= 33) & (both['recovery_score'] <= 66)]
        corr = both[['recovery_score', 'workout_quality']].corr().iloc[0, 1]
        
        if len(green) >= 5 and len(red) >= 5:
            diff = green['workout_quality'].mean() - red['workout_quality'].mean()
            if abs(diff) > 3:
                direction = "higher" if diff > 0 else "lower"
                text = (f"Your data shows green recovery days produce {direction} workout quality "
                       f"({green['workout_quality'].mean():.1f} vs {red['workout_quality'].mean():.1f}, "
                       f"correlation r={corr:.2f}). Based on {n} days of data.")
            else:
                text = (f"Your data shows recovery score has minimal impact on workout quality "
                       f"(green: {green['workout_quality'].mean():.1f}, red: {red['workout_quality'].mean():.1f}, "
                       f"r={corr:.2f}). You perform consistently regardless of recovery. Based on {n} days.")
            conf = "high" if n >= 60 else ("medium" if n >= 30 else "low")
            insights.append(("recovery_correlation", text, conf, n))

    # 2. HRV insight
    good = both[both['workout_quality'] >= 80]
    poor = both[both['workout_quality'] < 80]
    if len(good) >= 10:
        threshold = good['hrv_rmssd'].quantile(0.25)
        text = (f"Your best workouts (quality >= 80, n={len(good)}) typically occur when HRV is above "
               f"{threshold:.0f}ms. Average HRV on good days: {good['hrv_rmssd'].mean():.0f}ms "
               f"vs other days: {poor['hrv_rmssd'].mean():.0f}ms.")
        insights.append(("hrv_threshold", text, "medium", len(good)))

    # 3. Sleep insight
    sleep_data = both.dropna(subset=['sleep_hours'])
    if len(sleep_data) >= 20:
        corr_s = sleep_data[['sleep_hours', 'workout_quality']].corr().iloc[0, 1]
        avg_sleep = sleep_data['sleep_hours'].mean()
        text = (f"Sleep hours correlate with workout quality at r={corr_s:.2f} (n={len(sleep_data)}). "
               f"Your average sleep is {avg_sleep:.1f} hrs. "
               f"Nights with 7.5+ hrs show avg quality of "
               f"{sleep_data[sleep_data['sleep_hours'] >= 7.5]['workout_quality'].mean():.1f} "
               f"vs {sleep_data[sleep_data['sleep_hours'] < 6]['workout_quality'].mean():.1f} on <6 hr nights.")
        insights.append(("sleep_impact", text, "medium" if abs(corr_s) > 0.2 else "low", len(sleep_data)))

    # 4. Consistency insight
    cur.execute("SELECT COUNT(*) FROM training_workouts WHERE tss_planned > 0")
    planned = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM training_workouts WHERE tss_planned > 0 AND completed = true")
    completed = cur.fetchone()[0]
    pct = (completed / planned * 100) if planned > 0 else 0
    text = f"Workout completion rate: {pct:.1f}% ({completed}/{planned} planned workouts completed over full history)."
    insights.append(("consistency", text, "high", planned))

    # 5. Best conditions insight
    top20 = both.nlargest(20, 'workout_quality')
    if len(top20) >= 10:
        text = (f"Your top 20 workout days averaged: recovery {top20['recovery_score'].mean():.0f}%, "
               f"HRV {top20['hrv_rmssd'].mean():.0f}ms, sleep {top20['sleep_hours'].mean():.1f} hrs, "
               f"sleep score {top20['sleep_score'].mean():.0f}%.")
        insights.append(("best_conditions", text, "medium", 20))

    # 6. Trend insight (FTP)
    ftp_rows = pd.read_sql("SELECT test_date, ftp_watts FROM ftp_history ORDER BY test_date", conn)
    if len(ftp_rows) >= 2:
        first, last = ftp_rows.iloc[0], ftp_rows.iloc[-1]
        days_elapsed = (last['test_date'] - first['test_date']).days
        gain = last['ftp_watts'] - first['ftp_watts']
        rate = gain / max(1, days_elapsed / 7)
        target_gap = 300 - last['ftp_watts']
        weeks_left = max(1, (date(2026, 12, 31) - date.today()).days / 7)
        needed = target_gap / weeks_left
        text = (f"FTP has moved from {first['ftp_watts']}W to {last['ftp_watts']}W "
               f"({gain:+d}W, {rate:+.2f}W/week). Need {needed:.2f}W/week to reach 300W by end of 2026.")
        insights.append(("ftp_trend", text, "high", len(ftp_rows)))

    # Store insights
    cur.execute("DELETE FROM training_insights WHERE date = %s", (today,))
    for itype, text, conf, n_pts in insights:
        cur.execute("""
            INSERT INTO training_insights (date, insight_type, insight_text, confidence, data_points)
            VALUES (%s, %s, %s, %s, %s)
        """, (today, itype, text, conf, n_pts))
    conn.commit()

    # Display
    print("═" * 55)
    print("    💡 TRAINING INSIGHTS")
    print("═" * 55)
    print(f"\n  Generated {len(insights)} insights from your data:\n")
    for i, (itype, text, conf, n_pts) in enumerate(insights, 1):
        conf_emoji = "🟢" if conf == "high" else ("🟡" if conf == "medium" else "🔴")
        print(f"  {i}. [{itype}] {conf_emoji} {conf} confidence")
        # Word wrap the text
        words = text.split()
        line = "     "
        for w in words:
            if len(line) + len(w) + 1 > 55:
                print(line)
                line = "     " + w
            else:
                line += " " + w if line.strip() else "     " + w
        if line.strip():
            print(line)
        print()

    print("═" * 55)
    cur.close()
    conn.close()
    return insights


def get_top_insight():
    """Get the most recent top insight for status display."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT insight_type, insight_text, confidence 
            FROM training_insights 
            ORDER BY created_at DESC LIMIT 1
        """)
        row = cur.fetchone()
        cur.close(); conn.close()
        return row
    except Exception:
        return None


# ── Phase 5: Vatternrundan Race Prep ──────────────────────────────

RACE_DATE = date(2026, 6, 13)  # Start 03:20 AM June 13
RACE_DISTANCE_KM = 315
RACE_TARGET_HOURS = 10.0
RACE_TARGET_AVG_KPH = 31.5

VATTERN_SEGMENTS = [
    {"name": "Motala -> Karlsborg (East shore)", "km_start": 0, "km_end": 50, "terrain": "Rolling", "notes": "Start discipline! Stay easy."},
    {"name": "Karlsborg -> Hjo (West shore)", "km_start": 50, "km_end": 120, "terrain": "Some hills", "notes": "Find rhythm, don't chase."},
    {"name": "Hjo -> Jonkoping (Southern tip)", "km_start": 120, "km_end": 170, "terrain": "Exposed/wind", "notes": "Wind exposed. Stay aero, draft."},
    {"name": "Jonkoping -> Granna (East shore)", "km_start": 170, "km_end": 231, "terrain": "Climbing", "notes": "Granna hill is notable. Cap climbs at 72% FTP."},
    {"name": "Granna -> Motala (Final stretch)", "km_start": 231, "km_end": 315, "terrain": "Mixed", "notes": "Fatigue management. Push if legs are good."},
]

# Race start: 03:20 AM June 13, 2026
RACE_START_TIME = "03:20"

# Max 4 stops. Jönköping (~km 170 / mile 106) is the hot food stop.
# Fueling: Formula 369 in bottles (2 cages + 1 flexible), gels to supplement, pickles/bread/blueberry soup at stops
REST_STOPS = [
    {"km": 80, "mi": 50, "name": "Stop 1", "action": "Refill F369 bottles. Pickles + bread. 3-5 min max."},
    {"km": 170, "mi": 106, "name": "Jönköping (hot food)", "action": "Hot meal (meatballs/mashed). Blueberry soup. Refill bottles. 10-15 min."},
    {"km": 240, "mi": 149, "name": "Stop 3", "action": "Refill F369 bottles. Pickles + bread. Quick gel. 3-5 min."},
    {"km": 290, "mi": 180, "name": "Stop 4", "action": "Last refill. Gel + whatever looks good. Push to finish."},
]


def _get_current_ftp():
    """Get current FTP from ftp_history."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT ftp_watts, test_date FROM ftp_history ORDER BY test_date DESC LIMIT 1")
    row = cur.fetchone()
    cur.close(); conn.close()
    if row:
        return row[0], row[1]
    return 263, date.today()


def _project_ftp_at_race():
    """Project FTP at race day using linear trend."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT test_date, ftp_watts FROM ftp_history ORDER BY test_date")
    rows = cur.fetchall()
    cur.close(); conn.close()
    if not rows:
        return 263
    current_ftp = rows[-1][1]
    target_ftp = 300
    target_date = date(2026, 12, 31)
    weeks_to_target = max(1, (target_date - date.today()).days / 7)
    weekly_gain = (target_ftp - current_ftp) / weeks_to_target
    weeks_to_race = max(0, (RACE_DATE - date.today()).days / 7)
    return round(current_ftp + weekly_gain * weeks_to_race)


def _get_current_pmc():
    """Get latest CTL/ATL/TSB."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT ctl, atl, tsb, date FROM training_load ORDER BY date DESC LIMIT 1")
    row = cur.fetchone()
    cur.close(); conn.close()
    if row:
        return {"ctl": float(row[0]), "atl": float(row[1]), "tsb": float(row[2]), "date": row[3]}
    return {"ctl": 0, "atl": 0, "tsb": 0, "date": date.today()}


def cmd_race_plan():
    """Vatternrundan pacing strategy."""
    current_ftp, ftp_date = _get_current_ftp()
    projected_ftp = _project_ftp_at_race()
    days_to_race = (RACE_DATE - date.today()).days

    print("=" * 60)
    print("    🏁 VATTERNRUNDAN RACE PLAN")
    print(f"    {RACE_DATE} | Start 03:20 AM | {RACE_DISTANCE_KM}km / 196mi | Target: sub-{RACE_TARGET_HOURS:.0f} hours")
    print("=" * 60)
    print(f"\n  Days to race: {days_to_race}")
    print(f"  Current FTP: {current_ftp}W (as of {ftp_date})")
    print(f"  Projected FTP at race: ~{projected_ftp}W")
    print(f"  Required avg speed: {RACE_TARGET_AVG_KPH} kph")

    for ftp_label, ftp_val in [("Current FTP", current_ftp), ("Projected race-day FTP", projected_ftp)]:
        print(f"\n{'─'*60}")
        print(f"  📊 PACING @ {ftp_label}: {ftp_val}W")
        print(f"{'─'*60}")

        # Overall target NP range
        np_low = round(ftp_val * 0.55)
        np_high = round(ftp_val * 0.63)
        print(f"  Target NP range: {np_low}-{np_high}W (55-63% FTP)")
        print(f"  Climb cap: {round(ftp_val * 0.72)}W (72% FTP) | Hard limit: {round(ftp_val * 0.75)}W (75%)")

        # Segment breakdown
        print(f"\n  {'Segment':<38} {'Km':>7} {'%FTP':>7} {'Watts':>7}")
        print("  " + "-" * 62)

        segments_pacing = [
            ("First 100km (discipline!)", "0-100", 0.57, 0.60),
            ("Km 100-230 (rhythm)", "100-230", 0.60, 0.65),
            ("Km 230-315 (finish strong)", "230-315", 0.65, 0.70),
        ]
        for seg_name, km_range, pct_low, pct_high in segments_pacing:
            w_low = round(ftp_val * pct_low)
            w_high = round(ftp_val * pct_high)
            pct_str = f"{pct_low*100:.0f}-{pct_high*100:.0f}%"
            print(f"  {seg_name:<38} {km_range:>7} {pct_str:>7} {w_low}-{w_high:>3}W")

        # Flat/climb targets
        flat_low = round(ftp_val * 0.57)
        flat_high = round(ftp_val * 0.62)
        climb_max = round(ftp_val * 0.72)
        print(f"\n  Flat sections: {flat_low}-{flat_high}W (57-62% FTP)")
        print(f"  Climbs: up to {climb_max}W (72% FTP max)")

        # Estimated TSS
        # TSS = (duration_seconds * NP * IF) / (FTP * 3600) * 100
        # For ~10 hours at ~59% FTP: IF = 0.59, NP = 0.59*FTP
        est_if = 0.59
        est_duration_hrs = RACE_TARGET_HOURS
        est_tss = round(est_if * est_if * est_duration_hrs * 100)
        print(f"\n  Estimated TSS: ~{est_tss} (IF ~{est_if:.2f} over {est_duration_hrs:.0f}hrs)")
        est_time_h = int(RACE_DISTANCE_KM / RACE_TARGET_AVG_KPH)
        est_time_m = int((RACE_DISTANCE_KM / RACE_TARGET_AVG_KPH - est_time_h) * 60)
        print(f"  Estimated ride time: ~{est_time_h}h{est_time_m:02d}m (at {RACE_TARGET_AVG_KPH} kph avg)")

    # Course profile
    print(f"\n{'─'*60}")
    print("  🗺️  COURSE SEGMENTS")
    print(f"{'─'*60}")
    for seg in VATTERN_SEGMENTS:
        dist = seg["km_end"] - seg["km_start"]
        print(f"\n  Km {seg['km_start']}-{seg['km_end']} ({dist}km): {seg['name']}")
        print(f"    Terrain: {seg['terrain']}")
        print(f"    Strategy: {seg['notes']}")

    # Rest stop strategy (max 4 stops)
    print(f"\n{'─'*60}")
    print("  🍌 REST STOP / FUELING STRATEGY (4 stops max)")
    print(f"{'─'*60}")
    print("  Start: 03:20 AM, June 13. Riding with a friend.")
    print("")
    print("  PRIMARY FUEL: Formula 369 (30g carbs/scoop, 1:1 glucose:fructose)")
    print("    3 bottles: 2 in cages + 1 flexible rubber bottle")
    print("    Mix: 2-3 scoops per bottle (60-90g carbs per bottle)")
    print("    Target: 80-90g carbs/hour (bottle + gel every 45-60 min)")
    print("  SUPPLEMENT: Gels as needed between bottles")
    print("  AT STOPS: Pickles (sodium), bread buns, blueberry soup")
    print("  Jönköping: Hot meal (Swedish meatballs, mashed potatoes)")
    print("  Hydration: 16-24 oz/hour depending on temp.")
    print(f"\n  {'Stop':>6}  {'Mile':>5}  {'Elapsed':>8}  {'Clock':>7}  {'Action'}")
    print("  " + "-" * 72)
    for stop in REST_STOPS:
        est_hrs = stop["km"] / RACE_TARGET_AVG_KPH
        h = int(est_hrs)
        m = int((est_hrs - h) * 60)
        # Clock time based on 03:20 start
        clock_h = (3 + h + (20 + m) // 60) % 24
        clock_m = (20 + m) % 60
        am_pm = "AM" if clock_h < 12 else "PM"
        clock_h_12 = clock_h if clock_h <= 12 else clock_h - 12
        if clock_h_12 == 0:
            clock_h_12 = 12
        print(f"  {stop['name']:>20}  {stop['mi']:>3}mi  {h}h{m:02d}m  {clock_h_12}:{clock_m:02d}{am_pm}  {stop['action']}")

    print(f"\n  Dark section: Start through ~mi 30 (03:20-05:00 AM, pre-dawn)")
    print("  Sunrise ~3:30 AM in June Sweden. Lights mandatory at start.")
    print("  Keep eating from the gun! Don't wait until you're hungry.")

    # 2025 race reference
    print(f"\n{'─'*60}")
    print("  📋 2025 VÄTTERNRUNDAN REFERENCE (June 14, solo)")
    print(f"{'─'*60}")
    print("  Time: 10h09m | Distance: 196 mi | TSS: 682 | IF: 0.82")
    print("  NP: 215W | Avg: 192W | Avg HR: 142 | Max HR: 168")
    print("  Elevation: 5,850 ft | Calories: 7,002 | Cadence: 84")
    print("  Feeling: 1 (great) | PRs: 4")
    print("\n" + "=" * 60)


def cmd_race_weather():
    """Weather forecast for Motala, Sweden (race location)."""
    days_to_race = (RACE_DATE - date.today()).days

    print("=" * 60)
    print("    🌤️  VATTERNRUNDAN WEATHER — Motala, Sweden")
    print(f"    Race date: {RACE_DATE} ({days_to_race} days away)")
    print("=" * 60)

    if days_to_race > 14:
        print(f"\n  ⚠️  Race is {days_to_race} days away. Detailed forecasts not available yet.")
        print(f"\n  📊 JUNE CLIMATE AVERAGES FOR MOTALA:")
        print(f"    Temperature: 55-70F (13-21C)")
        print(f"    Overnight lows: 45-55F (7-13C)")
        print(f"    Precipitation: ~50mm for June (moderate)")
        print(f"    Daylight: ~18 hours (sunrise ~3:30am, sunset ~10:00pm)")
        print(f"    Wind: Variable, lake effect. West/SW common.")
        print(f"\n  🌅 Race timing:")
        print(f"    Start: Saturday late afternoon/evening")
        print(f"    Night section: ~10pm - 4am (short Nordic night)")
        print(f"    Finish: Sunday morning/midday")
        print(f"\n  👕 LIKELY KIT (based on June averages):")
        print(f"    Start (afternoon, ~65F): Bib shorts, short sleeve jersey, arm warmers in pocket")
        print(f"    Evening (55-60F): Add arm warmers, vest")
        print(f"    Night (45-55F): Long sleeve jersey, knee warmers, vest, REFLECTIVE VEST + LIGHTS")
        print(f"    Morning (50-60F): Shed layers as sun rises")
        print(f"\n  ⚡ MANDATORY for night section:")
        print(f"    - Reflective vest")
        print(f"    - Front + rear lights")
        print(f"    - Extra layer for 3am temp drop")
    else:
        # Fetch actual forecast from yr.no
        try:
            resp = requests.get(
                "https://api.met.no/weatherapi/locationforecast/2.0/compact?lat=58.537&lon=15.047",
                headers={"User-Agent": "AuriWren/1.0 auri@auri.email"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"\n  ❌ Weather fetch failed: {e}")
            print("  Falling back to climate averages (see above).")
            return

        timeseries = data.get("properties", {}).get("timeseries", [])
        if not timeseries:
            print("\n  ❌ No forecast data available.")
            return

        # Show next 48 hours of forecast
        print(f"\n  📅 CURRENT FORECAST (yr.no):")
        print(f"\n  {'Time (UTC)':<18} {'Temp':>6} {'Wind':>6} {'Precip':>7} {'Conditions'}")
        print("  " + "-" * 55)
        shown = 0
        for entry in timeseries[:48]:
            t = entry.get("time", "")
            instant = entry.get("data", {}).get("instant", {}).get("details", {})
            temp_c = instant.get("air_temperature", 0)
            temp_f = c_to_f(temp_c)
            wind_mps = instant.get("wind_speed", 0)
            wind_mph = round(wind_mps * 2.237)

            next1h = entry.get("data", {}).get("next_1_hours", {})
            precip = next1h.get("details", {}).get("precipitation_amount", 0)
            symbol = next1h.get("summary", {}).get("symbol_code", "")

            # Only show every 3 hours
            hour = t[11:13] if len(t) > 13 else ""
            if hour and int(hour) % 3 != 0:
                continue

            time_str = t[:16].replace("T", " ")
            precip_str = f"{precip:.1f}mm" if precip > 0 else "dry"
            print(f"  {time_str:<18} {temp_f:>4}F {wind_mph:>4}mph {precip_str:>7} {symbol}")
            shown += 1
            if shown >= 16:
                break

        # Kit recommendation based on forecast temps
        temps = [c_to_f(e.get("data", {}).get("instant", {}).get("details", {}).get("air_temperature", 15))
                 for e in timeseries[:24]]
        has_rain = any(e.get("data", {}).get("next_1_hours", {}).get("details", {}).get("precipitation_amount", 0) > 0.5
                      for e in timeseries[:24])
        min_temp = min(temps) if temps else 55
        max_temp = max(temps) if temps else 65
        avg_temp = sum(temps) / len(temps) if temps else 60

        print(f"\n  📊 24hr range: {min_temp}F - {max_temp}F (avg {avg_temp:.0f}F)")
        if has_rain:
            print("  🌧️  Rain expected!")

        print(f"\n  👕 KIT RECOMMENDATION:")
        if min_temp < 45 or has_rain:
            print("    Bib tights, long sleeve jersey, rain jacket, full gloves, shoe covers")
        elif min_temp < 55:
            print("    Bib shorts, long sleeve jersey, knee warmers, vest")
        elif min_temp < 65:
            print("    Bib shorts, short sleeve + arm warmers, vest in pocket")
        else:
            print("    Bib shorts, short sleeve jersey, arm coolers")

        print(f"\n  ⚡ Night section (always pack):")
        print(f"    - Reflective vest + lights (mandatory)")
        print(f"    - Extra thermal layer for overnight temp drop")

    print("\n" + "=" * 60)


def cmd_taper():
    """Taper protocol for Vatternrundan."""
    days_to_race = (RACE_DATE - date.today()).days
    weeks_to_race = days_to_race / 7.0
    current_ftp, _ = _get_current_ftp()
    pmc = _get_current_pmc()

    print("=" * 60)
    print("    📉 VATTERNRUNDAN TAPER PROTOCOL")
    print(f"    Race: {RACE_DATE} | {days_to_race} days / {weeks_to_race:.1f} weeks away")
    print("=" * 60)

    print(f"\n  Current PMC ({pmc['date']}):")
    print(f"    CTL (fitness): {pmc['ctl']:.1f}")
    print(f"    ATL (fatigue): {pmc['atl']:.1f}")
    print(f"    TSB (form):    {pmc['tsb']:+.1f}")

    # Project TSB at race day
    # If current training load continues, CTL and ATL decay toward 0 without new TSS
    # For projection: assume avg daily TSS from recent CTL
    avg_daily_tss = pmc['ctl']  # CTL approximates avg daily TSS
    ctl_proj = pmc['ctl']
    atl_proj = pmc['atl']
    for d in range(days_to_race):
        if days_to_race - d > 14:
            tss = avg_daily_tss  # Normal training
        elif days_to_race - d > 7:
            tss = avg_daily_tss * 0.7  # Week -2: -30%
        elif days_to_race - d > 2:
            tss = avg_daily_tss * 0.5  # Week -1: -50%
        else:
            tss = 15  # Easy spins
        ctl_proj = ctl_proj + (tss - ctl_proj) / 42.0
        atl_proj = atl_proj + (tss - atl_proj) / 7.0
    tsb_proj = ctl_proj - atl_proj

    print(f"\n  📅 PROJECTED PMC AT RACE DAY (with taper):")
    print(f"    CTL: ~{ctl_proj:.1f}")
    print(f"    ATL: ~{atl_proj:.1f}")
    print(f"    TSB: ~{tsb_proj:+.1f}")
    target_tsb_ok = 15 <= tsb_proj <= 25
    print(f"    Target TSB: +15 to +25 {'✅' if target_tsb_ok else '⚠️ adjust taper'}")

    # Training phase
    print(f"\n{'─'*60}")
    if days_to_race > 84:  # >12 weeks
        phase = "BASE"
        desc = "Build aerobic engine. Long endurance rides, zone 2."
        print(f"  🔵 CURRENT PHASE: {phase}")
        print(f"     {desc}")
    elif days_to_race > 42:  # 6-12 weeks
        phase = "BUILD"
        desc = "Add intensity. Sweet spot, threshold intervals. Increase TSS."
        print(f"  🟡 CURRENT PHASE: {phase}")
        print(f"     {desc}")
    elif days_to_race > 14:  # 2-6 weeks
        phase = "PEAK"
        desc = "Highest training load. Race-specific long rides. Simulate race fueling."
        print(f"  🟠 CURRENT PHASE: {phase}")
        print(f"     {desc}")
    else:
        phase = "TAPER"
        print(f"  🟢 CURRENT PHASE: {phase} -- Race is imminent!")

    if days_to_race <= 14:
        print(f"\n{'─'*60}")
        print("  📋 TAPER PROTOCOL (you're in the taper window!)")
        print(f"{'─'*60}")

        taper_start = RACE_DATE - timedelta(days=14)
        week2_end = RACE_DATE - timedelta(days=7)
        opener_day = RACE_DATE - timedelta(days=3)

        print(f"\n  Week -2 ({taper_start} to {week2_end}):")
        print(f"    Volume: -30% of normal")
        print(f"    Intensity: 2 short sweet spot sessions")
        print(f"    Example: 2x20min @ {round(current_ftp * 0.88)}-{round(current_ftp * 0.93)}W")

        print(f"\n  Week -1 ({week2_end} to {RACE_DATE}):")
        print(f"    Volume: -50% of normal")
        print(f"    One opener session on {opener_day}:")
        print(f"      Warmup 15min, then 4x1min @ {round(current_ftp * 1.15)}-{round(current_ftp * 1.20)}W (VO2max)")
        print(f"      Full recovery between intervals")

        print(f"\n  Days -2 to -1 ({RACE_DATE - timedelta(days=2)} - {RACE_DATE - timedelta(days=1)}):")
        print(f"    Easy spins only: 30-45 min at {round(current_ftp * 0.45)}-{round(current_ftp * 0.55)}W")
        print(f"    Focus: hydration, carb loading, sleep")
    else:
        print(f"\n{'─'*60}")
        print("  📋 TAPER TIMELINE")
        print(f"{'─'*60}")
        print(f"    Taper starts: {RACE_DATE - timedelta(days=14)} ({days_to_race - 14} days from now)")
        print(f"    Peak phase until then: keep building fitness")
        print(f"    Last hard week: {RACE_DATE - timedelta(days=21)} to {RACE_DATE - timedelta(days=15)}")

    # Key dates
    print(f"\n{'─'*60}")
    print("  📅 KEY DATES")
    print(f"{'─'*60}")
    key_dates = [
        (RACE_DATE - timedelta(days=42), "Build phase starts (6 weeks out)"),
        (RACE_DATE - timedelta(days=21), "Peak week (3 weeks out)"),
        (RACE_DATE - timedelta(days=14), "Taper begins"),
        (RACE_DATE - timedelta(days=7), "Final taper week"),
        (RACE_DATE - timedelta(days=3), "Opener session"),
        (RACE_DATE - timedelta(days=1), "Easy spin + prep"),
        (RACE_DATE, "🏁 RACE DAY"),
    ]
    for kd, desc in key_dates:
        delta = (kd - date.today()).days
        marker = " ◀ TODAY" if delta == 0 else (f" ({delta}d away)" if delta > 0 else f" ({-delta}d ago)")
        print(f"    {kd}  {desc}{marker}")

    print("\n" + "=" * 60)


def cmd_race_countdown():
    """Combined race dashboard: plan + weather + taper + FTP projection."""
    days_to_race = (RACE_DATE - date.today()).days
    current_ftp, ftp_date = _get_current_ftp()
    projected_ftp = _project_ftp_at_race()
    pmc = _get_current_pmc()

    print("=" * 60)
    print("    🏁 VATTERNRUNDAN RACE COUNTDOWN")
    print(f"    {RACE_DATE} | {days_to_race} days to go")
    print("=" * 60)

    # FTP summary
    print(f"\n  ⚡ FTP: {current_ftp}W now -> ~{projected_ftp}W projected at race")
    target_np_now = f"{round(current_ftp * 0.55)}-{round(current_ftp * 0.63)}W"
    target_np_proj = f"{round(projected_ftp * 0.55)}-{round(projected_ftp * 0.63)}W"
    print(f"  Target NP: {target_np_now} (current) | {target_np_proj} (projected)")

    # PMC summary
    print(f"\n  📊 Fitness: CTL {pmc['ctl']:.1f} | Fatigue: ATL {pmc['atl']:.1f} | Form: TSB {pmc['tsb']:+.1f}")

    # Training phase
    if days_to_race > 84:
        phase = "🔵 BASE"
    elif days_to_race > 42:
        phase = "🟡 BUILD"
    elif days_to_race > 14:
        phase = "🟠 PEAK"
    else:
        phase = "🟢 TAPER"
    print(f"  Phase: {phase}")

    # Pacing summary
    print(f"\n{'─'*60}")
    print("  🏁 PACING SUMMARY")
    print(f"{'─'*60}")
    print(f"  First 100km: max {round(current_ftp * 0.60)}W ({round(projected_ftp * 0.60)}W projected)")
    print(f"  Km 100-230:  {round(current_ftp * 0.60)}-{round(current_ftp * 0.65)}W")
    print(f"  Km 230-315:  {round(current_ftp * 0.65)}-{round(current_ftp * 0.70)}W")
    print(f"  Climb cap:   {round(current_ftp * 0.72)}W")
    est_tss = round(0.59 * 0.59 * RACE_TARGET_HOURS * 100)
    print(f"  Est. TSS: ~{est_tss} | Est. time: ~{RACE_DISTANCE_KM / RACE_TARGET_AVG_KPH:.1f}hrs")

    # Weather preview
    print(f"\n{'─'*60}")
    print("  🌤️  WEATHER PREVIEW")
    print(f"{'─'*60}")
    if days_to_race > 14:
        print(f"  Forecast available in {days_to_race - 14} days.")
        print(f"  June averages: 55-70F, overnight lows 45-55F, ~18hrs daylight")
    else:
        try:
            resp = requests.get(
                "https://api.met.no/weatherapi/locationforecast/2.0/compact?lat=58.537&lon=15.047",
                headers={"User-Agent": "AuriWren/1.0 auri@auri.email"},
                timeout=10,
            )
            resp.raise_for_status()
            ts = resp.json().get("properties", {}).get("timeseries", [])
            if ts:
                temps = [c_to_f(e["data"]["instant"]["details"].get("air_temperature", 15)) for e in ts[:24]]
                print(f"  Next 24h: {min(temps)}F - {max(temps)}F")
        except Exception:
            print(f"  Weather fetch failed. Run `cycling-training race-weather` for details.")

    # Taper status
    print(f"\n{'─'*60}")
    print("  📉 TAPER STATUS")
    print(f"{'─'*60}")
    if days_to_race <= 14:
        print(f"  🟢 IN TAPER WINDOW")
        if days_to_race > 7:
            print(f"  Week -2: Volume -30%. 2 short sweet spot sessions.")
        elif days_to_race > 2:
            print(f"  Week -1: Volume -50%. Opener {RACE_DATE - timedelta(days=3)}.")
        else:
            print(f"  Final days: Easy spins only. Rest, hydrate, carb load.")
    else:
        print(f"  Taper starts: {RACE_DATE - timedelta(days=14)} ({days_to_race - 14} days from now)")

    # Countdown
    print(f"\n{'─'*60}")
    weeks = days_to_race // 7
    rem_days = days_to_race % 7
    print(f"  ⏱️  {weeks} weeks, {rem_days} days to Vatternrundan")
    print(f"     {RACE_DISTANCE_KM}km around Lake Vattern. Target: sub-{RACE_TARGET_HOURS:.0f} hours.")
    print(f"     You've got this. 💪")
    print("\n" + "=" * 60)


# ── CLI ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Cycling Training CLI")
    sub = parser.add_subparsers(dest="command")

    p_whoop = sub.add_parser("sync-whoop", help="Sync Whoop recovery data")
    p_whoop.add_argument("--days", type=int, default=7)

    p_tp = sub.add_parser("sync-tp", help="Sync TrainingPeaks workouts")
    p_tp.add_argument("--days", type=int, default=7)

    p_all = sub.add_parser("sync-all", help="Sync all sources")
    p_all.add_argument("--days", type=int, default=7)

    sub.add_parser("status", help="Show current status")

    sub.add_parser("pmc", help="Calculate CTL/ATL/TSB (Performance Management Chart)")

    p_post = sub.add_parser("post-ride", help="Post-ride analysis")
    p_post.add_argument("date", nargs="?", default=None, help="Date (YYYY-MM-DD, default: today)")

    sub.add_parser("ftp-project", help="FTP trajectory projection")

    p_weekly = sub.add_parser("weekly-summary", help="Weekly training summary")
    p_weekly.add_argument("date", nargs="?", default=None, help="Any date in target week (YYYY-MM-DD)")

    sub.add_parser("strava-events", help="Show upcoming Strava club events")

    p_weather = sub.add_parser("weather", help="Weather and ride kit recommendation")
    p_weather.add_argument("location", nargs="?", default="Brooklyn, NY", help="Location (default: Brooklyn, NY)")

    sub.add_parser("correlate", help="Recovery-training correlation analysis")
    sub.add_parser("trends", help="Long-term training trends")
    sub.add_parser("insights", help="Generate AI-driven training insights")

    sub.add_parser("race-plan", help="Vatternrundan pacing strategy")
    sub.add_parser("race-weather", help="Weather forecast for Motala (race location)")
    sub.add_parser("taper", help="Taper protocol for Vatternrundan")
    sub.add_parser("race-countdown", help="Combined race dashboard")

    args = parser.parse_args()

    if args.command == "sync-whoop":
        sync_whoop(args.days)
    elif args.command == "sync-tp":
        sync_tp(args.days)
    elif args.command == "sync-all":
        sync_all(args.days)
    elif args.command == "status":
        show_status()
    elif args.command == "pmc":
        calc_pmc()
    elif args.command == "post-ride":
        post_ride(args.date)
    elif args.command == "ftp-project":
        ftp_project()
    elif args.command == "weekly-summary":
        weekly_summary(args.date)
    elif args.command == "strava-events":
        strava_events()
    elif args.command == "weather":
        weather(args.location)
    elif args.command == "correlate":
        cmd_correlate()
    elif args.command == "trends":
        cmd_trends()
    elif args.command == "insights":
        cmd_insights()
    elif args.command == "race-plan":
        cmd_race_plan()
    elif args.command == "race-weather":
        cmd_race_weather()
    elif args.command == "taper":
        cmd_taper()
    elif args.command == "race-countdown":
        cmd_race_countdown()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
