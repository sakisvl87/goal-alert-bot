import os
import time
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import requests
# ============================================================
# CONFIG
# ============================================================
API_KEY = os.environ["FIVE_DOLLAR_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
PORT = int(os.environ.get("PORT", "10000"))
# Free plan: 1 request / minute.
# We scan every 10 minutes.
CHECK_INTERVAL = 600
API_URL = "https://api.5dollarfootballapi.com/v1/fixtures"
# Goal Pressure Score threshold.
MIN_ALERT_SCORE = 70
# How much the BASE score must increase before
# another alert is allowed.
RE_ALERT_INCREASE = 8
# API retry configuration.
MAX_API_RETRIES = 3
DEFAULT_RETRY_SECONDS = 65
STATE_FILE = Path("prediction_state.json")
# ============================================================
# HEALTH SERVER
# ============================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"Goal Prediction Live - OK\n"
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/plain"
        )
        self.send_header(
            "Content-Length",
            str(len(body))
        )
        self.end_headers()
        self.wfile.write(body)
    def do_HEAD(self):
        body = b"Goal Prediction Live - OK\n"
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/plain"
        )
        self.send_header(
            "Content-Length",
            str(len(body))
        )
        self.end_headers()
    def log_message(self, *args):
        pass
def start_server():
    server = ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler
    )
    print(
        f"Health server listening on port {PORT}"
    )
    server.serve_forever()
# ============================================================
# STATE
# ============================================================
def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(
            STATE_FILE.read_text()
        )
    except Exception as error:
        print(
            "STATE LOAD ERROR:",
            repr(error)
        )
        return {}
def save_state(state):
    try:
        STATE_FILE.write_text(
            json.dumps(
                state,
                indent=2
            )
        )
    except Exception as error:
        print(
            "STATE SAVE ERROR:",
            repr(error)
        )
# ============================================================
# TELEGRAM
# ============================================================
def send_telegram(message):
    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data={
            "chat_id": CHAT_ID,
            "text": message
        },
        timeout=20
    )
    response.raise_for_status()
    result = response.json()
    if result.get("ok") is not True:
        raise RuntimeError(
            f"Telegram error: {result}"
        )
    print(
        "TELEGRAM PREDICTION: SENT"
    )
# ============================================================
# HELPERS
# ============================================================
def number(value, default=0):
    try:
        return float(value)
    except (
        TypeError,
        ValueError
    ):
        return float(default)
def get_value(obj, *names):
    if not isinstance(
        obj,
        dict
    ):
        return 0
    for name in names:
        if name in obj:
            return obj[name]
    return 0
def pair(stats, *names):
    value = get_value(
        stats,
        *names
    )
    if isinstance(
        value,
        dict
    ):
        home = get_value(
            value,
            "home",
            "Home"
        )
        away = get_value(
            value,
            "away",
            "Away"
        )
        return (
            number(home),
            number(away)
        )
    return (
        0,
        0
    )
def team_name(match, side):
    teams = (
        match.get("teams")
        or {}
    )
    team = (
        teams.get(side)
        or {}
    )
    return (
        team.get("name")
        or team.get("short_name")
        or team.get("shortName")
        or side.title()
    )
def get_score(match):
    goals = (
        match.get("goals")
        or {}
    )
    home = get_value(
        goals,
        "home",
        "Home"
    )
    away = get_value(
        goals,
        "away",
        "Away"
    )
    return (
        int(number(home)),
        int(number(away))
    )
# ============================================================
# MATCH MINUTE
# ============================================================
def get_minute(match):
    kickoff = (
        match.get("kickoff_utc")
        or match.get("kickoff")
    )
    if kickoff:
        try:
            dt = datetime.fromisoformat(
                kickoff.replace(
                    "Z",
                    "+00:00"
                )
            )
            elapsed = (
                datetime.now(timezone.utc)
                - dt
            ).total_seconds() / 60
            if 0 <= elapsed <= 130:
                return int(elapsed)
        except Exception:
            pass
    # Fallback to event minute.
    events = (
        match.get("events")
        or []
    )
    minutes = []
    for event in events:
        if not isinstance(
            event,
            dict
        ):
            continue
        minute = event.get(
            "minute"
        )
        if minute is not None:
            try:
                minutes.append(
                    float(minute)
                )
            except Exception:
                pass
    if minutes:
        return int(
            max(minutes)
        )
    return 0
# ============================================================
# CORNERS
# ============================================================
def get_corners(match, events):
    """
    Prefer the fixture's cumulative corner totals.
    If unavailable, fall back to counting corner events.
    """
    corners = (
        match.get("corners")
        or {}
    )
    home = get_value(
        corners,
        "home",
        "Home"
    )
    away = get_value(
        corners,
        "away",
        "Away"
    )
    if home is not None and away is not None:
        try:
            home_value = int(
                number(home)
            )
            away_value = int(
                number(away)
            )
            # Only accept if at least one value
            # is actually available.
            if (
                home is not None
                or away is not None
            ):
                return (
                    home_value,
                    away_value
                )
        except Exception:
            pass
    # --------------------------------------------------------
    # Fallback: count corner events
    # --------------------------------------------------------
    corners_home = 0
    corners_away = 0
    for event in events:
        if not isinstance(
            event,
            dict
        ):
            continue
        event_type = str(
            event.get(
                "type",
                ""
            )
        ).lower()
        if event_type != "corner":
            continue
        side = event.get(
            "team"
        )
        count = int(
            number(
                event.get(
                    "count"
                ),
                1
            )
        )
        if side == "home":
            corners_home += count
        elif side == "away":
            corners_away += count
    return (
        corners_home,
        corners_away
    )
# ============================================================
# EVENTS
# ============================================================
def analyse_events(match):
    events = (
        match.get("events")
        or []
    )
    result = {
        "corners_home": 0,
        "corners_away": 0,
        "red_home": 0,
        "red_away": 0,
        "missed_penalty_home": 0,
        "missed_penalty_away": 0,
        "recent_goals": 0,
        "recent_corners": 0,
        "recent_red_cards": 0
    }
    current_minute = get_minute(
        match
    )
    # --------------------------------------------------------
    # Corners
    # --------------------------------------------------------
    (
        result["corners_home"],
        result["corners_away"]
    ) = get_corners(
        match,
        events
    )
    # --------------------------------------------------------
    # Other events
    # --------------------------------------------------------
    for event in events:
        if not isinstance(
            event,
            dict
        ):
            continue
        event_type = str(
            event.get(
                "type",
                ""
            )
        ).lower()
        side = event.get(
            "team"
        )
        minute = number(
            event.get(
                "minute"
            ),
            0
        )
        age = (
            current_minute
            - minute
        )
        # ----------------------------------------------------
        # Recent corners
        # ----------------------------------------------------
        if event_type == "corner":
            count = int(
                number(
                    event.get(
                        "count"
                    ),
                    1
                )
            )
            if 0 <= age <= 10:
                result[
                    "recent_corners"
                ] += count
        # ----------------------------------------------------
        # Red cards
        # ----------------------------------------------------
        elif event_type == "red_card":
            if side == "home":
                result[
                    "red_home"
                ] += 1
            elif side == "away":
                result[
                    "red_away"
                ] += 1
            if 0 <= age <= 15:
                result[
                    "recent_red_cards"
                ] += 1
        # ----------------------------------------------------
        # Missed penalties
        # ----------------------------------------------------
        elif event_type == "missed_penalty":
            if side == "home":
                result[
                    "missed_penalty_home"
                ] += 1
            elif side == "away":
                result[
                    "missed_penalty_away"
                ] += 1
        # ----------------------------------------------------
        # Recent goals
        # ----------------------------------------------------
        elif event_type == "goal":
            if 0 <= age <= 10:
                result[
                    "recent_goals"
                ] += 1
    return result
# ============================================================
# PRESSURE MODEL
# ============================================================
def calculate_prediction(
    match,
    previous
):
    minute = get_minute(
        match
    )
    # --------------------------------------------------------
    # TIME FILTER
    # --------------------------------------------------------
    if minute < 15:
        return None
    if minute > 100:
        return None
    stats = (
        match.get("statistics")
        or {}
    )
    if not stats:
        return None
    # --------------------------------------------------------
    # LIVE STATS
    # --------------------------------------------------------
    attacks_h, attacks_a = pair(
        stats,
        "attacks"
    )
    dangerous_h, dangerous_a = pair(
        stats,
        "dangerous_attacks"
    )
    sot_h, sot_a = pair(
        stats,
        "shots_on_target"
    )
    off_h, off_a = pair(
        stats,
        "shots_off_target"
    )
    poss_h, poss_a = pair(
        stats,
        "possession"
    )
    shots_h = (
        sot_h
        + off_h
    )
    shots_a = (
        sot_a
        + off_a
    )
    events = analyse_events(
        match
    )
    # --------------------------------------------------------
    # TOTALS
    # --------------------------------------------------------
    total_sot = (
        sot_h
        + sot_a
    )
    total_shots = (
        shots_h
        + shots_a
    )
    total_dangerous = (
        dangerous_h
        + dangerous_a
    )
    total_attacks = (
        attacks_h
        + attacks_a
    )
    total_corners = (
        events["corners_home"]
        + events["corners_away"]
    )
    total_red = (
        events["red_home"]
        + events["red_away"]
    )
    total_missed_penalties = (
        events[
            "missed_penalty_home"
        ]
        + events[
            "missed_penalty_away"
        ]
    )
    home_score, away_score = get_score(
        match
    )
    total_goals = (
        home_score
        + away_score
    )
    # ========================================================
    # BASE SCORE
    # ========================================================
    base_score = 0.0
    # --------------------------------------------------------
    # SOT
    # Maximum 30
    # --------------------------------------------------------
    base_score += (
        min(
            total_sot / 8,
            1
        )
        * 30
    )
    # --------------------------------------------------------
    # Dangerous attacks
    # Maximum 25
    # --------------------------------------------------------
    base_score += (
        min(
            total_dangerous / 60,
            1
        )
        * 25
    )
    # --------------------------------------------------------
    # Shots
    # Maximum 15
    # --------------------------------------------------------
    base_score += (
        min(
            total_shots / 18,
            1
        )
        * 15
    )
    # --------------------------------------------------------
    # Corners
    # Maximum 10
    # --------------------------------------------------------
    base_score += (
        min(
            total_corners / 9,
            1
        )
        * 10
    )
    # --------------------------------------------------------
    # Attacks
    # Maximum 5
    # --------------------------------------------------------
    base_score += (
        min(
            total_attacks / 130,
            1
        )
        * 5
    )
    # ========================================================
    # TIME MODEL
    # ========================================================
    if 15 <= minute < 30:
        if total_sot >= 3:
            base_score += 6
        if total_dangerous >= 20:
            base_score += 4
        if total_shots >= 7:
            base_score += 3
    elif 30 <= minute < 45:
        if total_sot >= 3:
            base_score += 7
        if total_dangerous >= 25:
            base_score += 5
        if total_shots >= 8:
            base_score += 3
    elif 45 <= minute < 60:
        base_score += 5
        if total_sot >= 4:
            base_score += 6
        if total_dangerous >= 30:
            base_score += 5
    else:
        base_score += 10
        if total_sot >= 4:
            base_score += 6
        if total_dangerous >= 35:
            base_score += 5
    # ========================================================
    # SCORE STATE
    # ========================================================
    if total_goals == 0:
        base_score += 6
    elif total_goals == 1:
        base_score += 3
    # ========================================================
    # EVENTS
    # ========================================================
    if total_corners >= 5:
        base_score += 3
    if events["recent_corners"] >= 2:
        base_score += 4
    if events["recent_red_cards"] > 0:
        base_score += 5
    if total_missed_penalties > 0:
        base_score += 4
    # ========================================================
    # POSSESSION
    # ========================================================
    possession_difference = abs(
        poss_h
        - poss_a
    )
    if possession_difference >= 20:
        base_score += 2
    # ========================================================
    # BASE SCORE NORMALIZATION
    # ========================================================
    base_score = min(
        max(
            round(base_score),
            0
        ),
        99
    )
    # ========================================================
    # ACCELERATION
    # ========================================================
    previous_base_score = None
    if isinstance(
        previous,
        dict
    ):
        if (
            "base_score"
            in previous
        ):
            previous_base_score = number(
                previous.get(
                    "base_score"
                )
            )
    acceleration = 0.0
    acceleration_bonus = 0
    # --------------------------------------------------------
    # First observation
    # --------------------------------------------------------
    if previous_base_score is None:
        acceleration = 0.0
        acceleration_bonus = 0
    # --------------------------------------------------------
    # Existing observation
    # --------------------------------------------------------
    else:
        acceleration = (
            base_score
            - previous_base_score
        )
        if acceleration >= 8:
            acceleration_bonus = 7
        elif acceleration >= 5:
            acceleration_bonus = 4
        elif acceleration >= 3:
            acceleration_bonus = 2
        elif acceleration <= -8:
            acceleration_bonus = -3
    # ========================================================
    # FINAL SCORE
    # ========================================================
    final_score = (
        base_score
        + acceleration_bonus
    )
    final_score = min(
        max(
            round(final_score),
            0
        ),
        99
    )
    # ========================================================
    # CLASSIFICATION
    # ========================================================
    if final_score >= 85:
        level = "VERY HIGH"
    elif final_score >= 75:
        level = "HIGH"
    elif final_score >= 65:
        level = "MEDIUM"
    else:
        level = "LOW"
    # ========================================================
    # RESULT
    # ========================================================
    return {
        # Important state values
        "base_score": base_score,
        "score": final_score,
        "level": level,
        "minute": minute,
        "home_score": home_score,
        "away_score": away_score,
        "sot_h": int(sot_h),
        "sot_a": int(sot_a),
        "shots_h": int(shots_h),
        "shots_a": int(shots_a),
        "danger_h": int(dangerous_h),
        "danger_a": int(dangerous_a),
        "attacks_h": int(attacks_h),
        "attacks_a": int(attacks_a),
        "poss_h": int(poss_h),
        "poss_a": int(poss_a),
        "corners_h":
            events["corners_home"],
        "corners_a":
            events["corners_away"],
        "recent_corners":
            events["recent_corners"],
        "red_cards":
            total_red,
        "missed_penalties":
            total_missed_penalties,
        "recent_goals":
            events["recent_goals"],
        "acceleration":
            round(
                acceleration,
                1
            ),
        "acceleration_bonus":
            acceleration_bonus
    }
# ============================================================
# TELEGRAM MESSAGE
# ============================================================
def build_message(
    match,
    prediction
):
    home = team_name(
        match,
        "home"
    )
    away = team_name(
        match,
        "away"
    )
    acceleration = prediction[
        "acceleration"
    ]
    if acceleration >= 8:
        trend = (
            "📈 PRESSURE RISING FAST"
        )
    elif acceleration >= 3:
        trend = (
            "📈 PRESSURE RISING"
        )
    elif acceleration <= -5:
        trend = (
            "📉 PRESSURE FALLING"
        )
    else:
        trend = (
            "➡️ PRESSURE STABLE"
        )
    return (
        "🔥 GOAL PREDICTION\n\n"
        f"{prediction['minute']}′ — "
        f"{home} "
        f"{prediction['home_score']}-"
        f"{prediction['away_score']} "
        f"{away}\n\n"
        f"🎯 GOAL PRESSURE: "
        f"{prediction['score']}/100\n"
        f"📊 BASE SCORE: "
        f"{prediction['base_score']}/100\n"
        f"⚡ LEVEL: "
        f"{prediction['level']}\n"
        f"{trend}\n\n"
        f"📈 Acceleration: "
        f"{acceleration:+.1f}\n"
        f"➕ Acceleration bonus: "
        f"{prediction['acceleration_bonus']:+d}\n\n"
        f"🎯 SOT: "
        f"{prediction['sot_h']}-"
        f"{prediction['sot_a']}\n"
        f"⚽ Shots: "
        f"{prediction['shots_h']}-"
        f"{prediction['shots_a']}\n"
        f"🔥 Dangerous attacks: "
        f"{prediction['danger_h']}-"
        f"{prediction['danger_a']}\n"
        f"🏃 Attacks: "
        f"{prediction['attacks_h']}-"
        f"{prediction['attacks_a']}\n"
        f"🚩 Corners: "
        f"{prediction['corners_h']}-"
        f"{prediction['corners_a']}\n"
        f"📊 Possession: "
        f"{prediction['poss_h']}%-"
        f"{prediction['poss_a']}%\n\n"
        "⏱️ SIGNAL: NEXT ~10 MINUTES\n\n"
        "⚠️ Goal Pressure Score is a "
        "model signal, not a calibrated "
        "probability yet."
    )
# ============================================================
# PROCESS MATCHES
# ============================================================
def process_matches(matches):
    state = load_state()
    new_state = {}
    alerts = 0
    for match in matches:
        fixture_id = str(
            match.get("id")
        )
        if fixture_id == "None":
            continue
        home = team_name(
            match,
            "home"
        )
        away = team_name(
            match,
            "away"
        )
        minute = get_minute(
            match
        )
        print("")
        print(
            "--------------------------------------"
        )
        print(
            f"LIVE: {home} vs {away}"
        )
        print(
            f"MINUTE: {minute}"
        )
        previous = state.get(
            fixture_id,
            {}
        )
        if not isinstance(
            previous,
            dict
        ):
            previous = {}
        prediction = calculate_prediction(
            match,
            previous
        )
        if prediction is None:
            print(
                "PREDICTION: NOT READY"
            )
            continue
        print(
            f"BASE SCORE: "
            f"{prediction['base_score']}/100"
        )
        print(
            f"FINAL SCORE: "
            f"{prediction['score']}/100"
        )
        print(
            f"LEVEL: "
            f"{prediction['level']}"
        )
        print(
            f"ACCELERATION: "
            f"{prediction['acceleration']}"
        )
        print(
            f"ACCELERATION BONUS: "
            f"{prediction['acceleration_bonus']}"
        )
        print(
            f"SOT: "
            f"{prediction['sot_h']}-"
            f"{prediction['sot_a']}"
        )
        print(
            f"SHOTS: "
            f"{prediction['shots_h']}-"
            f"{prediction['shots_a']}"
        )
        print(
            f"DANGEROUS: "
            f"{prediction['danger_h']}-"
            f"{prediction['danger_a']}"
        )
        print(
            f"CORNERS: "
            f"{prediction['corners_h']}-"
            f"{prediction['corners_a']}"
        )
        # ----------------------------------------------------
        # ALERT LOGIC
        # ----------------------------------------------------
        old_base_score = None
        if "base_score" in previous:
            old_base_score = number(
                previous.get(
                    "base_score"
                )
            )
        should_alert = False
        # First high signal.
        if (
            prediction["score"]
            >= MIN_ALERT_SCORE
            and old_base_score is None
        ):
            should_alert = True
        # Existing match:
        # Alert only if base pressure has genuinely
        # increased by at least RE_ALERT_INCREASE.
        elif (
            prediction["score"]
            >= MIN_ALERT_SCORE
            and old_base_score is not None
            and prediction["base_score"]
            >= old_base_score
            + RE_ALERT_INCREASE
        ):
            should_alert = True
        if should_alert:
            message = build_message(
                match,
                prediction
            )
            try:
                send_telegram(
                    message
                )
                alerts += 1
            except Exception as error:
                print(
                    "TELEGRAM ERROR:",
                    repr(error)
                )
        # ----------------------------------------------------
        # SAVE STATE
        # ----------------------------------------------------
        new_state[
            fixture_id
        ] = prediction
    save_state(
        new_state
    )
    print("")
    print(
        f"PREDICTION STATE UPDATED: "
        f"{len(new_state)} matches, "
        f"{alerts} alerts"
    )
# ============================================================
# API REQUEST WITH RATE LIMIT PROTECTION
# ============================================================
def get_live_matches():
    headers = {
        "Authorization":
            f"Bearer {API_KEY}"
    }
    params = {
        "status":
            "live",
        "include":
            "events,stats",
        "per_page":
            500,
        "lang":
            "en"
    }
    for attempt in range(
        1,
        MAX_API_RETRIES + 1
    ):
        try:
            response = requests.get(
                API_URL,
                headers=headers,
                params=params,
                timeout=30
            )
            # ------------------------------------------------
            # RATE LIMIT INFO
            # ------------------------------------------------
            remaining = response.headers.get(
                "X-RateLimit-Remaining"
            )
            reset = response.headers.get(
                "X-RateLimit-Reset"
            )
            print(
                f"API RATE LIMIT REMAINING: "
                f"{remaining}"
            )
            if reset:
                print(
                    f"API RATE LIMIT RESET: "
                    f"{reset}"
                )
            # ------------------------------------------------
            # 429
            # ------------------------------------------------
            if response.status_code == 429:
                retry_after = response.headers.get(
                    "Retry-After"
                )
                try:
                    wait_seconds = int(
                        retry_after
                    )
                except (
                    TypeError,
                    ValueError
                ):
                    wait_seconds = (
                        DEFAULT_RETRY_SECONDS
                    )
                # Safety.
                wait_seconds = max(
                    wait_seconds,
                    5
                )
                print(
                    f"RATE LIMITED (429). "
                    f"Waiting {wait_seconds}s "
                    f"before retry..."
                )
                if attempt >= MAX_API_RETRIES:
                    raise RuntimeError(
                        "API rate limit persists "
                        "after retries."
                    )
                time.sleep(
                    wait_seconds
                )
                continue
            response.raise_for_status()
            data = response.json()
            if data.get(
                "success"
            ) != 1:
                raise RuntimeError(
                    f"API error: {data}"
                )
            return data.get(
                "data",
                []
            )
        except requests.RequestException as error:
            print(
                f"API REQUEST ERROR "
                f"(attempt {attempt}/"
                f"{MAX_API_RETRIES}):",
                repr(error)
            )
            if attempt >= MAX_API_RETRIES:
                raise
            time.sleep(
                DEFAULT_RETRY_SECONDS
            )
    return []
# ============================================================
# SCAN
# ============================================================
def scan():
    print("")
    print(
        "=============================="
    )
    print(
        "GOAL PREDICTION SCAN STARTED"
    )
    matches = get_live_matches()
    print(
        f"LIVE MATCHES: "
        f"{len(matches)}"
    )
    process_matches(
        matches
    )
    print(
        "GOAL PREDICTION SCAN FINISHED"
    )
# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    threading.Thread(
        target=start_server,
        daemon=True
    ).start()
    print(
        "GOAL PREDICTION BOT STARTED"
    )
    print(
        f"CHECK INTERVAL: "
        f"{CHECK_INTERVAL} seconds"
    )
    print(
        f"MIN ALERT SCORE: "
        f"{MIN_ALERT_SCORE}/100"
    )
    print(
        "MODEL: "
        "PRESSURE + ACCELERATION + EVENTS"
    )
    print(
        "================================"
    )
    while True:
        try:
            scan()
        except Exception as error:
            print(
                "SCAN ERROR:",
                repr(error)
            )
        print(
            f"Waiting "
            f"{CHECK_INTERVAL} seconds..."
        )
        time.sleep(
            CHECK_INTERVAL
        )
