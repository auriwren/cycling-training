"""
Dashboard generator module for cycling-training CLI.
Queries PostgreSQL and fills dashboard_template.html to produce dashboard.html.
"""

import html
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
import requests

from config import ConfigError, get_config, get_path

DB_CONN = ""
PROJECT_DIR = Path(".")
ATHLETE_NAME = "Athlete"
ATHLETE_FIRST = "Athlete"
COACH_NAME = "Coach"
COACH_FIRST = "Coach"
TEMPLATE_PATH = Path("dashboard_template.html")
OUTPUT_PATH = Path("dashboard.html")
FASTMAIL_ENV = Path(".")

HALVVATTERN_DATE = date.today()
VATTERNRUNDAN_DATE = date.today()
FTP_TARGET = 0
DEFAULT_FTP = 0
FASTMAIL_UPLOAD_URL = ""
FASTMAIL_UPLOAD_USER = ""
_CONFIG_LOADED = False
CONFIG = {}


def init_config() -> bool:
    global DB_CONN, PROJECT_DIR, ATHLETE_NAME, ATHLETE_FIRST, COACH_NAME, COACH_FIRST
    global TEMPLATE_PATH, OUTPUT_PATH, FASTMAIL_ENV, FASTMAIL_UPLOAD_URL, FASTMAIL_UPLOAD_USER
    global HALVVATTERN_DATE, VATTERNRUNDAN_DATE
    global FTP_TARGET, DEFAULT_FTP, _CONFIG_LOADED, CONFIG

    if _CONFIG_LOADED:
        return True

    try:
        config = get_config()
    except ConfigError as exc:
        print(f"❌ {exc}")
        return False

    dash_config = config.get("dashboard", {})
    DB_CONN = os.environ.get("CT_DB_CONN", config["database"]["connection"])
    PROJECT_DIR = Path(
        os.environ.get("CT_PROJECT_DIR", get_path(dash_config.get("project_dir", ".")))
    )
    ATHLETE_NAME = os.environ.get("CT_ATHLETE_NAME", dash_config.get("athlete_name", "Athlete"))
    ATHLETE_FIRST = os.environ.get("CT_ATHLETE_FIRST", ATHLETE_NAME.split()[0])
    COACH_NAME = os.environ.get("CT_COACH_NAME", dash_config.get("coach_name", "Coach"))
    COACH_FIRST = os.environ.get("CT_COACH_FIRST", COACH_NAME.split()[0])
    TEMPLATE_PATH = PROJECT_DIR / dash_config.get("template_path", "dashboard_template.html")
    OUTPUT_PATH = PROJECT_DIR / dash_config.get("output_path", "dashboard.html")
    FASTMAIL_ENV = get_path(config["credentials"]["fastmail_env"])
    FASTMAIL_UPLOAD_URL = dash_config.get("upload_url", "")
    FASTMAIL_UPLOAD_USER = dash_config.get("upload_user", "")

    HALVVATTERN_DATE = date.fromisoformat(config["race"]["halvvattern_date"])
    VATTERNRUNDAN_DATE = date.fromisoformat(config["race"]["race_date"])
    FTP_TARGET = config["ftp"]["target_ftp"]
    DEFAULT_FTP = config["ftp"]["default_ftp"]

    CONFIG = config
    _CONFIG_LOADED = True
    return True


def _db():
    return psycopg2.connect(DB_CONN)


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def load_env(path: Path) -> Dict[str, str]:
    env: Dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"')
    return env


def _f(val, decimals=1):
    """Format a number, return 'N/A' if None."""
    if val is None:
        return "N/A"
    return f"{float(val):.{decimals}f}"


def _classify_zone(title, if_actual=None):
    """Classify workout into power zone by title, fallback to IF."""
    t = (title or "").lower()
    if any(k in t for k in ["threshold", "vo2", "anaerobic", "over-under", "over/under"]):
        return "Threshold/VO2"
    if "sweetspot" in t or "sweet spot" in t:
        return "Sweetspot"
    if "tempo" in t:
        return "Tempo"
    if any(k in t for k in ["endurance", "easy", "recovery", "zone 2", "z2"]):
        return "Endurance"
    if any(k in t for k in ["free ride", "unstructured", "outdoor", "group"]):
        return "Free Ride"
    # Fallback to IF ranges
    if if_actual is not None:
        ifa = float(if_actual)
        if ifa >= 0.91:
            return "Threshold/VO2"
        if ifa >= 0.84:
            return "Sweetspot"
        if ifa >= 0.76:
            return "Tempo"
        if ifa >= 0.56:
            return "Endurance"
    return "Other High Int."


def generate_dashboard(upload: bool = False) -> None:
    """Generate the HTML dashboard from template + database."""
    if not init_config():
        return

    today = date.today()
    now = datetime.now()

    conn = _db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # ── Core metrics ──
    cur.execute("SELECT ftp_watts, test_date FROM ftp_history ORDER BY test_date DESC LIMIT 1")
    ftp_row = cur.fetchone()
    ftp = int(ftp_row["ftp_watts"]) if ftp_row else DEFAULT_FTP

    cur.execute("SELECT ctl, atl, tsb, date FROM training_load ORDER BY date DESC LIMIT 1")
    load = cur.fetchone()
    ctl = float(load["ctl"]) if load else 0
    atl = float(load["atl"]) if load else 0
    tsb = float(load["tsb"]) if load else 0

    halv_days = (HALVVATTERN_DATE - today).days
    vatt_days = (VATTERNRUNDAN_DATE - today).days

    # ── This Week ──
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)

    cur.execute("""
        SELECT * FROM training_workouts
        WHERE date BETWEEN %s AND %s ORDER BY date
    """, (monday, sunday))
    week_workouts = cur.fetchall()

    # Weekly aggregates
    completed_wk = [w for w in week_workouts if w["completed"]]
    planned_wk = [w for w in week_workouts if w["tss_planned"] and float(w["tss_planned"]) > 0]
    tss_actual_wk = sum(float(w["tss_actual"] or 0) for w in week_workouts)
    tss_planned_wk = sum(float(w["tss_planned"] or 0) for w in week_workouts)
    progress_wk = int(tss_actual_wk / tss_planned_wk * 100) if tss_planned_wk > 0 else 0

    hours_wk = sum(float(w["duration_actual_min"] or w["duration_planned_min"] or 0) for w in completed_wk) / 60
    ifs_wk = [float(w["if_actual"]) for w in completed_wk if w["if_actual"]]
    avg_if_wk = sum(ifs_wk) / len(ifs_wk) if ifs_wk else 0
    peak_if_wk = max(ifs_wk) if ifs_wk else 0

    # Recovery this week
    cur.execute("""
        SELECT recovery_score, hrv_rmssd, sleep_duration_min
        FROM whoop_recovery WHERE date BETWEEN %s AND %s
    """, (monday, sunday))
    week_rec = cur.fetchall()
    avg_rec_wk = sum(float(r["recovery_score"] or 0) for r in week_rec) / len(week_rec) if week_rec else 0
    avg_hrv_wk = sum(float(r["hrv_rmssd"] or 0) for r in week_rec) / len(week_rec) if week_rec else 0
    avg_sleep_wk = sum(float(r["sleep_duration_min"] or 0) / 60 for r in week_rec) / len(week_rec) if week_rec else 0

    # Today's focus
    cur.execute("SELECT * FROM training_workouts WHERE date = %s", (today,))
    today_workouts = cur.fetchall()
    # Next upcoming workout
    cur.execute("""
        SELECT * FROM training_workouts
        WHERE date > %s AND tss_planned > 0
        ORDER BY date LIMIT 1
    """, (today,))
    next_workout = cur.fetchone()

    if today_workouts and any(w["tss_planned"] and float(w["tss_planned"]) > 0 for w in today_workouts):
        tw = [w for w in today_workouts if w["tss_planned"] and float(w["tss_planned"]) > 0][0]
        tw_title = _escape(tw.get("title") or "")
        if tw["completed"]:
            focus_title = f"Today's Focus: {tw_title} ✅ Done"
            focus_icon = "✅"
        else:
            focus_title = f"Today's Focus: {tw_title}"
            focus_icon = "🚴"
        focus_detail = f"TSS {int(float(tw['tss_planned']))} planned"
        if next_workout and next_workout["date"] != today:
            days_name = next_workout["date"].strftime("%A")
            next_title = _escape(next_workout.get("title") or "")
            focus_detail += f" | Next: {days_name} {next_title}"
    else:
        focus_title = "Today's Focus: Rest Day"
        focus_icon = "😴"
        if next_workout:
            days_name = next_workout["date"].strftime("%A")
            next_title = _escape(next_workout.get("title") or "")
            np_str = f" (TSS {int(float(next_workout['tss_planned']))} planned)" if next_workout["tss_planned"] else ""
            focus_detail = f"Next workout: {days_name} {next_title}{np_str}"
        else:
            focus_detail = "No upcoming workouts scheduled"

    # Build workout table rows
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    workout_rows = []
    for i in range(7):
        d = monday + timedelta(days=i)
        day_name = day_names[i]
        day_workouts = [w for w in week_workouts if w["date"] == d]

        if not day_workouts or all(not w["tss_planned"] or float(w["tss_planned"] or 0) == 0 for w in day_workouts):
            workout_rows.append(f'            <tr><td>{day_name}</td><td colspan="4" style="color:var(--text-muted)">Day Off</td></tr>')
        else:
            for w in day_workouts:
                if not w["tss_planned"] or float(w["tss_planned"] or 0) == 0:
                    continue
                title_safe = _escape(w.get("title") or "")
                if w["completed"]:
                    tss_a = float(w["tss_actual"] or 0)
                    np_a = f"{int(float(w['np_actual']))}W" if w["np_actual"] else "—"
                    q = float(w["workout_quality"]) if w["workout_quality"] else None
                    if q:
                        badge_class = "badge-green" if q >= 80 else ("badge-yellow" if q >= 60 else "badge-red")
                        q_str = f'<span class="badge {badge_class}">{int(q)}</span>'
                    else:
                        q_str = "—"
                    workout_rows.append(f'            <tr><td>{day_name}</td><td>{title_safe}</td><td>{tss_a:.1f}</td><td>{np_a}</td><td>{q_str}</td></tr>')
                else:
                    tss_p = int(float(w["tss_planned"])) if w["tss_planned"] else "?"
                    if d >= today:
                        workout_rows.append(f'            <tr style="opacity:0.6"><td>{day_name}</td><td>{title_safe}</td><td colspan="2" style="color:var(--text-muted)">TSS {tss_p} planned</td><td><span class="badge" style="background:var(--surface-elevated);color:var(--text-muted)">Upcoming</span></td></tr>')
                    else:
                        workout_rows.append(f'            <tr style="opacity:0.5"><td>{day_name}</td><td>{title_safe}</td><td colspan="2" style="color:var(--text-muted)">Missed (TSS {tss_p})</td><td><span class="badge badge-red">Missed</span></td></tr>')

    # CTL change this week
    cur.execute("SELECT ctl FROM training_load WHERE date = %s", (monday,))
    ctl_monday = cur.fetchone()
    ctl_start = float(ctl_monday["ctl"]) if ctl_monday else ctl
    ctl_change = ctl - ctl_start
    ctl_arrow = "&uarr;" if ctl_change > 0 else ("&darr;" if ctl_change < 0 else "&rarr;")
    ctl_color = "var(--green-light)" if ctl_change > 0 else ("var(--red-light)" if ctl_change < 0 else "var(--text-secondary)")
    tsb_desc = "Fresh, ready to load" if tsb > 0 else ("Absorbing load well" if tsb > -15 else "Heavy loading")

    week_pmc_text = f'''CTL: {ctl:.1f} ({ctl_change:+.1f} this week) <span style="color:{ctl_color}">{ctl_arrow}</span><br>
          ATL: {atl:.1f}<br>
          TSB: {tsb:.1f} &mdash; {tsb_desc}<br>
          <span style="color:var(--text-muted)">FTP: {ftp}W &rarr; Target {FTP_TARGET}W</span>'''

    rec_color = "green" if avg_rec_wk >= 67 else ("yellow" if avg_rec_wk >= 34 else "red")
    badge_map = {"green": "badge-green", "yellow": "badge-yellow", "red": "badge-red"}
    rec_label = rec_color.capitalize()
    week_recovery_text = f'''Avg Recovery: {avg_rec_wk:.0f}% <span class="badge {badge_map[rec_color]}">{rec_label}</span><br>
          Avg HRV: {avg_hrv_wk:.0f}ms<br>
          Avg Sleep: {avg_sleep_wk:.1f} hrs'''

    # ── Weekly TSS chart data ──
    cur.execute("""
        SELECT date_trunc('week', date)::date as week_start,
               COALESCE(SUM(tss_actual), 0) as tss
        FROM training_workouts
        WHERE date >= %s
        GROUP BY week_start ORDER BY week_start
    """, (today - timedelta(days=400),))
    weekly_tss_rows = cur.fetchall()

    # Check for flu/sick weeks from daily_performance annotations
    cur.execute("""
        SELECT date, notes FROM daily_performance
        WHERE notes IS NOT NULL AND (lower(notes) LIKE '%%flu%%' OR lower(notes) LIKE '%%sick%%')
    """)
    sick_dates = set()
    for r in cur.fetchall():
        if r["date"]:
            week_start = r["date"] - timedelta(days=r["date"].weekday())
            sick_dates.add(week_start)

    # Also flag weeks with very low TSS after high weeks as potential sick weeks
    weekly_tss_js = []
    for r in weekly_tss_rows:
        ws = r["week_start"]
        tss_val = float(r["tss"])
        week_label = ws.strftime("%b %d") if ws else "?"
        is_flu = ws in sick_dates
        weekly_tss_js.append(f"{{week:'{week_label}',tss:{tss_val:.1f},flu:{'true' if is_flu else 'false'}}}")

    weekly_tss_data = "[\n  " + ",".join(weekly_tss_js) + "\n]"

    # ── PMC chart data ──
    cur.execute("""
        SELECT date, ctl, atl, tsb FROM training_load
        WHERE date >= %s ORDER BY date
    """, (today - timedelta(days=90),))
    pmc_rows = cur.fetchall()
    pmc_js = []
    for r in pmc_rows:
        d_str = r["date"].strftime("%b %d") if r["date"] else "?"
        pmc_js.append(f"{{d:'{d_str}',ctl:{float(r['ctl']):.1f},atl:{float(r['atl']):.1f},tsb:{float(r['tsb']):.1f}}}")
    pmc_data = "[\n  " + ",".join(pmc_js) + "\n]"

    # ── Recovery 30-day data ──
    cur.execute("""
        SELECT date, recovery_score, hrv_rmssd, sleep_duration_min
        FROM whoop_recovery
        WHERE date >= %s AND recovery_score IS NOT NULL
        ORDER BY date
    """, (today - timedelta(days=31),))
    rec_30 = cur.fetchall()

    recovery_30_js = ",".join(f"{{d:'{r['date'].strftime('%b %d')}',r:{int(float(r['recovery_score']))}}}" for r in rec_30)
    hrv_30_js = ",".join(f"{{d:'{r['date'].strftime('%b %d')}',h:{int(float(r['hrv_rmssd']))}}}" for r in rec_30 if r["hrv_rmssd"])
    sleep_30_js = ",".join(f"{{d:'{r['date'].strftime('%b %d')}',s:{float(r['sleep_duration_min'] or 0)/60:.1f}}}" for r in rec_30 if r["sleep_duration_min"])

    # 7-day averages for recovery
    rec_7d = [r for r in rec_30 if r["date"] >= today - timedelta(days=7)]
    rec_7d_avg = sum(float(r["recovery_score"]) for r in rec_7d) / len(rec_7d) if rec_7d else 0
    hrv_7d_avg = sum(float(r["hrv_rmssd"] or 0) for r in rec_7d) / len(rec_7d) if rec_7d else 0
    sleep_7d_avg = sum(float(r["sleep_duration_min"] or 0) / 60 for r in rec_7d) / len(rec_7d) if rec_7d else 0

    # 30-day change in recovery
    if len(rec_30) >= 14:
        first_7 = rec_30[:7]
        last_7 = rec_30[-7:]
        rec_first = sum(float(r["recovery_score"]) for r in first_7) / len(first_7)
        rec_last = sum(float(r["recovery_score"]) for r in last_7) / len(last_7)
        rec_30d_change = int(rec_last - rec_first)
        rec_30d_str = f"{rec_30d_change:+d}%"
    else:
        rec_30d_str = "N/A"

    # ── Quality data ──
    cur.execute("""
        SELECT date, workout_quality FROM training_workouts
        WHERE workout_quality IS NOT NULL AND completed = true
        ORDER BY date
    """)
    qual_rows = cur.fetchall()
    quality_js = ",".join(f"{{d:'{r['date'].strftime('%b %d')}',q:{float(r['workout_quality']):.1f}}}" for r in qual_rows[-80:])  # Last ~80 data points

    # Quality by recovery bracket
    cur.execute("""
        SELECT dp.recovery_score, dp.workout_quality
        FROM daily_performance dp
        WHERE dp.recovery_score IS NOT NULL AND dp.workout_quality IS NOT NULL
    """)
    qual_rec = cur.fetchall()
    red_q = [float(r["workout_quality"]) for r in qual_rec if float(r["recovery_score"]) < 33]
    yellow_q = [float(r["workout_quality"]) for r in qual_rec if 33 <= float(r["recovery_score"]) <= 66]
    green_q = [float(r["workout_quality"]) for r in qual_rec if float(r["recovery_score"]) > 66]

    qual_red = f"{sum(red_q)/len(red_q):.1f}" if red_q else "N/A"
    qual_yellow = f"{sum(yellow_q)/len(yellow_q):.1f}" if yellow_q else "N/A"
    qual_green = f"{sum(green_q)/len(green_q):.1f}" if green_q else "N/A"

    # Quality spread finding
    vals = []
    if red_q:
        vals.append(sum(red_q) / len(red_q))
    if yellow_q:
        vals.append(sum(yellow_q) / len(yellow_q))
    if green_q:
        vals.append(sum(green_q) / len(green_q))
    if len(vals) >= 2:
        spread = max(vals) - min(vals)
        qual_finding = f"Only {spread:.1f} point spread across all brackets. Mental toughness confirmed by data."
    else:
        qual_finding = "Insufficient data for bracket comparison."

    # Recovery-quality correlation
    if len(qual_rec) >= 20:
        n_dp = len(qual_rec)
        rec_vals = [float(r["recovery_score"]) for r in qual_rec]
        q_vals = [float(r["workout_quality"]) for r in qual_rec]
        mean_r = sum(rec_vals) / n_dp
        mean_q = sum(q_vals) / n_dp
        cov = sum((r - mean_r) * (q - mean_q) for r, q in zip(rec_vals, q_vals)) / n_dp
        std_r = (sum((r - mean_r) ** 2 for r in rec_vals) / n_dp) ** 0.5
        std_q = (sum((q - mean_q) ** 2 for q in q_vals) / n_dp) ** 0.5
        corr_rq = cov / (std_r * std_q) if std_r > 0 and std_q > 0 else 0
    else:
        corr_rq = 0
        n_dp = len(qual_rec)

    recovery_finding = (
        f"Recovery score has minimal impact on workout quality (r={corr_rq:.2f}). "
        f"Eiwe performs consistently regardless of Whoop recovery color. Based on {n_dp} days of data."
    ) if abs(corr_rq) < 0.3 else (
        f"Recovery score shows moderate correlation with workout quality (r={corr_rq:.2f}). Based on {n_dp} days of data."
    )

    # Best workout conditions (top 20)
    cur.execute("""
        SELECT dp.recovery_score, dp.hrv_rmssd, dp.sleep_hours, dp.workout_quality
        FROM daily_performance dp
        WHERE dp.workout_quality IS NOT NULL AND dp.recovery_score IS NOT NULL
        ORDER BY dp.workout_quality DESC LIMIT 20
    """)
    top20 = cur.fetchall()
    best_recovery = f"{sum(float(r['recovery_score']) for r in top20)/len(top20):.0f}%" if top20 else "N/A"
    best_hrv = f"{sum(float(r['hrv_rmssd'] or 0) for r in top20)/len(top20):.0f}ms" if top20 else "N/A"
    best_sleep = f"{sum(float(r['sleep_hours'] or 0) for r in top20)/len(top20):.1f}h" if top20 else "N/A"
    best_quality = f"{sum(float(r['workout_quality']) for r in top20)/len(top20):.1f}" if top20 else "N/A"

    # ── Power Zone Distribution (from Strava real power data) ──
    cur.execute("""
        SELECT
            SUM(recovery_sec) as recovery,
            SUM(endurance_sec) as endurance,
            SUM(tempo_sec) as tempo,
            SUM(threshold_sec) as threshold,
            SUM(vo2_sec) as vo2,
            SUM(anaerobic_sec) as anaerobic,
            SUM(neuromuscular_sec) as neuromuscular,
            SUM(total_sec) as total,
            COUNT(*) as n
        FROM strava_power_zones
    """)
    zr = cur.fetchone()

    if zr and zr["total"] and int(zr["total"]) > 0:
        # Real power zone data from Strava
        zones: Dict[str, Dict[str, float]] = {
            "Recovery":      {"hours": float(zr["recovery"] or 0) / 3600, "count": 0},
            "Endurance":     {"hours": float(zr["endurance"] or 0) / 3600, "count": 0},
            "Tempo":         {"hours": float(zr["tempo"] or 0) / 3600, "count": 0},
            "Threshold":     {"hours": float(zr["threshold"] or 0) / 3600, "count": 0},
            "VO2":           {"hours": float(zr["vo2"] or 0) / 3600, "count": 0},
            "Anaerobic":     {"hours": float(zr["anaerobic"] or 0) / 3600, "count": 0},
            "Neuromuscular": {"hours": float(zr["neuromuscular"] or 0) / 3600, "count": 0},
        }
        total_workouts = int(zr["n"])
    else:
        # Fallback to title-based classification
        cur.execute("""
            SELECT title, tss_actual, if_actual FROM training_workouts
            WHERE completed = true AND tss_actual > 0
        """)
        all_workouts = cur.fetchall()
        zones = {
            "Recovery": {"hours": 0, "count": 0},
            "Endurance": {"hours": 0, "count": 0},
            "Tempo": {"hours": 0, "count": 0},
            "Threshold": {"hours": 0, "count": 0},
            "VO2": {"hours": 0, "count": 0},
            "Anaerobic": {"hours": 0, "count": 0},
            "Neuromuscular": {"hours": 0, "count": 0},
        }
        for w in all_workouts:
            zone = _classify_zone(w["title"], w["if_actual"])
            tss = float(w["tss_actual"] or 0)
            if_val = float(w["if_actual"]) if w["if_actual"] else 0.65
            hours = tss / (if_val ** 2 * 100) if if_val > 0 else tss / 42.25
            # Map old zone names to new
            if zone in ("Threshold/VO2",):
                zones["Threshold"]["hours"] += hours * 0.6
                zones["VO2"]["hours"] += hours * 0.4
            elif zone == "Sweetspot":
                zones["Tempo"]["hours"] += hours
            elif zone == "Free Ride":
                zones["Endurance"]["hours"] += hours * 0.7
                zones["Tempo"]["hours"] += hours * 0.3
            elif zone == "Other High Int.":
                zones["Anaerobic"]["hours"] += hours
            elif zone in zones:
                zones[zone]["hours"] += hours
            else:
                zones["Endurance"]["hours"] += hours
        total_workouts = len(all_workouts)

    # Zone donut data (in minutes for chart)
    zone_order = ["Recovery", "Endurance", "Tempo", "Threshold", "VO2", "Anaerobic", "Neuromuscular"]
    zone_donut = [int(zones[z]["hours"] * 60) for z in zone_order]

    # ── FTP Trajectory ──
    cur.execute("SELECT test_date, ftp_watts FROM ftp_history ORDER BY test_date")
    ftp_history = cur.fetchall()

    # Monthly max NP from high-IF workouts
    cur.execute("""
        SELECT date_trunc('month', date)::date as month,
               MAX(np_actual) as max_np
        FROM training_workouts
        WHERE completed = true AND np_actual IS NOT NULL AND if_actual >= 0.7
        GROUP BY month ORDER BY month
    """)
    np_monthly = cur.fetchall()

    np_trend_labels = [r["month"].strftime("%b") if r["month"] else "?" for r in np_monthly[-13:]]
    np_trend_values = [int(float(r["max_np"])) for r in np_monthly[-13:]]
    # Color the peak value differently
    peak_np_val = max(np_trend_values) if np_trend_values else 0
    np_trend_colors = []
    for v in np_trend_values:
        if v == peak_np_val:
            np_trend_colors.append("'rgba(249,115,22,0.7)'")
        elif v >= peak_np_val - 10:
            np_trend_colors.append("'rgba(96,165,250,0.5)'")
        else:
            np_trend_colors.append("'rgba(96,165,250,0.35)'")

    recent_max_np = np_trend_values[-1] if np_trend_values else 0
    peak_np = peak_np_val
    peak_np_month = ""
    for r in np_monthly:
        if float(r["max_np"]) == peak_np:
            peak_np_month = r["month"].strftime("%b '%y") if r["month"] else "?"

    # NP trend direction (last 3 months)
    if len(np_trend_values) >= 3:
        last3 = np_trend_values[-3:]
        if last3[-1] > last3[0] + 5:
            np_trend = "UP ↑"
        elif last3[-1] < last3[0] - 5:
            np_trend = "DOWN ↓"
        else:
            np_trend = "FLAT"
    else:
        np_trend = "N/A"

    # FTP projection (data-driven, not linear)
    # Base plateau through current month, build gains Apr-Jun, continued Jul-Dec
    months_labels = ["Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    # Adjust starting month based on current date
    current_month = today.month
    ftp_proj = []
    for i, m in enumerate(months_labels):
        month_num = i + 2  # Feb=2, Mar=3, ...
        if month_num <= 3:  # Base phase
            ftp_proj.append(ftp)
        elif month_num <= 4:  # Early build
            ftp_proj.append(ftp + 2)
        elif month_num <= 5:  # Build
            ftp_proj.append(ftp + int((FTP_TARGET - ftp) * 0.25))
        elif month_num <= 6:  # Peak
            ftp_proj.append(ftp + int((FTP_TARGET - ftp) * 0.4))
        elif month_num <= 8:  # Summer gains
            ftp_proj.append(ftp + int((FTP_TARGET - ftp) * 0.55 + (month_num - 6) * 2))
        else:  # Fall push
            progress = 0.55 + (month_num - 8) * 0.11
            ftp_proj.append(min(FTP_TARGET, ftp + int((FTP_TARGET - ftp) * min(progress, 1.0))))

    # FTP insight text
    if len(np_trend_values) >= 3:
        last3_avg = sum(np_trend_values[-3:]) / 3
        ftp_insight = (
            f"NP trend is {np_trend.lower().replace(' ↑','').replace(' ↓','').strip()} over the last 3 months "
            f"({min(np_trend_values[-3:])}-{max(np_trend_values[-3:])}W). "
            f"This is expected and normal during base phase: high volume, lower intensity. "
            f"CTL is climbing ({ctl:.1f}, up from low 20s), which means aerobic fitness is building "
            f"even though peak power numbers haven't moved yet. The build phase (Apr-May) is where "
            f"threshold work drives FTP gains. Peak summer NP hit {peak_np}W in {peak_np_month} with a lower CTL. "
            f"With a stronger base this year, similar or higher peaks are achievable."
        )
    else:
        ftp_insight = "Insufficient NP data for trend analysis."

    # Next FTP test date (~2 weeks from last test or hardcoded)
    next_test_date = "Feb 26"
    ftp_next_range = f"{ftp-3}-{ftp+7}W"
    ftp_at_race = f"{ftp_proj[4] if len(ftp_proj) > 4 else ftp+15}-{ftp_proj[4]+10 if len(ftp_proj) > 4 else ftp+25}W"

    # ── Key Insights ──
    # Load from training_insights table
    cur.execute("SELECT insight_type, insight_text FROM training_insights ORDER BY created_at DESC")
    insights_db = {r["insight_type"]: r["insight_text"] for r in cur.fetchall()}

    # Consistency insight
    cur.execute("SELECT COUNT(*) as n FROM training_workouts WHERE tss_planned > 0")
    total_planned = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) as n FROM training_workouts WHERE tss_planned > 0 AND completed = true")
    total_completed = cur.fetchone()["n"]
    completion_rate = (total_completed / total_planned * 100) if total_planned > 0 else 0

    # Recent adherence streak
    cur.execute("""
        SELECT date, completed FROM training_workouts
        WHERE tss_planned > 0 ORDER BY date DESC LIMIT 30
    """)
    recent_wk = cur.fetchall()
    streak = 0
    for w in recent_wk:
        if w["completed"]:
            streak += 1
        else:
            break

    insight_recovery_db = insights_db.get("recovery_correlation")
    if insight_recovery_db:
        insight_recovery = _escape(insight_recovery_db)
    else:
        insight_recovery = (
            f"Recovery score has minimal impact on workout quality (green: {qual_green}, red: {qual_red}, r={corr_rq:.2f}). "
            f"You perform consistently regardless of recovery. Based on {n_dp} days."
        )

    # HRV insight
    cur.execute("""
        SELECT dp.hrv_rmssd, dp.workout_quality FROM daily_performance dp
        WHERE dp.hrv_rmssd IS NOT NULL AND dp.workout_quality IS NOT NULL
    """)
    hrv_qual = cur.fetchall()
    good_hrv_data = [float(r["hrv_rmssd"]) for r in hrv_qual if float(r["workout_quality"]) >= 80]
    other_hrv_data = [float(r["hrv_rmssd"]) for r in hrv_qual if float(r["workout_quality"]) < 80]
    good_hrv_avg = sum(good_hrv_data) / len(good_hrv_data) if good_hrv_data else 0
    other_hrv_avg = sum(other_hrv_data) / len(other_hrv_data) if other_hrv_data else 0
    # HRV threshold (25th percentile of good workouts)
    if good_hrv_data:
        sorted_hrv = sorted(good_hrv_data)
        hrv_threshold = sorted_hrv[len(sorted_hrv) // 4]
    else:
        hrv_threshold = 30

    insight_hrv = (
        f"Best workouts (quality &ge;80, n={len(good_hrv_data)}) typically occur when HRV is above "
        f"{hrv_threshold:.0f}ms. Average HRV on good days: {good_hrv_avg:.0f}ms vs other days: {other_hrv_avg:.0f}ms."
    )

    # Sleep insight
    cur.execute("""
        SELECT dp.sleep_hours, dp.workout_quality FROM daily_performance dp
        WHERE dp.sleep_hours IS NOT NULL AND dp.workout_quality IS NOT NULL
    """)
    sleep_qual = cur.fetchall()
    avg_sleep_all = sum(float(r["sleep_hours"]) for r in sleep_qual) / len(sleep_qual) if sleep_qual else 0
    high_sleep_q = [float(r["workout_quality"]) for r in sleep_qual if float(r["sleep_hours"]) >= 7.5]
    low_sleep_q = [float(r["workout_quality"]) for r in sleep_qual if float(r["sleep_hours"]) < 6]
    high_sleep_avg = sum(high_sleep_q) / len(high_sleep_q) if high_sleep_q else 0
    low_sleep_avg = sum(low_sleep_q) / len(low_sleep_q) if low_sleep_q else 0

    # Sleep correlation
    if len(sleep_qual) >= 10:
        s_vals = [float(r["sleep_hours"]) for r in sleep_qual]
        q_vals2 = [float(r["workout_quality"]) for r in sleep_qual]
        n_s = len(s_vals)
        mean_s = sum(s_vals) / n_s
        mean_q2 = sum(q_vals2) / n_s
        cov_s = sum((s - mean_s) * (q - mean_q2) for s, q in zip(s_vals, q_vals2)) / n_s
        std_s = (sum((s - mean_s) ** 2 for s in s_vals) / n_s) ** 0.5
        std_q2 = (sum((q - mean_q2) ** 2 for q in q_vals2) / n_s) ** 0.5
        corr_sleep = cov_s / (std_s * std_q2) if std_s > 0 and std_q2 > 0 else 0
    else:
        corr_sleep = 0

    insight_sleep = (
        f"Sleep hours correlate with workout quality at r={corr_sleep:.2f} (n={len(sleep_qual)}). "
        f"Average sleep {avg_sleep_all:.1f} hrs. 7.5+ hrs shows quality of {high_sleep_avg:.1f} "
        f"vs {low_sleep_avg:.1f} on &lt;6 hr nights. {'Minimal difference.' if abs(corr_sleep) < 0.2 else 'Notable impact.'}"
    )

    insight_consistency = (
        f"Workout completion rate: {completion_rate:.1f}% ({total_completed}/{total_planned} planned workouts). "
        f"Recent {streak}-workout streak at 100% completion." if streak > 3 else
        f"Workout completion rate: {completion_rate:.1f}% ({total_completed}/{total_planned} planned workouts)."
    )

    # FTP outlook insight
    insight_ftp = (
        f"NP trend {np_trend.lower().strip()} at {min(np_trend_values[-3:])}-{max(np_trend_values[-3:])}W over last 3 months (base phase). "
        f"Peak NP was {peak_np}W in {peak_np_month}. FTP gains expected during build phase (Apr-May). "
        f"Next test (~{next_test_date}) prediction: {ftp_next_range}. "
        f"{FTP_TARGET}W target requires strong summer/fall block."
    ) if len(np_trend_values) >= 3 else "Insufficient data for FTP outlook."

    # ── Race projections ──
    # Project CTL/TSB at race using taper model
    ctl_proj = ctl
    atl_proj = atl
    days_to_race = vatt_days
    for d in range(days_to_race):
        weeks_out = (days_to_race - d) / 7
        if weeks_out > 12:
            daily_tss = max(ctl * 7, 350) / 7
        elif weeks_out > 6:
            daily_tss = 500 / 7
        elif weeks_out > 3:
            daily_tss = 600 / 7
        elif weeks_out > 2:
            daily_tss = 600 * 0.7 / 7
        elif weeks_out > 0.3:
            daily_tss = 600 * 0.5 / 7
        else:
            daily_tss = 15
        ctl_proj = ctl_proj + (daily_tss - ctl_proj) / 42.0
        atl_proj = atl_proj + (daily_tss - atl_proj) / 7.0
    tsb_proj = ctl_proj - atl_proj

    # Projected race FTP
    weeks_to_target = max(1, (date(2026, 12, 31) - today).days / 7)
    weekly_gain = (FTP_TARGET - ftp) / weeks_to_target
    weeks_to_race = max(0, vatt_days / 7)
    proj_race_ftp = round(ftp + weekly_gain * weeks_to_race)

    # Race config values
    race_cfg = CONFIG.get("race", {})
    target_if = race_cfg.get("target_if", 0.80)
    race_ftp_cfg = race_cfg.get("projected_race_ftp", proj_race_ftp)
    draft_pct = race_cfg.get("drafting_benefit_pct", 20)
    climb_cap_pct = race_cfg.get("climb_cap_pct", 0.85)
    seg_pacing = race_cfg.get("segments_pacing", [])
    vi = race_cfg.get("variability_index", 1.12)

    race_np = round(target_if * race_ftp_cfg)
    race_avg = round(race_np / vi)
    tss_per_hr = target_if ** 2 * 100
    # Estimate ride hours (rough: distance / estimated speed)
    est_ride_hrs = 9.0  # approximate
    race_est_tss = round(tss_per_hr * est_ride_hrs)

    # Build segment rows from config
    seg_rows = ""
    for seg in seg_pacing:
        pct_low = seg["pct_low"]
        pct_high = seg["pct_high"]
        w_low = round(race_ftp_cfg * pct_low)
        w_high = round(race_ftp_cfg * pct_high)
        seg_rows += f'<tr><td>{_escape(seg["name"])}</td><td>{seg["km"]}</td><td>{w_low}-{w_high}W ({pct_low*100:.0f}-{pct_high*100:.0f}%)</td></tr>\n          '
    if not seg_rows:
        seg_rows = f'<tr><td>Full course</td><td>0-315km</td><td>{race_np}W (IF {target_if})</td></tr>'

    race_climb_cap = round(race_ftp_cfg * climb_cap_pct)

    # Phase indicator
    if days_to_race > 84:
        phase = "🔵 Current Phase: BASE"
    elif days_to_race > 42:
        phase = "🟡 Current Phase: BUILD"
    elif days_to_race > 14:
        phase = "🟠 Current Phase: PEAK"
    else:
        phase = "🟢 Current Phase: TAPER"

    # ── Coaching assessment ──
    # Gather all coaching variables
    cur.execute("""
        SELECT date_trunc('week', date)::date as week_start,
               COALESCE(SUM(tss_actual), 0) as tss
        FROM training_workouts
        WHERE date >= %s AND completed = true
        GROUP BY week_start ORDER BY week_start
    """, (today - timedelta(days=84),))
    recent_weeks = cur.fetchall()
    avg_weekly_tss = sum(float(r["tss"]) for r in recent_weeks) / len(recent_weeks) if recent_weeks else 0
    last4_weeks = recent_weeks[-4:] if len(recent_weeks) >= 4 else recent_weeks
    last4_avg_tss = sum(float(r["tss"]) for r in last4_weeks) / len(last4_weeks) if last4_weeks else 0

    # Count consecutive weeks above 350
    consec_350 = 0
    for w in reversed(recent_weeks):
        if float(w["tss"]) >= 350:
            consec_350 += 1
        else:
            break

    # Weeks of training
    cur.execute("SELECT MIN(date) FROM training_workouts WHERE completed = true")
    first_workout = cur.fetchone()
    first_date = first_workout[0] if first_workout and first_workout[0] else today
    weeks_training = max(1, (today - first_date).days // 7)

    # Latest workout
    cur.execute("""
        SELECT title, workout_quality, date FROM training_workouts
        WHERE completed = true AND workout_quality IS NOT NULL
        ORDER BY date DESC LIMIT 1
    """)
    latest_wk = cur.fetchone()
    latest_name_raw = latest_wk["title"] if latest_wk else "N/A"
    latest_name = _escape(latest_name_raw)
    latest_quality = float(latest_wk["workout_quality"]) if latest_wk else 0
    latest_date = latest_wk["date"].strftime("%b %d") if latest_wk else "N/A"

    # Average quality score
    cur.execute("SELECT AVG(workout_quality) as avg_q FROM training_workouts WHERE workout_quality IS NOT NULL")
    avg_quality = float(cur.fetchone()["avg_q"] or 0)

    # CTL start value (earliest in training_load)
    cur.execute("SELECT ctl FROM training_load ORDER BY date ASC LIMIT 1")
    ctl_start_row = cur.fetchone()
    ctl_start_val = float(ctl_start_row["ctl"]) if ctl_start_row else 0

    coaching_text = f'''<p><strong>Current Fitness Trajectory:</strong> Eiwe, your CTL has grown from {ctl_start_val:.0f} to {ctl:.1f} today over {weeks_training} weeks of training. Average weekly TSS over the last 12 weeks is {avg_weekly_tss:.0f}, with the last four weeks averaging {last4_avg_tss:.0f}. {f"{consec_350} consecutive weeks above 350 TSS, " if consec_350 > 0 else ""}completion rate of {completion_rate:.0f}%. You are building consistently with strong weekly loads.</p>

    <p><strong>What the Data Says About You:</strong> Three things stand out. <strong>Consistency</strong>: {completion_rate:.1f}% overall completion rate{f", {streak}-workout streak recently" if streak > 3 else ""}. <strong>Mental toughness</strong>: workout quality on red recovery days ({qual_red}) is virtually identical to green days ({qual_green}). Recovery score correlates at r={corr_rq:.2f} with quality; statistically {'zero' if abs(corr_rq) < 0.15 else 'weak'}. You execute regardless of how you feel, and the data confirms this works for you. <strong>Quality</strong>: average score {avg_quality:.1f}, with your most recent {latest_name} hitting {latest_quality:.1f} on {latest_date}.</p>

    <p><strong>Areas to Watch:</strong> Recovery trending {rec_30d_str} over the last month. Sleep averaging {avg_sleep_all:.1f} hours; target 7.5+ for a 48-year-old in a loading phase. HRV at {hrv_7d_avg:.1f}ms is {'stable but not climbing, which would be the ideal signal' if abs(hrv_7d_avg - good_hrv_avg) < 5 else 'showing movement'}. None are red flags, but monitor closely as build phase intensity increases.</p>

    <p><strong>Race Readiness:</strong> At {vatt_days} days out, you are well-positioned. CTL {ctl:.1f} gives a solid platform; we want 55-65 with TSB +15 to +25 on race day. The taper model projects CTL ~{ctl_proj:.0f}, TSB ~{tsb_proj:+.0f}. The 2026 pacing strategy (IF 0.64, NP 175-180W) is significantly more disciplined than 2025's aggressive IF 0.82, while the addition of drafting with a riding partner saves 20-30% energy. Your 2025 solo effort at that intensity, finishing strong with feeling of 1/great, confirms the endurance capacity is there. This year you are riding smarter, not just harder.</p>

    <p><strong>Fueling and Logistics:</strong> Formula 369 at 80-90g carbs/hour with 3 bottles plus gels is solid. Start eating within the first 20 minutes and stay ahead of the deficit all day. Practice this exact protocol on every long training ride. The 03:20 AM start gives you full daylight by mile 10 (sunrise 03:51) and 18+ hours of light. Coordinate stops with your riding partner; the 54-minute total stop budget is realistic for crowded depots.</p>

    <p><strong>Recommendations:</strong> (1) Continue base progression through March at 350-400 TSS/week. (2) Build phase April/May: sweet spot, threshold, race-specific 3-4 hour efforts at target IF. (3) Practice fueling protocol on every long ride; train the gut now. (4) Prioritize sleep: 7.5+ hours during heavy loading. (5) FTP test ~{next_test_date} to calibrate zones. (6) One 150+ mile ride in April/May with your partner to practice everything together. (7) Race Halvv&auml;ttern hard June 6, taper through June 12. (8) Practice the 2 AM wake-up before race week.</p>'''

    cur.close()
    conn.close()

    # ── Build replacements dict ──
    replacements = {
        "__MOBILE_FTP__": str(ftp),
        "__MOBILE_CTL__": f"{ctl:.1f}",
        "__HALV_DAYS__": str(halv_days),
        "__VATT_DAYS__": str(vatt_days),
        "__HEADER_DATE__": today.strftime("%b %d, %Y"),
        "__FTP__": str(ftp),
        "__CTL__": f"{ctl:.1f}",
        "__ATL__": f"{atl:.1f}",
        "__TSB__": f"{tsb:.1f}",
        "__WEEK_RANGE__": f"{monday.strftime('%b %d')}-{sunday.strftime('%d, %Y')}",
        "__TODAY_FOCUS_ICON__": focus_icon,
        "__TODAY_FOCUS_TITLE__": focus_title,
        "__TODAY_FOCUS_DETAIL__": focus_detail,
        "__WEEK_TSS_ACTUAL__": f"{tss_actual_wk:.0f}",
        "__WEEK_TSS_PLANNED__": f"{tss_planned_wk:.0f}",
        "__WEEK_PROGRESS__": f"{progress_wk}%",
        "__WEEK_HOURS__": f"{hours_wk:.1f}h",
        "__WEEK_COMPLETED__": f"{len(completed_wk)}/{len(planned_wk)}",
        "__WEEK_AVG_IF__": f"{avg_if_wk:.2f}",
        "__WEEK_PEAK_IF__": f"{peak_if_wk:.2f}",
        "__WEEK_AVG_RECOVERY__": f"{avg_rec_wk:.0f}%",
        "__WEEK_WORKOUT_ROWS__": "\n".join(workout_rows),
        "__WEEK_PMC_TEXT__": week_pmc_text,
        "__WEEK_RECOVERY_TEXT__": week_recovery_text,
        "__WEEKLY_TSS_DATA__": weekly_tss_data,
        "__PMC_DATA__": pmc_data,
        "__RECOVERY_30_DATA__": f"[{recovery_30_js}]",
        "__HRV_30_DATA__": f"[{hrv_30_js}]",
        "__SLEEP_30_DATA__": f"[{sleep_30_js}]",
        "__QUALITY_DATA__": f"[{quality_js}]",
        "__ZONE_RECOVERY_H__": f"{zones['Recovery']['hours']:.1f}h",
        "__ZONE_ENDURANCE_H__": f"{zones['Endurance']['hours']:.1f}h",
        "__ZONE_TEMPO_H__": f"{zones['Tempo']['hours']:.1f}h",
        "__ZONE_THRESHOLD_H__": f"{zones['Threshold']['hours']:.1f}h",
        "__ZONE_VO2_H__": f"{zones['VO2']['hours']:.1f}h",
        "__ZONE_ANAEROBIC_H__": f"{zones['Anaerobic']['hours']:.1f}h",
        "__ZONE_NEUROMUSCULAR_H__": f"{zones['Neuromuscular']['hours']:.1f}h",
        "__ZONE_DONUT_DATA__": str(zone_donut),
        "__TOTAL_WORKOUTS__": str(total_workouts),
        "__RECOVERY_FINDING__": recovery_finding,
        "__REC_7D_AVG__": f"{rec_7d_avg:.0f}%",
        "__HRV_7D_AVG__": f"{hrv_7d_avg:.1f}ms",
        "__SLEEP_7D_AVG__": f"{sleep_7d_avg:.1f}hrs",
        "__REC_30D_CHANGE__": rec_30d_str,
        "__QUAL_RED__": qual_red,
        "__QUAL_RED_N__": str(len(red_q)),
        "__QUAL_YELLOW__": qual_yellow,
        "__QUAL_YELLOW_N__": str(len(yellow_q)),
        "__QUAL_GREEN__": qual_green,
        "__QUAL_GREEN_N__": str(len(green_q)),
        "__QUAL_FINDING__": qual_finding,
        "__BEST_RECOVERY__": best_recovery,
        "__BEST_HRV__": best_hrv,
        "__BEST_SLEEP__": best_sleep,
        "__BEST_QUALITY__": best_quality,
        "__TODAY_SHORT__": today.strftime("%b %d"),
        "__FTP_NEXT_TEST__": ftp_next_range,
        "__NEXT_TEST_DATE__": next_test_date,
        "__FTP_AT_RACE__": ftp_at_race,
        "__RECENT_MAX_NP__": str(recent_max_np),
        "__PEAK_NP__": str(peak_np),
        "__PEAK_NP_DATE__": peak_np_month,
        "__NP_TREND__": np_trend,
        "__FTP_INSIGHT_TEXT__": ftp_insight,
        "__NP_TREND_LABELS__": json.dumps(np_trend_labels),
        "__NP_TREND_DATA__": json.dumps(np_trend_values),
        "__NP_TREND_COLORS__": "[" + ",".join(np_trend_colors) + "]",
        "__FTP_PROJECTION_DATA__": json.dumps(ftp_proj),
        "__INSIGHT_RECOVERY__": insight_recovery,
        "__INSIGHT_HRV__": insight_hrv,
        "__INSIGHT_SLEEP__": insight_sleep,
        "__INSIGHT_CONSISTENCY__": insight_consistency,
        "__INSIGHT_FTP__": insight_ftp,
        "__PROJ_RACE_CTL__": f"~{ctl_proj:.0f}",
        "__PROJ_RACE_TSB__": f"+{tsb_proj:.0f}" if tsb_proj > 0 else f"{tsb_proj:.0f}",
        "__PROJ_RACE_FTP__": str(race_ftp_cfg),
        "__RACE_SEGMENTS_ROWS__": seg_rows,
        "__RACE_CLIMB_CAP__": str(race_climb_cap),
        "__RACE_CLIMB_CAP_PCT__": str(round(climb_cap_pct * 100)),
        "__RACE_TARGET_NP__": str(race_np),
        "__RACE_TARGET_IF__": str(target_if),
        "__RACE_EST_TSS__": str(race_est_tss),
        "__RACE_DRAFT_PCT__": str(draft_pct),
        "__PHASE_INDICATOR__": phase,
        "__COACHING_TEXT__": coaching_text,
        "__GENERATED_DATE__": today.strftime("%b %d, %Y"),
        "__ATHLETE_NAME__": ATHLETE_NAME,
        "__ATHLETE_FIRST__": ATHLETE_FIRST,
        "__COACH_NAME__": COACH_NAME,
        "__COACH_FIRST__": COACH_FIRST,
    }

    # ── Fill template ──
    template = TEMPLATE_PATH.read_text()
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)

    # Check for unfilled placeholders
    import re
    unfilled = re.findall(r'__[A-Z_]+__', template)
    if unfilled:
        print(f"⚠️  Unfilled placeholders: {set(unfilled)}")

    OUTPUT_PATH.write_text(template)
    print(f"✅ Dashboard generated: {OUTPUT_PATH}")

    if upload:
        print("📤 Uploading to Fastmail...")
        env = load_env(FASTMAIL_ENV)
        password = env.get("FASTMAIL_FILES_PASSWORD")
        user = env.get("FASTMAIL_FILES_USER", FASTMAIL_UPLOAD_USER or "user")
        if not password:
            print("❌ Upload failed: FASTMAIL_FILES_PASSWORD not found in fastmail.env")
            return
        with OUTPUT_PATH.open("rb") as handle:
            resp = requests.put(
                FASTMAIL_UPLOAD_URL,
                data=handle,
                auth=(user, password),
                timeout=30,
            )
        if resp.ok:
            print("✅ Uploaded to Fastmail")
        else:
            print(f"❌ Upload failed: HTTP {resp.status_code} {resp.text.strip()}")
