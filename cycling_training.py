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


def weather(location="Brooklyn, NY"):
    """Show weather and ride kit recommendation."""
    loc_url = location.replace(" ", "+").replace(",", ",")
    try:
        resp = requests.get(f"https://wttr.in/{loc_url}?format=j1", timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"❌ Weather fetch failed: {e}")
        return

    current = data.get("current_condition", [{}])[0]
    temp_f = int(current.get("temp_F", 0))
    feels_f = int(current.get("FeelsLikeF", 0))
    wind_mph = int(current.get("windspeedMiles", 0))
    wind_dir = current.get("winddir16Point", "")
    desc = current.get("weatherDesc", [{}])[0].get("value", "")
    humidity = current.get("humidity", "")

    print("═" * 55)
    print(f"    🌤️  WEATHER — {location}")
    print("═" * 55)

    print(f"\n  NOW: {temp_f}F (feels like {feels_f}F)")
    print(f"  Conditions: {desc}")
    print(f"  Wind: {wind_mph} mph {wind_dir}")
    print(f"  Humidity: {humidity}%")

    # Kit recommendation based on feels-like
    print(f"\n  KIT: {get_kit_recommendation(feels_f)}")

    # Forecast
    forecasts = data.get("weather", [])
    if forecasts:
        print(f"\n  {'Date':<12} {'High':>5} {'Low':>5} {'Conditions'}")
        print("  " + "-" * 45)
        for day in forecasts[:3]:
            d = day.get("date", "")
            hi = day.get("maxtempF", "")
            lo = day.get("mintempF", "")
            cond = day.get("hourly", [{}])[4].get("weatherDesc", [{}])[0].get("value", "") if len(day.get("hourly", [])) > 4 else ""
            rideable = "✅" if int(hi) >= 30 else "❄️"
            print(f"  {d:<12} {hi:>4}F {lo:>4}F {cond} {rideable}")

    print("\n" + "═" * 55)


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
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
