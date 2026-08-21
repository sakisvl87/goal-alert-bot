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

# 1 API request / 10 minutes
CHECK_INTERVAL = 600

API_URL = "https://api.5dollarfootballapi.com/v1/fixtures"

# Start model evaluation from 15'
MINUTE_START = 15

# Do not evaluate after 100'
MINUTE_END = 100

# Minimum final score for an alert
MIN_ALERT_SCORE = 70

# Rolling window
SNAPSHOT_WINDOW_MINUTES = 10

# Keep a little extra history
MAX_SNAPSHOTS = 8

STATE_FILE = Path("prediction_state.json")


# ============================================================
# HEALTH SERVER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        body = b"Goal Prediction Live - OK\n"

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header(
            "Content-Length",
            str(len(body))
        )
        self.end_headers()

        self.wfile.write(body)

    def do_HEAD(self):
        body = b"Goal Prediction Live - OK\n"

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
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
    except (TypeError, ValueError):
        return float(default)


def get_value(obj, *names):

    if not isinstance(obj, dict):
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

    if isinstance(value, dict):

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

    return 0, 0


def team_name(match, side):

    teams = match.get("teams") or {}

    team = teams.get(side) or {}

    return (
        team.get("name")
        or team.get("short_name")
        or team.get("shortName")
        or side.title()
    )


def get_score(match):

    goals = match.get("goals") or {}

    home = int(
        number(
            get_value(
                goals,
                "home",
                "Home"
            )
        )
    )

    away = int(
        number(
            get_value(
                goals,
                "away",
                "Away"
            )
        )
    )

    return home, away


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

    events = match.get("events") or []

    minutes = []

    for event in events:

        if not isinstance(event, dict):
            continue

        minute = event.get("minute")

        if minute is not None:

            try:
                minutes.append(
                    float(minute)
                )
            except Exception:
                pass

    if minutes:
        return int(max(minutes))

    return 0


# ============================================================
# EVENTS
# ============================================================

def analyse_events(match):

    events = match.get("events") or []

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

    current_minute = get_minute(match)

    for event in events:

        if not isinstance(event, dict):
            continue

        event_type = str(
            event.get("type", "")
        ).lower()

        side = event.get("team")

        minute = number(
            event.get("minute"),
            0
        )

        # ----------------------------------------------------
        # CORNER
        # ----------------------------------------------------

        if event_type == "corner":

            count = int(
                number(
                    event.get("count"),
                    1
                )
            )

            if side == "home":
                result["corners_home"] += count

            elif side == "away":
                result["corners_away"] += count

            age = current_minute - minute

            if 0 <= age <= 10:
                result["recent_corners"] += count

        # ----------------------------------------------------
        # RED CARD
        # ----------------------------------------------------

        elif event_type == "red_card":

            if side == "home":
                result["red_home"] += 1

            elif side == "away":
                result["red_away"] += 1

            age = current_minute - minute

            if 0 <= age <= 15:
                result["recent_red_cards"] += 1

        # ----------------------------------------------------
        # MISSED PENALTY
        # ----------------------------------------------------

        elif event_type == "missed_penalty":

            if side == "home":
                result["missed_penalty_home"] += 1

            elif side == "away":
                result["missed_penalty_away"] += 1

        # ----------------------------------------------------
        # GOAL
        # ----------------------------------------------------

        elif event_type == "goal":

            age = current_minute - minute

            if 0 <= age <= 10:
                result["recent_goals"] += 1

    return result


# ============================================================
# CURRENT STATISTICS
# ============================================================

def extract_snapshot(match):

    minute = get_minute(match)

    stats = match.get("statistics") or {}

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

    events = analyse_events(match)

    shots_h = sot_h + off_h
    shots_a = sot_a + off_a

    home_score, away_score = get_score(match)

    now = time.time()

    return {
        "timestamp": now,
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

        "corners_h": int(
            events["corners_home"]
        ),

        "corners_a": int(
            events["corners_away"]
        ),

        "red_cards": int(
            events["red_home"]
            + events["red_away"]
        ),

        "missed_penalties": int(
            events["missed_penalty_home"]
            + events["missed_penalty_away"]
        )
    }


# ============================================================
# SNAPSHOT MANAGEMENT
# ============================================================

def update_snapshots(
    fixture_state,
    current_snapshot
):

    snapshots = fixture_state.get(
        "snapshots",
        []
    )

    snapshots.append(
        current_snapshot
    )

    cutoff = (
        current_snapshot["timestamp"]
        - SNAPSHOT_WINDOW_MINUTES * 60
    )

    snapshots = [
        snapshot
        for snapshot in snapshots
        if snapshot.get(
            "timestamp",
            0
        ) >= cutoff
    ]

    snapshots = snapshots[
        -MAX_SNAPSHOTS:
    ]

    fixture_state["snapshots"] = snapshots

    return snapshots


# ============================================================
# 10-MINUTE PRESSURE
# ============================================================

def calculate_rolling_pressure(
    snapshots,
    current
):

    if not snapshots:
        return {
            "available": False,
            "sot_10": 0,
            "shots_10": 0,
            "danger_10": 0,
            "attacks_10": 0,
            "corners_10": 0,
            "pressure_10": 0,
            "acceleration_10": 0
        }

    current_time = current["timestamp"]

    cutoff = (
        current_time
        - SNAPSHOT_WINDOW_MINUTES * 60
    )

    old = None

    for snapshot in snapshots:

        if snapshot["timestamp"] <= cutoff:
            old = snapshot

    if old is None:

        # If we do not yet have a complete
        # 10-minute history, use the oldest
        # available snapshot but mark it
        # as incomplete.

        old = snapshots[0]

    sot_10 = (
        current["sot_h"]
        + current["sot_a"]
        - old["sot_h"]
        - old["sot_a"]
    )

    shots_10 = (
        current["shots_h"]
        + current["shots_a"]
        - old["shots_h"]
        - old["shots_a"]
    )

    danger_10 = (
        current["danger_h"]
        + current["danger_a"]
        - old["danger_h"]
        - old["danger_a"]
    )

    attacks_10 = (
        current["attacks_h"]
        + current["attacks_a"]
        - old["attacks_h"]
        - old["attacks_a"]
    )

    corners_10 = (
        current["corners_h"]
        + current["corners_a"]
        - old["corners_h"]
        - old["corners_a"]
    )

    # Prevent negative values caused by
    # provider corrections.

    sot_10 = max(0, sot_10)
    shots_10 = max(0, shots_10)
    danger_10 = max(0, danger_10)
    attacks_10 = max(0, attacks_10)
    corners_10 = max(0, corners_10)

    # --------------------------------------------------------
    # 10' PRESSURE SCORE
    # --------------------------------------------------------

    pressure = 0.0

    pressure += min(
        sot_10 / 3,
        1
    ) * 35

    pressure += min(
        shots_10 / 8,
        1
    ) * 20

    pressure += min(
        danger_10 / 25,
        1
    ) * 25

    pressure += min(
        attacks_10 / 45,
        1
    ) * 10

    pressure += min(
        corners_10 / 4,
        1
    ) * 10

    pressure = round(
        min(
            pressure,
            100
        )
    )

    # --------------------------------------------------------
    # ACCELERATION
    # --------------------------------------------------------

    acceleration = 0.0

    if len(snapshots) >= 3:

        previous = snapshots[-2]

        previous_pressure = (
            previous.get(
                "rolling_pressure",
                0
            )
        )

        acceleration = (
            pressure
            - previous_pressure
        )

    return {
        "available": len(snapshots) >= 2,

        "sot_10": int(sot_10),
        "shots_10": int(shots_10),
        "danger_10": int(danger_10),
        "attacks_10": int(attacks_10),
        "corners_10": int(corners_10),

        "pressure_10": int(pressure),

        "acceleration_10": round(
            acceleration,
            1
        )
    }


# ============================================================
# FINAL GOAL MODEL
# ============================================================

def calculate_prediction(
    match,
    fixture_state
):

    minute = get_minute(match)

    if minute < MINUTE_START:
        return None

    if minute > MINUTE_END:
        return None

    stats = match.get(
        "statistics"
    ) or {}

    if not stats:
        return None

    current = extract_snapshot(match)

    snapshots = update_snapshots(
        fixture_state,
        current
    )

    rolling = calculate_rolling_pressure(
        snapshots,
        current
    )

    # Store rolling pressure inside
    # current snapshot for next comparison.

    current["rolling_pressure"] = (
        rolling["pressure_10"]
    )

    current["rolling_available"] = (
        rolling["available"]
    )

    # --------------------------------------------------------
    # CURRENT TOTALS
    # --------------------------------------------------------

    total_sot = (
        current["sot_h"]
        + current["sot_a"]
    )

    total_shots = (
        current["shots_h"]
        + current["shots_a"]
    )

    total_dangerous = (
        current["danger_h"]
        + current["danger_a"]
    )

    total_corners = (
        current["corners_h"]
        + current["corners_a"]
    )

    total_attacks = (
        current["attacks_h"]
        + current["attacks_a"]
    )

    total_goals = (
        current["home_score"]
        + current["away_score"]
    )

    # ========================================================
    # BASE CURRENT PRESSURE
    # ========================================================

    base = 0.0

    # SOT
    base += min(
        total_sot / 8,
        1
    ) * 30

    # Dangerous attacks
    base += min(
        total_dangerous / 60,
        1
    ) * 25

    # Shots
    base += min(
        total_shots / 18,
        1
    ) * 15

    # Corners
    base += min(
        total_corners / 9,
        1
    ) * 10

    # Attacks
    base += min(
        total_attacks / 130,
        1
    ) * 5

    # ========================================================
    # TIME FACTOR
    # ========================================================

    if 15 <= minute < 30:

        if total_sot >= 3:
            base += 6

        if total_dangerous >= 20:
            base += 4

        if total_shots >= 7:
            base += 3

    elif 30 <= minute < 45:

        if total_sot >= 3:
            base += 7

        if total_dangerous >= 25:
            base += 5

        if total_shots >= 8:
            base += 3

    elif 45 <= minute < 60:

        base += 5

        if total_sot >= 4:
            base += 6

        if total_dangerous >= 30:
            base += 5

    else:

        base += 10

        if total_sot >= 4:
            base += 6

        if total_dangerous >= 35:
            base += 5

    # ========================================================
    # SCORE STATE
    # ========================================================

    if total_goals == 0:
        base += 6

    elif total_goals == 1:
        base += 3

    # ========================================================
    # 10-MINUTE PRESSURE
    # ========================================================

    rolling_pressure = rolling[
        "pressure_10"
    ]

    if rolling["available"]:

        # Rolling pressure is now a major
        # part of the model.

        base += (
            rolling_pressure
            * 0.20
        )

    # ========================================================
    # ACCELERATION
    # ========================================================

    acceleration = rolling[
        "acceleration_10"
    ]

    acceleration_bonus = 0

    if acceleration >= 20:
        acceleration_bonus = 12

    elif acceleration >= 15:
        acceleration_bonus = 9

    elif acceleration >= 10:
        acceleration_bonus = 7

    elif acceleration >= 6:
        acceleration_bonus = 4

    elif acceleration >= 3:
        acceleration_bonus = 2

    base += acceleration_bonus

    # ========================================================
    # EVENTS
    # ========================================================

    if current["red_cards"] > 0:
        base += 5

    if current["missed_penalties"] > 0:
        base += 4

    # ========================================================
    # FINAL SCORE
    # ========================================================

    final_score = int(
        min(
            round(base),
            99
        )
    )

    if final_score >= 85:
        level = "VERY HIGH"

    elif final_score >= 75:
        level = "HIGH"

    elif final_score >= 65:
        level = "MEDIUM"

    else:
        level = "LOW"

    return {
        "score": final_score,
        "base_score": int(
            min(
                round(base - acceleration_bonus),
                99
            )
        ),

        "level": level,

        "minute": minute,

        "home_score": current[
            "home_score"
        ],

        "away_score": current[
            "away_score"
        ],

        "sot_h": current["sot_h"],
        "sot_a": current["sot_a"],

        "shots_h": current["shots_h"],
        "shots_a": current["shots_a"],

        "danger_h": current["danger_h"],
        "danger_a": current["danger_a"],

        "attacks_h": current["attacks_h"],
        "attacks_a": current["attacks_a"],

        "poss_h": current["poss_h"],
        "poss_a": current["poss_a"],

        "corners_h": current["corners_h"],
        "corners_a": current["corners_a"],

        "rolling_pressure": rolling_pressure,

        "sot_10": rolling["sot_10"],
        "shots_10": rolling["shots_10"],
        "danger_10": rolling["danger_10"],
        "attacks_10": rolling["attacks_10"],
        "corners_10": rolling["corners_10"],

        "acceleration": acceleration,

        "acceleration_bonus": acceleration_bonus,

        "rolling_available": rolling[
            "available"
        ]
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

    if acceleration >= 10:
        trend = "📈 PRESSURE ACCELERATING"

    elif acceleration >= 5:
        trend = "📈 PRESSURE RISING"

    elif acceleration <= -5:
        trend = "📉 PRESSURE FALLING"

    else:
        trend = "➡️ PRESSURE STABLE"

    if prediction[
        "rolling_available"
    ]:

        rolling_text = (
            f"🔥 10′ Pressure: "
            f"{prediction['rolling_pressure']}/100\n"
            f"🎯 SOT last 10′: "
            f"{prediction['sot_10']}\n"
            f"⚽ Shots last 10′: "
            f"{prediction['shots_10']}\n"
            f"🔥 Dangerous last 10′: "
            f"{prediction['danger_10']}\n"
            f"🚩 Corners last 10′: "
            f"{prediction['corners_10']}\n"
        )

    else:

        rolling_text = (
            "⏳ 10′ history: "
            "NOT YET AVAILABLE\n"
        )

    return (
        "🔥 GOAL PREDICTION\n\n"

        f"{prediction['minute']}′ — "
        f"{home} "
        f"{prediction['home_score']}-"
        f"{prediction['away_score']} "
        f"{away}\n\n"

        f"🎯 FINAL SCORE: "
        f"{prediction['score']}/100\n"

        f"📊 BASE SCORE: "
        f"{prediction['base_score']}/100\n"

        f"⚡ LEVEL: "
        f"{prediction['level']}\n"

        f"{trend}\n\n"

        f"{rolling_text}\n"

        f"📈 Acceleration: "
        f"{prediction['acceleration']}\n"

        f"➕ Acceleration bonus: "
        f"{prediction['acceleration_bonus']}\n\n"

        f"📌 CURRENT STATS\n"

        f"🎯 SOT: "
        f"{prediction['sot_h']}-"
        f"{prediction['sot_a']}\n"

        f"⚽ Shots: "
        f"{prediction['shots_h']}-"
        f"{prediction['shots_a']}\n"

        f"🔥 Dangerous: "
        f"{prediction['danger_h']}-"
        f"{prediction['danger_a']}\n"

        f"🏃 Attacks: "
        f"{prediction['attacks_h']}-"
        f"{prediction['attacks_a']}\n"

        f"🚩 Corners: "
        f"{prediction['corners_h']}-"
        f"{prediction['corners_a']}\n\n"

        "⏱️ MODEL WINDOW: NEXT ~10′\n\n"

        "⚠️ Model score is not a guaranteed "
        "probability until validated by backtesting."
    )


# ============================================================
# ALERT LOGIC
# ============================================================

def should_send_alert(
    prediction,
    previous
):

    score = prediction["score"]

    old_score = number(
        previous.get("score"),
        0
    )

    rolling = prediction[
        "rolling_pressure"
    ]

    old_rolling = number(
        previous.get(
            "rolling_pressure"
        ),
        0
    )

    acceleration = prediction[
        "acceleration"
    ]

    # --------------------------------------------------------
    # First strong signal
    # --------------------------------------------------------

    if score >= MIN_ALERT_SCORE:

        if old_score < MIN_ALERT_SCORE:
            return True

    # --------------------------------------------------------
    # New strong 10' pressure
    # --------------------------------------------------------

    if (
        rolling >= 75
        and rolling >= old_rolling + 12
    ):
        return True

    # --------------------------------------------------------
    # Strong acceleration
    # --------------------------------------------------------

    if (
        acceleration >= 15
        and score >= 65
    ):
        return True

    # --------------------------------------------------------
    # Large final-score jump
    # --------------------------------------------------------

    if (
        score >= MIN_ALERT_SCORE
        and score >= old_score + 10
    ):
        return True

    return False


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

        print("")
        print(
            "--------------------------------------"
        )

        prediction_state = state.get(
            fixture_id,
            {}
        )

        prediction = calculate_prediction(
            match,
            prediction_state
        )

        minute = get_minute(match)

        print(
            f"LIVE: {home} vs {away}"
        )

        print(
            f"MINUTE: {minute}"
        )

        if prediction is None:

            print(
                "PREDICTION: NOT READY"
            )

            # Keep previous snapshots alive
            # if the provider temporarily has
            # unusable statistics.

            if prediction_state:
                new_state[
                    fixture_id
                ] = prediction_state

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
            f"10' PRESSURE: "
            f"{prediction['rolling_pressure']}/100"
        )

        print(
            f"10' SOT: "
            f"{prediction['sot_10']}"
        )

        print(
            f"10' SHOTS: "
            f"{prediction['shots_10']}"
        )

        print(
            f"10' DANGEROUS: "
            f"{prediction['danger_10']}"
        )

        print(
            f"10' CORNERS: "
            f"{prediction['corners_10']}"
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
        # ALERT
        # ----------------------------------------------------

        if should_send_alert(
            prediction,
            prediction_state
        ):

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
        ] = prediction_state

        # Current prediction
        # is stored separately from
        # the snapshot history.

        new_state[
            fixture_id
        ]["score"] = prediction[
            "score"
        ]

        new_state[
            fixture_id
        ]["base_score"] = prediction[
            "base_score"
        ]

        new_state[
            fixture_id
        ]["rolling_pressure"] = prediction[
            "rolling_pressure"
        ]

        new_state[
            fixture_id
        ]["acceleration"] = prediction[
            "acceleration"
        ]

        new_state[
            fixture_id
        ]["level"] = prediction[
            "level"
        ]

        new_state[
            fixture_id
        ]["last_minute"] = prediction[
            "minute"
        ]

        new_state[
            fixture_id
        ]["last_score"] = (
            f"{prediction['home_score']}-"
            f"{prediction['away_score']}"
        )

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
# API
# ============================================================

def get_live_matches():

    headers = {
        "Authorization":
            f"Bearer {API_KEY}"
    }

    params = {
        "status": "live",
        "include": "events,stats",
        "per_page": 500,
        "lang": "en"
    }

    response = requests.get(
        API_URL,
        headers=headers,
        params=params,
        timeout=30
    )

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

    # --------------------------------------------------------
    # RATE LIMIT
    # --------------------------------------------------------

    if response.status_code == 429:

        print(
            "API RATE LIMIT REACHED"
        )

        print(
            "Keeping previous state."
        )

        return None

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

    # API rate-limit response
    # do not destroy saved snapshots.

    if matches is None:

        print(
            "SCAN SKIPPED - API RATE LIMIT"
        )

        return

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
        "MODEL: CURRENT PRESSURE + "
        "ROLLING 10M PRESSURE + "
        "ACCELERATION"
    )

    print(
        "================================"
    )

    while True:

        try:

            scan()

        except Exception as error:

            print(
                "ERROR:",
                repr(error)
            )

        print(
            f"Waiting "
            f"{CHECK_INTERVAL} seconds..."
        )

        time.sleep(
            CHECK_INTERVAL
        )
