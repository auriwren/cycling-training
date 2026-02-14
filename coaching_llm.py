"""
LLM-powered coaching assessment for the cycling dashboard.
Gathers training data, sends to Claude with a coaching persona,
and returns contextual HTML assessment text.
Caches results to avoid redundant API calls.
"""

import hashlib
import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import psycopg2
import psycopg2.extras

from config import get_config

CACHE_DIR = Path(os.environ.get("CYCLING_CACHE_DIR", str(Path.home() / ".openclaw/cache")))
CACHE_FILE = CACHE_DIR / "coaching-assessment-cache.json"
CACHE_TTL_HOURS = 20  # Regenerate if older than 20 hours (covers daily 5 AM runs)

SYSTEM_PROMPT = """You are an elite cycling coach and performance analyst. Your analysis methodology draws from the work of Hunter Allen, Joe Friel, and Andrew Coggan. You specialize in data-driven endurance coaching for serious amateur cyclists preparing for ultra-distance events.

Your role is to write a daily coaching assessment for the athlete's training dashboard. This is NOT a generic summary. You must:

1. IDENTIFY PATTERNS: Look for trends in the data — load ramp rates, sleep-performance correlations, recovery trends, consistency streaks or breaks, quality changes.
2. BE SPECIFIC: Reference actual numbers, dates, and workouts. "Your CTL jumped 3.5 points this week" not "fitness is improving."
3. FLAG CONCERNS: If sleep is dropping during a loading week, say so directly. If recovery is trending down while load is trending up, that's a yellow flag.
4. CONTEXTUALIZE FOR THE RACE: Everything connects back to race preparation. Where is the athlete relative to where they need to be?
5. BE GENUINELY DYNAMIC: Every assessment should be different because the data is different. Don't repeat the same observations day after day unless the situation genuinely hasn't changed.

{coach_framing}

FORMATTING RULES:
- Output valid HTML paragraphs (<p> tags) with <strong> for emphasis
- Use 4-5 paragraphs: Fitness Trajectory, What the Data Says, Areas to Watch, Race Readiness, Observations for the Coach
- Never use em-dashes. Use commas, semicolons, or colons instead.
- Be direct, not flowery. Write like a coach who respects the athlete's intelligence.
- No bullet points. Prose only.
- Keep total length to 400-600 words."""


def _parse_structure(structure_json: Optional[str], ftp: int) -> Optional[str]:
    """Parse TP workout structure into human-readable interval description."""
    if not structure_json:
        return None
    try:
        data = json.loads(structure_json) if isinstance(structure_json, str) else structure_json
        steps = data.get("structure", [])
        intensity_metric = data.get("primaryIntensityMetric", "percentOfFtp")
        parts = []
        for block in steps:
            for step in block.get("steps", []):
                name = step.get("name", "Unknown")
                length = step.get("length", {})
                duration_sec = length.get("value", 0) if length.get("unit") == "second" else 0
                duration_min = duration_sec // 60 if duration_sec else 0

                targets = step.get("targets", [])
                target_str = ""
                if targets and intensity_metric == "percentOfFtp":
                    low = targets[0].get("minValue", 0)
                    high = targets[0].get("maxValue", 0)
                    low_w = round(ftp * low / 100)
                    high_w = round(ftp * high / 100)
                    target_str = f" ({low}-{high}% FTP, {low_w}-{high_w}W)"

                notes = step.get("notes", "")
                note_str = f' — "{notes}"' if notes else ""

                reps = block.get("length", {}).get("value", 1)
                rep_str = f" x{reps}" if reps > 1 else ""

                parts.append(f"{duration_min}min {name}{target_str}{rep_str}{note_str}")
        return " → ".join(parts) if parts else None
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _get_coaching_data(conn) -> Dict[str, Any]:
    """Gather all data needed for the coaching assessment."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    today = date.today()
    data: Dict[str, Any] = {"date": today.isoformat()}

    cfg = get_config()

    # Athlete and coach info
    data["athlete_name"] = cfg.get("athlete_name", "Athlete")
    data["athlete_first"] = data["athlete_name"].split()[0]
    data["coach_name"] = cfg.get("coach_name", "")
    data["coach_first"] = data["coach_name"].split()[0] if data["coach_name"] else ""

    # Current PMC
    cur.execute("SELECT ctl, atl, tsb FROM training_load ORDER BY date DESC LIMIT 1")
    pmc = cur.fetchone()
    if pmc:
        data["ctl"] = float(pmc["ctl"])
        data["atl"] = float(pmc["atl"])
        data["tsb"] = float(pmc["tsb"])
    else:
        data["ctl"] = data["atl"] = data["tsb"] = 0

    # PMC 7 days ago for weekly change
    cur.execute("SELECT ctl, atl, tsb FROM training_load WHERE date <= %s ORDER BY date DESC LIMIT 1",
                (today - timedelta(days=7),))
    pmc_7d = cur.fetchone()
    if pmc_7d:
        data["ctl_7d_ago"] = float(pmc_7d["ctl"])
        data["ctl_change_7d"] = data["ctl"] - float(pmc_7d["ctl"])
    else:
        data["ctl_7d_ago"] = data["ctl"]
        data["ctl_change_7d"] = 0

    # Weekly TSS (last 8 weeks)
    cur.execute("""
        SELECT date_trunc('week', date)::date as week_start,
               COALESCE(SUM(tss_actual), 0) as tss,
               COUNT(*) FILTER (WHERE completed = true) as completed,
               COUNT(*) as total
        FROM training_workouts
        WHERE date >= %s
        GROUP BY week_start ORDER BY week_start
    """, (today - timedelta(days=56),))
    weeks = cur.fetchall()
    data["weekly_tss"] = [
        {"week": str(w["week_start"]), "tss": float(w["tss"]),
         "completed": int(w["completed"]), "total": int(w["total"])}
        for w in weeks
    ]

    # This week's workouts (detail)
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    # Get FTP early for structure parsing
    cur.execute("SELECT ftp_watts FROM ftp_history ORDER BY test_date DESC LIMIT 1")
    ftp_row_early = cur.fetchone()
    ftp_val = int(ftp_row_early["ftp_watts"]) if ftp_row_early else 263

    cur.execute("""
        SELECT date, title, tss_actual, tss_planned, if_actual, if_planned, np_actual,
               COALESCE(duration_actual_min, duration_planned_min) as duration,
               workout_quality, completed, workout_structure, notes
        FROM training_workouts
        WHERE date >= %s AND date <= %s
        ORDER BY date
    """, (monday, sunday))
    data["this_week_workouts"] = [
        {"date": str(w["date"]), "title": w["title"],
         "tss": float(w["tss_actual"]) if w["tss_actual"] else (float(w["tss_planned"]) if w["tss_planned"] else None),
         "tss_planned": float(w["tss_planned"]) if w["tss_planned"] else None,
         "if": float(w["if_actual"]) if w["if_actual"] else None,
         "if_planned": float(w["if_planned"]) if w["if_planned"] else None,
         "np": float(w["np_actual"]) if w["np_actual"] else None,
         "duration_min": float(w["duration"]) if w["duration"] else None,
         "quality": float(w["workout_quality"]) if w["workout_quality"] else None,
         "completed": w["completed"],
         "structure": _parse_structure(w["workout_structure"], ftp_val),
         "coach_notes": w["notes"]}
        for w in cur.fetchall()
    ]

    # Last 5 completed workouts
    cur.execute("""
        SELECT date, title, tss_actual, if_actual, np_actual, workout_quality
        FROM training_workouts
        WHERE completed = true AND tss_actual IS NOT NULL
        ORDER BY date DESC LIMIT 5
    """)
    data["recent_workouts"] = [
        {"date": str(w["date"]), "title": w["title"],
         "tss": float(w["tss_actual"]), "if": float(w["if_actual"]) if w["if_actual"] else None,
         "np": float(w["np_actual"]) if w["np_actual"] else None,
         "quality": float(w["workout_quality"]) if w["workout_quality"] else None}
        for w in cur.fetchall()
    ]

    # Completion rate
    cur.execute("""
        SELECT COUNT(*) FILTER (WHERE completed = true) as done,
               COUNT(*) as total
        FROM training_workouts
        WHERE tss_planned IS NOT NULL OR tss_actual IS NOT NULL
    """)
    comp = cur.fetchone()
    data["completion_rate"] = (int(comp["done"]) / int(comp["total"]) * 100) if comp["total"] else 0

    # Streak
    cur.execute("""
        SELECT date, completed FROM training_workouts
        WHERE date <= %s AND (tss_planned IS NOT NULL OR tss_actual IS NOT NULL)
        ORDER BY date DESC LIMIT 20
    """, (today,))
    streak = 0
    for w in cur.fetchall():
        if w["completed"]:
            streak += 1
        else:
            break
    data["streak"] = streak

    # Quality by recovery color (last 90 days)
    cur.execute("""
        SELECT
            AVG(CASE WHEN wr.recovery_score < 34 THEN tw.workout_quality END) as red_quality,
            AVG(CASE WHEN wr.recovery_score BETWEEN 34 AND 66 THEN tw.workout_quality END) as yellow_quality,
            AVG(CASE WHEN wr.recovery_score > 66 THEN tw.workout_quality END) as green_quality
        FROM training_workouts tw
        JOIN whoop_recovery wr ON tw.date = wr.date
        WHERE tw.workout_quality IS NOT NULL AND tw.date >= %s
    """, (today - timedelta(days=90),))
    qr = cur.fetchone()
    data["quality_by_recovery"] = {
        "red": round(float(qr["red_quality"]), 1) if qr["red_quality"] else None,
        "yellow": round(float(qr["yellow_quality"]), 1) if qr["yellow_quality"] else None,
        "green": round(float(qr["green_quality"]), 1) if qr["green_quality"] else None,
    }

    # Recovery trend (last 14 days)
    cur.execute("""
        SELECT date, recovery_score, hrv_rmssd as hrv, resting_hr as rhr,
               sleep_duration_min / 60.0 as sleep_hours
        FROM whoop_recovery
        WHERE date >= %s ORDER BY date
    """, (today - timedelta(days=14),))
    recovery = cur.fetchall()
    data["recovery_14d"] = [
        {"date": str(r["date"]),
         "recovery": float(r["recovery_score"]) if r["recovery_score"] else None,
         "hrv": float(r["hrv"]) if r["hrv"] else None,
         "rhr": float(r["rhr"]) if r["rhr"] else None,
         "sleep": float(r["sleep_hours"]) if r["sleep_hours"] else None}
        for r in recovery
    ]

    # Sleep average (7d and 30d)
    cur.execute("SELECT AVG(sleep_duration_min / 60.0) as avg FROM whoop_recovery WHERE date >= %s",
                (today - timedelta(days=7),))
    data["sleep_avg_7d"] = round(float(cur.fetchone()["avg"] or 0), 1)
    cur.execute("SELECT AVG(sleep_duration_min / 60.0) as avg FROM whoop_recovery WHERE date >= %s",
                (today - timedelta(days=30),))
    data["sleep_avg_30d"] = round(float(cur.fetchone()["avg"] or 0), 1)

    # HRV average (7d)
    cur.execute("SELECT AVG(hrv_rmssd) as avg FROM whoop_recovery WHERE date >= %s",
                (today - timedelta(days=7),))
    data["hrv_avg_7d"] = round(float(cur.fetchone()["avg"] or 0), 1)

    # Recovery correlation
    cur.execute("""
        SELECT CORR(wr.recovery_score, tw.workout_quality)
        FROM training_workouts tw
        JOIN whoop_recovery wr ON tw.date = wr.date
        WHERE tw.workout_quality IS NOT NULL
    """)
    corr = cur.fetchone()
    corr_val = list(corr.values())[0] if corr else 0
    data["recovery_quality_corr"] = round(float(corr_val or 0), 3)

    # FTP
    cur.execute("SELECT ftp_watts, test_date, test_protocol, confidence, notes FROM ftp_history ORDER BY test_date DESC LIMIT 1")
    ftp_row = cur.fetchone()
    data["current_ftp"] = int(ftp_row["ftp_watts"]) if ftp_row else 0
    data["ftp_test_date"] = str(ftp_row["test_date"]) if ftp_row else "unknown"
    data["ftp_protocol"] = ftp_row["test_protocol"] if ftp_row else "unknown"
    data["ftp_confidence"] = ftp_row["confidence"] if ftp_row else "unknown"

    # Annotations (flu/sick weeks, injuries, etc.)
    cur.execute("""
        SELECT date, notes FROM daily_performance
        WHERE notes IS NOT NULL AND notes != ''
        ORDER BY date
    """)
    annotations = cur.fetchall()
    data["annotations"] = [
        {"date": str(a["date"]), "note": a["notes"]}
        for a in annotations
    ]

    # Race config
    race_cfg = cfg.get("race_plan", {})
    data["race_target_if"] = race_cfg.get("target_if", 0.80)
    data["race_ftp"] = race_cfg.get("projected_race_ftp", 280)
    data["race_np"] = round(data["race_target_if"] * data["race_ftp"], 0)
    data["draft_pct"] = race_cfg.get("drafting_benefit_pct", 20)

    # Days to races
    vatt_str = cfg.get("vatternrundan_date", "2026-06-13")
    halv_str = cfg.get("halvvattern_date", "2026-06-06")
    data["days_to_vatternrundan"] = (date.fromisoformat(vatt_str) - today).days
    data["days_to_halvvattern"] = (date.fromisoformat(halv_str) - today).days

    # Taper projection
    data["taper_ctl_proj"] = round(data["ctl"] * 0.85, 1)  # Rough estimate
    data["taper_tsb_proj"] = round(data["taper_ctl_proj"] - data["atl"] * 0.5, 1)

    # CTL start (for growth tracking)
    cur.execute("SELECT ctl FROM training_load ORDER BY date ASC LIMIT 1")
    ctl_start = cur.fetchone()
    data["ctl_start"] = float(ctl_start["ctl"]) if ctl_start else 0

    # Weeks of training
    cur.execute("SELECT MIN(date) as min_date FROM training_workouts WHERE completed = true")
    first = cur.fetchone()
    first_date = first["min_date"] if first and first["min_date"] else today
    data["weeks_training"] = max(1, (today - first_date).days // 7)

    # Week-over-week load change
    if len(data["weekly_tss"]) >= 2:
        this_wk = data["weekly_tss"][-1]["tss"]
        last_wk = data["weekly_tss"][-2]["tss"]
        data["load_change_pct"] = round((this_wk - last_wk) / last_wk * 100, 1) if last_wk > 0 else 0
    else:
        data["load_change_pct"] = 0

    cur.close()
    return data


def _build_user_prompt(data: Dict[str, Any]) -> str:
    """Build the user prompt with all training data."""
    lines = [
        f"Today is {data['date']}. Generate the daily coaching assessment.",
        "",
        f"ATHLETE: {data['athlete_name']}",
        f"FTP: {data['current_ftp']}W (method: {data['ftp_protocol']}, confidence: {data['ftp_confidence']}, date: {data['ftp_test_date']}). NOTE: 'estimated' means derived from workout data, NOT from an actual FTP test. Do not say the athlete 'tested at' this FTP unless the protocol is 'ramp_test' or '20min_test'.",
        f"Race: Vätternrundan in {data['days_to_vatternrundan']} days, Halvvättern in {data['days_to_halvvattern']} days",
        f"Race plan: IF {data['race_target_if']}, NP {data['race_np']:.0f}W at projected {data['race_ftp']}W FTP, {data['draft_pct']}% drafting benefit",
        "",
        "CURRENT PMC:",
        f"  CTL (fitness): {data['ctl']:.1f} (was {data['ctl_7d_ago']:.1f} a week ago, change: {data['ctl_change_7d']:+.1f})",
        f"  ATL (fatigue): {data['atl']:.1f}",
        f"  TSB (form): {data['tsb']:.1f}",
        f"  CTL growth: from {data['ctl_start']:.0f} to {data['ctl']:.1f} over {data['weeks_training']} weeks",
        "",
        "WEEKLY TSS (last 8 weeks):",
    ]
    for w in data["weekly_tss"]:
        lines.append(f"  {w['week']}: TSS {w['tss']:.0f} ({w['completed']}/{w['total']} workouts)")
    if data["load_change_pct"] != 0:
        lines.append(f"  Week-over-week load change: {data['load_change_pct']:+.1f}%")

    # Calculate weekly totals
    completed_tss = sum(w["tss"] for w in data["this_week_workouts"] if w["completed"] and w["tss"])
    remaining_tss = sum(w["tss_planned"] for w in data["this_week_workouts"] if not w["completed"] and w["tss_planned"])
    projected_week_tss = completed_tss + remaining_tss
    completed_count = sum(1 for w in data["this_week_workouts"] if w["completed"])
    remaining_count = sum(1 for w in data["this_week_workouts"] if not w["completed"])

    lines.extend([
        "",
        f"THIS WEEK'S PLAN (Mon-Sun): {completed_count} completed ({completed_tss:.0f} TSS), {remaining_count} remaining ({remaining_tss:.0f} TSS planned), projected week total: {projected_week_tss:.0f} TSS",
        "Full schedule:",
    ])
    for w in data["this_week_workouts"]:
        status = "✓ DONE" if w["completed"] else "UPCOMING"
        metrics = []
        if w["completed"]:
            if w["tss"]: metrics.append(f"TSS {w['tss']:.0f}")
            if w["if"]: metrics.append(f"IF {w['if']:.3f}")
            if w["np"]: metrics.append(f"NP {w['np']:.0f}W")
            if w["duration_min"]: metrics.append(f"{w['duration_min']:.0f}min")
            if w["quality"]: metrics.append(f"quality {w['quality']:.1f}")
        else:
            if w["tss_planned"]: metrics.append(f"TSS {w['tss_planned']:.0f} planned")
            if w["if_planned"]: metrics.append(f"IF {w['if_planned']:.3f} planned")
            if w["duration_min"]: metrics.append(f"{w['duration_min']:.0f}min planned")
        lines.append(f"  {w['date']} {w['title']} [{status}] {', '.join(metrics)}")
        if w.get("structure"):
            lines.append(f"    Intervals: {w['structure']}")
        if w.get("coach_notes"):
            lines.append(f"    Coach notes: {w['coach_notes']}")

    lines.extend([
        "",
        "LAST 5 COMPLETED WORKOUTS:",
    ])
    for w in data["recent_workouts"]:
        metrics = [f"TSS {w['tss']:.0f}"]
        if w["if"]: metrics.append(f"IF {w['if']:.3f}")
        if w["np"]: metrics.append(f"NP {w['np']:.0f}W")
        if w["quality"]: metrics.append(f"quality {w['quality']:.1f}")
        lines.append(f"  {w['date']} {w['title']}: {', '.join(metrics)}")

    lines.extend([
        "",
        f"COMPLETION: {data['completion_rate']:.1f}% overall, current streak: {data['streak']} workouts",
        "",
        "QUALITY BY RECOVERY COLOR (last 90 days):",
        f"  Red days: {data['quality_by_recovery']['red']}",
        f"  Yellow days: {data['quality_by_recovery']['yellow']}",
        f"  Green days: {data['quality_by_recovery']['green']}",
        f"  Recovery-quality correlation: r={data['recovery_quality_corr']:.3f}",
        "",
        "RECOVERY (last 14 days):",
    ])
    for r in data["recovery_14d"]:
        parts = []
        if r["recovery"] is not None: parts.append(f"recovery {r['recovery']:.0f}%")
        if r["hrv"] is not None: parts.append(f"HRV {r['hrv']:.0f}ms")
        if r["sleep"] is not None: parts.append(f"sleep {r['sleep']:.1f}h")
        if r["rhr"] is not None: parts.append(f"RHR {r['rhr']:.0f}")
        lines.append(f"  {r['date']}: {', '.join(parts)}")

    lines.extend([
        "",
        f"SLEEP: 7-day avg {data['sleep_avg_7d']}h, 30-day avg {data['sleep_avg_30d']}h",
        f"HRV: 7-day avg {data['hrv_avg_7d']}ms",
    ])

    # Add annotations if any exist
    if data.get("annotations"):
        lines.extend(["", "ANNOTATIONS (illness, injury, travel, etc.):"])
        for a in data["annotations"]:
            lines.append(f"  {a['date']}: {a['note']}")
        lines.append("IMPORTANT: When you see low TSS weeks that overlap with annotations (flu, injury, travel), attribute the dip to the annotated cause. Do not speculate about other reasons.")

    return "\n".join(lines)


def _call_llm(system: str, user: str) -> str:
    """Call LLM via OpenClaw gateway's chat completions endpoint."""
    import requests as req

    # Use OpenClaw gateway's OpenAI-compatible endpoint
    gateway_port = os.environ.get("OPENCLAW_GATEWAY_PORT", "18789")
    gateway_token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")

    # Try to read gateway token from systemd service config
    if not gateway_token:
        try:
            import subprocess
            result = subprocess.run(
                ["systemctl", "--user", "show", "openclaw-gateway.service",
                 "--property=Environment"],
                capture_output=True, text=True
            )
            for part in result.stdout.split():
                if part.startswith("OPENCLAW_GATEWAY_TOKEN="):
                    gateway_token = part.split("=", 1)[1]
                elif part.startswith("OPENCLAW_GATEWAY_PORT="):
                    gateway_port = part.split("=", 1)[1]
        except Exception:
            pass

    if not gateway_token:
        raise RuntimeError("No OPENCLAW_GATEWAY_TOKEN found")

    resp = req.post(
        f"http://127.0.0.1:{gateway_port}/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {gateway_token}",
            "Content-Type": "application/json",
        },
        json={
            "model": "anthropic/claude-sonnet-4-20250514",
            "max_tokens": 1500,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=45,
    )
    resp.raise_for_status()
    result = resp.json()
    return result["choices"][0]["message"]["content"]


def _get_cached(data_hash: str) -> Optional[str]:
    """Return cached assessment if fresh enough."""
    if not CACHE_FILE.exists():
        return None
    try:
        cache = json.loads(CACHE_FILE.read_text())
        if cache.get("hash") == data_hash:
            age_hours = (time.time() - cache.get("timestamp", 0)) / 3600
            if age_hours < CACHE_TTL_HOURS:
                return cache["html"]
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _save_cache(data_hash: str, html: str) -> None:
    """Cache the assessment."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps({
        "hash": data_hash,
        "timestamp": time.time(),
        "date": date.today().isoformat(),
        "html": html,
    }))
    os.chmod(CACHE_FILE, 0o600)


def generate_coaching_assessment(conn) -> str:
    """Generate (or return cached) LLM coaching assessment HTML."""
    data = _get_coaching_data(conn)

    # Hash the data to detect changes
    data_str = json.dumps(data, sort_keys=True, default=str)
    data_hash = hashlib.sha256(data_str.encode()).hexdigest()[:16]

    # Check cache
    cached = _get_cached(data_hash)
    if cached:
        return cached

    # Build prompts
    has_coach = bool(data["coach_name"] and data["coach_name"].lower() not in ("coach", "none", ""))
    if has_coach:
        coach_framing = f"""IMPORTANT: {data['athlete_first']} trains under coach {data['coach_name']}. {data['coach_first']}'s prescribed workouts and periodization are the primary plan. Your role is to provide supplementary data-driven analysis. Frame recommendations as "Observations for {data['coach_first']}" or "Topics to discuss with {data['coach_first']}." Never contradict or override the coach's plan. Open with a brief acknowledgment that this supplements {data['coach_first']}'s coaching."""
    else:
        coach_framing = "You are the primary coaching voice. Write recommendations directly to the athlete."

    system = SYSTEM_PROMPT.format(coach_framing=coach_framing)
    user = _build_user_prompt(data)

    # Call LLM
    try:
        raw_text = _call_llm(system, user)
        # Ensure it's wrapped in HTML
        if not raw_text.strip().startswith("<p"):
            raw_text = f"<p>{raw_text}</p>"
        _save_cache(data_hash, raw_text)
        return raw_text
    except Exception as e:
        # Fall back to a simple message on failure
        return f"<p><em>Coaching assessment unavailable: {e}. Data is current as of {data['date']}.</em></p>"
