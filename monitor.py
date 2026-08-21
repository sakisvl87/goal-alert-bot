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

CHECK_INTERVAL = 600

API_URL = "https://api.5dollarfootballapi.com/v1/fixtures"

MIN_ALERT_SCORE = 70

MIN_EVALUATION_MINUTE = 15
MAX_EVALUATION_MINUTE = 130

# Snapshot history
MAX_SNAPSHOT_AGE_SECONDS = 35 * 60
TARGET_WINDOW_SECONDS = 10 * 60
MIN_REFERENCE_AGE_SECONDS = 8 * 60
MAX_REFERENCE_AGE_SECONDS = 13 * 60

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
# BASIC HELPERS
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
# STATE
# ============================================================

def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        data = json.loads(
            STATE_FILE.read_text()
        )

        if isinstance(data, dict):
            return data

    except Exception as error:
        print(
            "STATE LOAD ERROR:",
            repr(error)
        )

    return {}


def save_state(state):
    temp_file = STATE_FILE.with_suffix(".tmp")

    try:
        temp_file.write_text(
            json.dumps(
                state,
                indent=2
            )
        )

        temp_file.replace(
            STATE_FILE
        )

    except Exception as error:
        print(
            "STATE SAVE ERROR:",
            repr(error)
        )

        try:
            if temp_file.exists():
                temp_file.unlink()
        except Exception:
            pass


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

            if dt.tzinfo is None:
                dt = dt.replace(
                    tzinfo=timezone.utc
                )

            elapsed = (
                datetime.now(timezone.utc)
                - dt
            ).total_seconds() / 60

            if 0 <= elapsed <= 150:
                return int(elapsed)

        except Exception:
            pass

    events = match.get("events") or []

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
# EVENTS
# ============================================================

def analyse_events(match):

    events = match.get(
        "events"
    ) or []

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

        # ----------------------------------------------------
        # CORNERS
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

            if side == "home":
                result[
                    "corners_home"
                ] += count

            elif side == "away":
                result[
                    "corners_away"
                ] += count

            if (
                0
                <= current_minute - minute
                <= 10
            ):
                result[
                    "recent_corners"
                ] += count

        # ----------------------------------------------------
        # RED CARDS
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

            if (
                0
                <= current_minute - minute
                <= 15
            ):
                result[
                    "recent_red_cards"
                ] += 1

        # ----------------------------------------------------
        # MISSED PENALTY
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
        # GOAL
        # ----------------------------------------------------

        elif event_type == "goal":

            if (
                0
                <= current_minute - minute
                <= 10
            ):
                result[
                    "recent_goals"
                ] += 1

    return result


# ============================================================
# CURRENT STATISTICS
# ============================================================

def extract_statistics(match):

    stats = match.get(
        "statistics"
    ) or {}

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

    events = analyse_events(
        match
    )

    shots_h = sot_h + off_h
    shots_a = sot_a + off_a

    return {
        "attacks_h": int(attacks_h),
        "attacks_a": int(attacks_a),

        "danger_h": int(dangerous_h),
        "danger_a": int(dangerous_a),

        "sot_h": int(sot_h),
        "sot_a": int(sot_a),

        "shots_h": int(shots_h),
        "shots_a": int(shots_a),

        "poss_h": int(poss_h),
        "poss_a": int(poss_a),

        "corners_h": int(
            events["corners_home"]
        ),

        "corners_a": int(
            events["corners_away"]
        ),

        "recent_corners": int(
            events["recent_corners"]
        ),

        "red_cards": int(
            events["red_home"]
            + events["red_away"]
        ),

        "missed_penalties": int(
            events[
                "missed_penalty_home"
            ]
            + events[
                "missed_penalty_away"
            ]
        ),

        "recent_goals": int(
            events["recent_goals"]
        )
    }


# ============================================================
# SNAPSHOT
# ============================================================

def create_snapshot(match):

    minute = get_minute(
        match
    )

    stats = extract_statistics(
        match
    )

    return {
        "timestamp": time.time(),
        "minute": minute,

        "attacks_h": stats["attacks_h"],
        "attacks_a": stats["attacks_a"],

        "danger_h": stats["danger_h"],
        "danger_a": stats["danger_a"],

        "sot_h": stats["sot_h"],
        "sot_a": stats["sot_a"],

        "shots_h": stats["shots_h"],
        "shots_a": stats["shots_a"],

        "poss_h": stats["poss_h"],
        "poss_a": stats["poss_a"],

        "corners_h": stats["corners_h"],
        "corners_a": stats["corners_a"],

        "red_cards": stats["red_cards"],
        "missed_penalties": stats[
            "missed_penalties"
        ],

        "recent_goals": stats[
            "recent_goals"
        ]
    }


# ============================================================
# SNAPSHOT HISTORY
# ============================================================

def clean_snapshots(history):

    now = time.time()

    cleaned = []

    for snapshot in history:

        if not isinstance(
            snapshot,
            dict
        ):
            continue

        timestamp = number(
            snapshot.get(
                "timestamp"
            ),
            0
        )

        if timestamp <= 0:
            continue

        if (
            now - timestamp
            <= MAX_SNAPSHOT_AGE_SECONDS
        ):
            cleaned.append(
                snapshot
            )

    cleaned.sort(
        key=lambda x: number(
            x.get(
                "timestamp"
            ),
            0
        )
    )

    # Remove exact duplicate timestamps
    result = []

    last_timestamp = None

    for snapshot in cleaned:

        timestamp = number(
            snapshot.get(
                "timestamp"
            ),
            0
        )

        if (
            last_timestamp is not None
            and abs(
                timestamp
                - last_timestamp
            ) < 1
        ):
            continue

        result.append(
            snapshot
        )

        last_timestamp = timestamp

    return result


def find_reference_snapshot(history):

    if not history:
        return None

    now = time.time()

    candidates = []

    for snapshot in history:

        timestamp = number(
            snapshot.get(
                "timestamp"
            ),
            0
        )

        if timestamp <= 0:
            continue

        age = now - timestamp

        if (
            MIN_REFERENCE_AGE_SECONDS
            <= age
            <= MAX_REFERENCE_AGE_SECONDS
        ):
            candidates.append(
                (
                    abs(
                        age
                        - TARGET_WINDOW_SECONDS
                    ),
                    snapshot
                )
            )

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: x[0]
    )

    return candidates[0][1]


# ============================================================
# DELTA CALCULATION
# ============================================================

def calculate_delta(
    current,
    previous
):

    if previous is None:

        return {
            "available": False,
            "age_seconds": 0,

            "sot": 0,
            "shots": 0,
            "dangerous": 0,
            "attacks": 0,
            "corners": 0,

            "pressure": 0
        }

    current_time = time.time()

    previous_time = number(
        previous.get(
            "timestamp"
        ),
        0
    )

    age = max(
        0,
        current_time - previous_time
    )

    sot_delta = max(
        0,
        (
            current["sot_h"]
            + current["sot_a"]
        )
        - (
            number(
                previous.get(
                    "sot_h"
                ),
                0
            )
            + number(
                previous.get(
                    "sot_a"
                ),
                0
            )
        )
    )

    shots_delta = max(
        0,
        (
            current["shots_h"]
            + current["shots_a"]
        )
        - (
            number(
                previous.get(
                    "shots_h"
                ),
                0
            )
            + number(
                previous.get(
                    "shots_a"
                ),
                0
            )
        )
    )

    dangerous_delta = max(
        0,
        (
            current["danger_h"]
            + current["danger_a"]
        )
        - (
            number(
                previous.get(
                    "danger_h"
                ),
                0
            )
            + number(
                previous.get(
                    "danger_a"
                ),
                0
            )
        )
    )

    attacks_delta = max(
        0,
        (
            current["attacks_h"]
            + current["attacks_a"]
        )
        - (
            number(
                previous.get(
                    "attacks_h"
                ),
                0
            )
            + number(
                previous.get(
                    "attacks_a"
                ),
                0
            )
        )
    )

    # --------------------------------------------------------
    # IMPORTANT:
    # Corners are cumulative totals.
    # We therefore calculate the delta from the snapshot.
    # This prevents errors such as 46-9.
    # --------------------------------------------------------

    corners_delta = max(
        0,
        (
            current["corners_h"]
            + current["corners_a"]
        )
        - (
            number(
                previous.get(
                    "corners_h"
                ),
                0
            )
            + number(
                previous.get(
                    "corners_a"
                ),
                0
            )
        )
    )

    # --------------------------------------------------------
    # 10-MINUTE PRESSURE
    # --------------------------------------------------------

    pressure = 0.0

    pressure += min(
        sot_delta / 3,
        1
    ) * 35

    pressure += min(
        shots_delta / 7,
        1
    ) * 20

    pressure += min(
        dangerous_delta / 25,
        1
    ) * 25

    pressure += min(
        corners_delta / 4,
        1
    ) * 10

    pressure += min(
        attacks_delta / 40,
        1
    ) * 10

    pressure = min(
        round(pressure),
        100
    )

    return {
        "available": True,
        "age_seconds": int(age),

        "sot": int(sot_delta),
        "shots": int(shots_delta),
        "dangerous": int(
            dangerous_delta
        ),
        "attacks": int(
            attacks_delta
        ),
        "corners": int(
            corners_delta
        ),

        "pressure": int(
            pressure
        )
    }


# ============================================================
# CURRENT PRESSURE
# ============================================================

def calculate_current_pressure(stats):

    total_sot = (
        stats["sot_h"]
        + stats["sot_a"]
    )

    total_shots = (
        stats["shots_h"]
        + stats["shots_a"]
    )

    total_dangerous = (
        stats["danger_h"]
        + stats["danger_a"]
    )

    total_attacks = (
        stats["attacks_h"]
        + stats["attacks_a"]
    )

    total_corners = (
        stats["corners_h"]
        + stats["corners_a"]
    )

    score = 0.0

    score += min(
        total_sot / 8,
        1
    ) * 30

    score += min(
        total_dangerous / 60,
        1
    ) * 25

    score += min(
        total_shots / 18,
        1
    ) * 15

    score += min(
        total_corners / 9,
        1
    ) * 10

    score += min(
        total_attacks / 130,
        1
    ) * 5

    return min(
        round(score),
        99
    )


# ============================================================
# PREDICTION MODEL
# ============================================================

def calculate_prediction(
    match,
    previous_prediction,
    history
):

    minute = get_minute(
        match
    )

    if (
        minute < MIN_EVALUATION_MINUTE
        or minute > MAX_EVALUATION_MINUTE
    ):
        return None

    stats = extract_statistics(
        match
    )

    current_pressure = (
        calculate_current_pressure(
            stats
        )
    )

    reference = find_reference_snapshot(
        history
    )

    ten_minute = calculate_delta(
        stats,
        reference
    )

    # --------------------------------------------------------
    # CURRENT PRESSURE
    # --------------------------------------------------------

    base_score = float(
        current_pressure
    )

    # --------------------------------------------------------
    # TRUE 10-MINUTE PRESSURE
    # --------------------------------------------------------

    ten_pressure = number(
        ten_minute.get(
            "pressure"
        ),
        0
    )

    # --------------------------------------------------------
    # ACCELERATION
    #
    # Compare current 10' pressure against previous
    # stored 10' pressure, not against cumulative stats.
    # --------------------------------------------------------

    previous_10_pressure = number(
        previous_prediction.get(
            "ten_minute_pressure"
        ),
        0
    )

    acceleration = (
        ten_pressure
        - previous_10_pressure
    )

    acceleration_bonus = 0

    if ten_minute["available"]:

        if acceleration >= 20:
            acceleration_bonus = 10

        elif acceleration >= 12:
            acceleration_bonus = 7

        elif acceleration >= 7:
            acceleration_bonus = 4

        elif acceleration >= 3:
            acceleration_bonus = 2

    # --------------------------------------------------------
    # TIME FACTOR
    # --------------------------------------------------------

    time_bonus = 0

    if 45 <= minute < 60:
        time_bonus = 4

    elif 60 <= minute < 75:
        time_bonus = 7

    elif 75 <= minute <= 100:
        time_bonus = 10

    # --------------------------------------------------------
    # SCORE STATE
    # --------------------------------------------------------

    home_score, away_score = get_score(
        match
    )

    total_goals = (
        home_score
        + away_score
    )

    score_bonus = 0

    if total_goals == 0:
        score_bonus = 6

    elif total_goals == 1:
        score_bonus = 3

    # --------------------------------------------------------
    # EVENTS
    # --------------------------------------------------------

    events = analyse_events(
        match
    )

    event_bonus = 0

    total_corners = (
        stats["corners_h"]
        + stats["corners_a"]
    )

    if total_corners >= 5:
        event_bonus += 3

    if events[
        "recent_corners"
    ] >= 2:
        event_bonus += 4

    if events[
        "recent_red_cards"
    ] > 0:
        event_bonus += 5

    if (
        events[
            "missed_penalty_home"
        ]
        + events[
            "missed_penalty_away"
        ]
        > 0
    ):
        event_bonus += 4

    # --------------------------------------------------------
    # FINAL SCORE
    # --------------------------------------------------------

    final_score = (
        base_score
        + time_bonus
        + score_bonus
        + event_bonus
        + acceleration_bonus
    )

    # Give the true 10' pressure meaningful weight only
    # when history exists.

    if ten_minute["available"]:

        final_score += (
            ten_pressure * 0.20
        )

    final_score = min(
        round(final_score),
        99
    )

    # --------------------------------------------------------
    # LEVEL
    # --------------------------------------------------------

    if final_score >= 85:
        level = "VERY HIGH"

    elif final_score >= 75:
        level = "HIGH"

    elif final_score >= 65:
        level = "MEDIUM"

    else:
        level = "LOW"

    # --------------------------------------------------------
    # UNCALIBRATED ESTIMATE
    #
    # NOT a real statistical probability.
    # --------------------------------------------------------

    estimated_probability = min(
        90,
        max(
            5,
            round(
                final_score * 0.75
            )
        )
    )

    return {
        "timestamp": time.time(),

        "minute": minute,

        "base_score": int(
            current_pressure
        ),

        "score": int(
            final_score
        ),

        "level": level,

        "estimated_goal_probability":
            int(
                estimated_probability
            ),

        "ten_history_available":
            bool(
                ten_minute[
                    "available"
                ]
            ),

        "ten_window_age_seconds":
            int(
                ten_minute[
                    "age_seconds"
                ]
            ),

        "ten_minute_pressure":
            int(
                ten_pressure
            ),

        "ten_sot":
            int(
                ten_minute[
                    "sot"
                ]
            ),

        "ten_shots":
            int(
                ten_minute[
                    "shots"
                ]
            ),

        "ten_dangerous":
            int(
                ten_minute[
                    "dangerous"
                ]
            ),

        "ten_attacks":
            int(
                ten_minute[
                    "attacks"
                ]
            ),

        "ten_corners":
            int(
                ten_minute[
                    "corners"
                ]
            ),

        "acceleration":
            round(
                acceleration,
                1
            ),

        "acceleration_bonus":
            int(
                acceleration_bonus
            ),

        "sot_h":
            stats["sot_h"],

        "sot_a":
            stats["sot_a"],

        "shots_h":
            stats["shots_h"],

        "shots_a":
            stats["shots_a"],

        "danger_h":
            stats["danger_h"],

        "danger_a":
            stats["danger_a"],

        "attacks_h":
            stats["attacks_h"],

        "attacks_a":
            stats["attacks_a"],

        "poss_h":
            stats["poss_h"],

        "poss_a":
            stats["poss_a"],

        "corners_h":
            stats["corners_h"],

        "corners_a":
            stats["corners_a"]
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

    acceleration = number(
        prediction[
            "acceleration"
        ]
    )

    if acceleration >= 12:
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

    history = (
        "YES"
        if prediction[
            "ten_history_available"
        ]
        else "NO"
    )

    return (
        "🔥 GOAL PREDICTION\n\n"

        f"{prediction['minute']}′ — "
        f"{home} "
        f"{prediction['score'] if False else ''}"
        f"{prediction.get('home_score', '')}"
        f"{'-' if False else ''}"
        f"{prediction.get('away_score', '')} "
        f"{away}\n\n"

        f"🎯 Goal Pressure Score: "
        f"{prediction['score']}/100\n"

        f"🎯 Estimated Goal Probability: "
        f"{prediction['estimated_goal_probability']}%\n"

        f"⚡ Level: "
        f"{prediction['level']}\n"

        f"{trend}\n\n"

        f"🔥 CURRENT PRESSURE: "
        f"{prediction['base_score']}/100\n\n"

        f"⏱️ TRUE 10′ PRESSURE: "
        f"{prediction['ten_minute_pressure']}/100\n"

        f"📚 10′ HISTORY: "
        f"{history}\n"

        f"⏱️ WINDOW AGE: "
        f"{prediction['ten_window_age_seconds']} sec\n\n"

        f"🎯 10′ SOT: "
        f"{prediction['ten_sot']}\n"

        f"⚽ 10′ SHOTS: "
        f"{prediction['ten_shots']}\n"

        f"🔥 10′ DANGEROUS: "
        f"{prediction['ten_dangerous']}\n"

        f"🚩 10′ CORNERS: "
        f"{prediction['ten_corners']}\n\n"

        f"⚡ ACCELERATION: "
        f"{prediction['acceleration']}\n"

        f"➕ ACCELERATION BONUS: "
        f"{prediction['acceleration_bonus']}\n\n"

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
        f"{prediction['corners_a']}\n\n"

        "⚠️ Model signal, "
        "not a guaranteed probability."
    )


# ============================================================
# ALERT LOGIC
# ============================================================

def should_alert(
    prediction,
    previous_prediction
):

    # Never alert before a real 10' reference exists.
    if not prediction[
        "ten_history_available"
    ]:
        return False

    score = prediction[
        "score"
    ]

    ten_pressure = prediction[
        "ten_minute_pressure"
    ]

    acceleration = prediction[
        "acceleration"
    ]

    old_score = number(
        previous_prediction.get(
            "score"
        ),
        0
    )

    # --------------------------------------------------------
    # Strong current pressure
    # + actual recent pressure
    # --------------------------------------------------------

    strong_pressure = (
        score >= MIN_ALERT_SCORE
        and ten_pressure >= 35
    )

    strong_acceleration = (
        acceleration >= 7
    )

    # Initial alert
    if (
        strong_pressure
        and old_score < MIN_ALERT_SCORE
    ):
        return True

    # New alert only after meaningful improvement
    if (
        strong_pressure
        and strong_acceleration
        and score >= old_score + 8
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

        old_match_state = (
            state.get(
                fixture_id,
                {}
            )
        )

        previous_prediction = (
            old_match_state.get(
                "prediction",
                {}
            )
        )

        history = (
            old_match_state.get(
                "snapshots",
                []
            )
        )

        history = clean_snapshots(
            history
        )

        # ----------------------------------------------------
        # CREATE CURRENT SNAPSHOT
        # ----------------------------------------------------

        snapshot = create_snapshot(
            match
        )

        # ----------------------------------------------------
        # FIND PREDICTION
        # ----------------------------------------------------

        prediction = calculate_prediction(
            match,
            previous_prediction,
            history
        )

        # ----------------------------------------------------
        # SAVE SNAPSHOT EVEN WHEN PREDICTION IS NOT READY
        # ----------------------------------------------------

        history.append(
            snapshot
        )

        history = clean_snapshots(
            history
        )

        # ----------------------------------------------------
        # MATCH NOT READY
        # ----------------------------------------------------

        if prediction is None:

            print(
                "PREDICTION: NOT READY"
            )

            new_state[
                fixture_id
            ] = {
                "snapshots": history,
                "prediction":
                    previous_prediction
            }

            continue

        # ----------------------------------------------------
        # LOG PREDICTION
        # ----------------------------------------------------

        print(
            f"BASE SCORE: "
            f"{prediction['base_score']}/100"
        )

        print(
            f"FINAL SCORE: "
            f"{prediction['score']}/100"
        )

        print(
            f"ESTIMATED GOAL PROBABILITY: "
            f"{prediction['estimated_goal_probability']}%"
        )

        print(
            f"LEVEL: "
            f"{prediction['level']}"
        )

        print(
            f"10' HISTORY AVAILABLE: "
            f"{prediction['ten_history_available']}"
        )

        print(
            f"10' WINDOW AGE: "
            f"{prediction['ten_window_age_seconds']} sec"
        )

        print(
            f"10' PRESSURE: "
            f"{prediction['ten_minute_pressure']}/100"
        )

        print(
            f"10' SOT: "
            f"{prediction['ten_sot']}"
        )

        print(
            f"10' SHOTS: "
            f"{prediction['ten_shots']}"
        )

        print(
            f"10' DANGEROUS: "
            f"{prediction['ten_dangerous']}"
        )

        print(
            f"10' CORNERS: "
            f"{prediction['ten_corners']}"
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

        if should_alert(
            prediction,
            previous_prediction
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
        ] = {
            "snapshots": history,
            "prediction": prediction
        }

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
    # IMPORTANT:
    # Do NOT overwrite prediction state on 429.
    # --------------------------------------------------------

    if response.status_code == 429:

        print(
            "API RATE LIMIT HIT - "
            "KEEPING EXISTING STATE"
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

    # --------------------------------------------------------
    # API 429
    # --------------------------------------------------------

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
        "TRUE 10M SNAPSHOT DELTA + ACCELERATION"
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
