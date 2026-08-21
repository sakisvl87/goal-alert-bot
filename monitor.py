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

MINUTE_START = 15
MINUTE_END = 100

MIN_ALERT_SCORE = 70

# Rolling statistics window
SNAPSHOT_WINDOW = 600

# Minimum history required before calculating a 10-minute delta
MIN_HISTORY = 480

# Keep approximately 30 minutes of snapshots
MAX_SNAPSHOTS = 12

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

        tmp_file = STATE_FILE.with_suffix(
            ".tmp"
        )

        tmp_file.write_text(
            json.dumps(
                state,
                indent=2
            )
        )

        tmp_file.replace(
            STATE_FILE
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

        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage",

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

    teams = match.get(
        "teams"
    ) or {}

    team = teams.get(
        side
    ) or {}

    return (
        team.get("name")
        or team.get("short_name")
        or team.get("shortName")
        or side.title()
    )


def get_score(match):

    goals = match.get(
        "goals"
    ) or {}

    return (

        int(
            number(
                get_value(
                    goals,
                    "home",
                    "Home"
                )
            )
        ),

        int(
            number(
                get_value(
                    goals,
                    "away",
                    "Away"
                )
            )
        )
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

                return int(
                    elapsed
                )

        except Exception:

            pass

    # Fallback to events

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
# EVENTS
# ============================================================

def analyse_events(match):

    events = (
        match.get("events")
        or []
    )

    minute = get_minute(
        match
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

    corners = {
        "home": [],
        "away": []
    }

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

        event_minute = number(
            event.get(
                "minute"
            ),
            0
        )

        age = (
            minute
            - event_minute
        )

        # ----------------------------------------------------
        # CORNERS
        # ----------------------------------------------------

        if event_type == "corner":

            value = event.get(
                "count"
            )

            corners.setdefault(
                side,
                []
            ).append(

                1
                if value is None
                else max(
                    1,
                    int(
                        number(
                            value,
                            1
                        )
                    )
                )
            )

            if (
                0 <= age <= 10
            ):

                result[
                    "recent_corners"
                ] += 1

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
                0 <= age <= 15
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
                0 <= age <= 10
            ):

                result[
                    "recent_goals"
                ] += 1

    # --------------------------------------------------------
    # CORNER TOTALS
    # --------------------------------------------------------

    for side, key in (
        ("home", "corners_home"),
        ("away", "corners_away")
    ):

        values = corners.get(
            side,
            []
        )

        if not values:

            result[key] = 0

        elif max(values) > 1:

            result[key] = max(
                values
            )

        else:

            result[key] = len(
                values
            )

    return result


# ============================================================
# SNAPSHOT
# ============================================================

def extract_snapshot(match):

    stats = (
        match.get(
            "statistics"
        )
        or {}
    )

    (
        attacks_h,
        attacks_a
    ) = pair(
        stats,
        "attacks"
    )

    (
        dangerous_h,
        dangerous_a
    ) = pair(
        stats,
        "dangerous_attacks"
    )

    (
        sot_h,
        sot_a
    ) = pair(
        stats,
        "shots_on_target"
    )

    (
        off_h,
        off_a
    ) = pair(
        stats,
        "shots_off_target"
    )

    (
        poss_h,
        poss_a
    ) = pair(
        stats,
        "possession"
    )

    events = analyse_events(
        match
    )

    home_score, away_score = (
        get_score(match)
    )

    return {

        "timestamp": time.time(),

        "minute": get_minute(
            match
        ),

        "home_score": home_score,
        "away_score": away_score,

        "sot_h": int(sot_h),
        "sot_a": int(sot_a),

        "shots_h": int(
            sot_h + off_h
        ),

        "shots_a": int(
            sot_a + off_a
        ),

        "danger_h": int(
            dangerous_h
        ),

        "danger_a": int(
            dangerous_a
        ),

        "attacks_h": int(
            attacks_h
        ),

        "attacks_a": int(
            attacks_a
        ),

        "poss_h": int(
            poss_h
        ),

        "poss_a": int(
            poss_a
        ),

        "corners_h": int(
            events[
                "corners_home"
            ]
        ),

        "corners_a": int(
            events[
                "corners_away"
            ]
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
        )
    }


def snapshot_signature(snapshot):

    keys = (

        "minute",

        "home_score",
        "away_score",

        "sot_h",
        "sot_a",

        "shots_h",
        "shots_a",

        "danger_h",
        "danger_a",

        "attacks_h",
        "attacks_a",

        "poss_h",
        "poss_a",

        "corners_h",
        "corners_a",

        "red_cards",

        "missed_penalties"
    )

    return tuple(
        snapshot.get(
            key,
            0
        )
        for key in keys
    )


# ============================================================
# FIND 10-MINUTE REFERENCE SNAPSHOT
# ============================================================

def find_reference(
    snapshots,
    now
):

    target = (
        now
        - SNAPSHOT_WINDOW
    )

    # Prefer snapshot at least 10 minutes old

    old_snapshots = [

        snapshot
        for snapshot in snapshots

        if snapshot.get(
            "timestamp",
            0
        ) <= target
    ]

    if old_snapshots:

        reference = max(
            old_snapshots,
            key=lambda x:
                x.get(
                    "timestamp",
                    0
                )
        )

        age = int(
            now
            - reference[
                "timestamp"
            ]
        )

        return (
            reference,
            age
        )

    # Fallback if we have at least 8 minutes
    # of history

    old_snapshots = [

        snapshot
        for snapshot in snapshots

        if (
            now
            - snapshot.get(
                "timestamp",
                now
            )
        ) >= MIN_HISTORY
    ]

    if old_snapshots:

        reference = min(

            old_snapshots,

            key=lambda x:
                abs(
                    (
                        now
                        - x.get(
                            "timestamp",
                            now
                        )
                    )
                    - SNAPSHOT_WINDOW
                )
        )

        age = int(
            now
            - reference[
                "timestamp"
            ]
        )

        return (
            reference,
            age
        )

    return (
        None,
        0
    )


# ============================================================
# TRUE ROLLING 10-MINUTE PRESSURE
# ============================================================

def rolling_pressure(
    snapshots,
    current
):

    reference, age = (
        find_reference(
            snapshots,
            current[
                "timestamp"
            ]
        )
    )

    if reference is None:

        return {

            "available": False,

            "age": 0,

            "sot_10": 0,
            "shots_10": 0,
            "danger_10": 0,
            "attacks_10": 0,
            "corners_10": 0,

            "pressure_10": 0
        }

    def delta(key):

        return max(

            0,

            int(
                current.get(
                    key,
                    0
                )
                - reference.get(
                    key,
                    0
                )
            )
        )

    sot_10 = (
        delta("sot_h")
        + delta("sot_a")
    )

    shots_10 = (
        delta("shots_h")
        + delta("shots_a")
    )

    danger_10 = (
        delta("danger_h")
        + delta("danger_a")
    )

    attacks_10 = (
        delta("attacks_h")
        + delta("attacks_a")
    )

    corners_10 = (
        delta("corners_h")
        + delta("corners_a")
    )

    # --------------------------------------------------------
    # 10-MINUTE PRESSURE MODEL
    # --------------------------------------------------------

    pressure = 0

    # Shots on target
    pressure += (
        min(
            sot_10 / 3,
            1
        )
        * 40
    )

    # Total shots
    pressure += (
        min(
            shots_10 / 7,
            1
        )
        * 20
    )

    # Dangerous attacks
    pressure += (
        min(
            danger_10 / 20,
            1
        )
        * 25
    )

    # Attacks
    pressure += (
        min(
            attacks_10 / 40,
            1
        )
        * 10
    )

    # Corners
    pressure += (
        min(
            corners_10 / 3,
            1
        )
        * 5
    )

    pressure = int(
        min(
            round(
                pressure
            ),
            100
        )
    )

    return {

        "available": True,

        "age": age,

        "sot_10": sot_10,
        "shots_10": shots_10,
        "danger_10": danger_10,
        "attacks_10": attacks_10,
        "corners_10": corners_10,

        "pressure_10": pressure
    }


# ============================================================
# SAVE SNAPSHOT
# ============================================================

def append_snapshot(
    state,
    current
):

    snapshots = (
        state.get(
            "snapshots"
        )
        or []
    )

    # Avoid duplicate snapshots

    if (
        snapshots
        and snapshot_signature(
            snapshots[-1]
        )
        == snapshot_signature(
            current
        )
    ):

        snapshots[-1][
            "timestamp"
        ] = current[
            "timestamp"
        ]

        snapshots[-1][
            "minute"
        ] = current[
            "minute"
        ]

    else:

        snapshots.append(
            current
        )

    # Keep approximately last 30 minutes

    cutoff = (
        current[
            "timestamp"
        ]
        - 1800
    )

    state[
        "snapshots"
    ] = [

        snapshot

        for snapshot in snapshots

        if snapshot.get(
            "timestamp",
            0
        ) >= cutoff

    ][
        -MAX_SNAPSHOTS:
    ]


# ============================================================
# PREDICTION MODEL
# ============================================================

def calculate_prediction(
    match,
    state
):

    minute = get_minute(
        match
    )

    # --------------------------------------------------------
    # TIME FILTER
    # --------------------------------------------------------

    if (
        minute < MINUTE_START
        or minute > MINUTE_END
    ):

        return None

    stats = (
        match.get(
            "statistics"
        )
        or {}
    )

    if not stats:

        return None

    # --------------------------------------------------------
    # CURRENT SNAPSHOT
    # --------------------------------------------------------

    current = extract_snapshot(
        match
    )

    snapshots = (
        state.get(
            "snapshots"
        )
        or []
    )

    # --------------------------------------------------------
    # ROLLING 10-MINUTE PRESSURE
    # --------------------------------------------------------

    rolling = rolling_pressure(
        snapshots,
        current
    )

    # --------------------------------------------------------
    # ACCELERATION
    # --------------------------------------------------------

    acceleration = 0

    if (
        rolling["available"]
        and
        state.get(
            "last_rolling_pressure"
        ) is not None
    ):

        acceleration = round(

            rolling[
                "pressure_10"
            ]
            - number(
                state[
                    "last_rolling_pressure"
                ]
            ),

            1
        )

    # Save current snapshot

    append_snapshot(
        state,
        current
    )

    # --------------------------------------------------------
    # TOTAL CURRENT STATISTICS
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

    total_attacks = (
        current["attacks_h"]
        + current["attacks_a"]
    )

    total_corners = (
        current["corners_h"]
        + current["corners_a"]
    )

    total_goals = (
        current["home_score"]
        + current["away_score"]
    )

    # --------------------------------------------------------
    # BASE SCORE
    # --------------------------------------------------------

    base_score = 0

    # SOT
    base_score += (
        min(
            total_sot / 8,
            1
        )
        * 30
    )

    # Dangerous attacks
    base_score += (
        min(
            total_dangerous / 60,
            1
        )
        * 25
    )

    # Shots
    base_score += (
        min(
            total_shots / 18,
            1
        )
        * 15
    )

    # Corners
    base_score += (
        min(
            total_corners / 12,
            1
        )
        * 8
    )

    # Attacks
    base_score += (
        min(
            total_attacks / 130,
            1
        )
        * 5
    )

    # --------------------------------------------------------
    # TIME BONUS
    # --------------------------------------------------------

    if minute >= 60:

        base_score += 7

    elif minute >= 45:

        base_score += 4

    elif minute >= 30:

        base_score += 6

    else:

        base_score += 4

    # --------------------------------------------------------
    # SCORE STATE
    # --------------------------------------------------------

    if total_goals == 0:

        base_score += 5

    elif total_goals == 1:

        base_score += 2

    # --------------------------------------------------------
    # 10-MINUTE PRESSURE CONTRIBUTION
    # --------------------------------------------------------

    if rolling["available"]:

        base_score += (
            rolling[
                "pressure_10"
            ]
            * 0.22
        )

    # --------------------------------------------------------
    # ACCELERATION BONUS
    # --------------------------------------------------------

    acceleration_bonus = 0

    if acceleration >= 20:

        acceleration_bonus = 10

    elif acceleration >= 15:

        acceleration_bonus = 8

    elif acceleration >= 10:

        acceleration_bonus = 6

    elif acceleration >= 6:

        acceleration_bonus = 4

    elif acceleration >= 3:

        acceleration_bonus = 2

    if rolling["available"]:

        base_score += (
            acceleration_bonus
        )

    # --------------------------------------------------------
    # EVENTS
    # --------------------------------------------------------

    if current[
        "red_cards"
    ] > 0:

        base_score += 4

    if current[
        "missed_penalties"
    ] > 0:

        base_score += 3

    # --------------------------------------------------------
    # FINAL SCORE
    # --------------------------------------------------------

    score = int(

        min(
            round(
                base_score
            ),
            99
        )
    )

    # --------------------------------------------------------
    # LEVEL
    # --------------------------------------------------------

    if score >= 85:

        level = "VERY HIGH"

    elif score >= 75:

        level = "HIGH"

    elif score >= 65:

        level = "MEDIUM"

    else:

        level = "LOW"

    # --------------------------------------------------------
    # ESTIMATED GOAL PROBABILITY
    # --------------------------------------------------------
    #
    # This is MODEL ESTIMATE.
    # It is NOT calibrated probability yet.
    #

    probability = int(

        min(

            95,

            max(

                5,

                round(
                    score * 0.82
                )
            )
        )
    )

    # --------------------------------------------------------
    # SAVE ROLLING PRESSURE
    # --------------------------------------------------------

    if rolling["available"]:

        state[
            "last_rolling_pressure"
        ] = rolling[
            "pressure_10"
        ]

    return {

        **current,

        "score": score,

        "base_score": int(
            min(
                round(
                    base_score
                    - acceleration_bonus
                ),
                99
            )
        ),

        "goal_probability":
            probability,

        "level":
            level,

        "rolling_available":
            rolling[
                "available"
            ],

        "window_age_seconds":
            rolling[
                "age"
            ],

        "rolling_pressure":
            rolling[
                "pressure_10"
            ],

        "sot_10":
            rolling[
                "sot_10"
            ],

        "shots_10":
            rolling[
                "shots_10"
            ],

        "danger_10":
            rolling[
                "danger_10"
            ],

        "attacks_10":
            rolling[
                "attacks_10"
            ],

        "corners_10":
            rolling[
                "corners_10"
            ],

        "acceleration":
            acceleration,

        "acceleration_bonus":
            acceleration_bonus
    }


# ============================================================
# ALERT CONDITIONS
# ============================================================

def should_alert(
    prediction,
    old
):

    # No 10-minute history = no alert

    if not prediction[
        "rolling_available"
    ]:

        return False

    score = prediction[
        "score"
    ]

    rolling_pressure = (
        prediction[
            "rolling_pressure"
        ]
    )

    acceleration = (
        prediction[
            "acceleration"
        ]
    )

    old_score = number(
        old.get(
            "score"
        )
    )

    old_rolling_pressure = (
        number(
            old.get(
                "rolling_pressure"
            )
        )
    )

    # --------------------------------------------------------
    # Strong current pressure
    # --------------------------------------------------------

    if (
        score >= 75
        and
        rolling_pressure >= 70
        and
        old_score < 75
    ):

        return True

    # --------------------------------------------------------
    # Strong pressure acceleration
    # --------------------------------------------------------

    if (
        rolling_pressure >= 80
        and
        rolling_pressure
        >= old_rolling_pressure + 12
    ):

        return True

    # --------------------------------------------------------
    # Rapid pressure increase
    # --------------------------------------------------------

    if (
        acceleration >= 15
        and
        rolling_pressure >= 65
        and
        score >= 70
    ):

        return True

    # --------------------------------------------------------
    # Score + rolling pressure confirmation
    # --------------------------------------------------------

    if (
        score >= MIN_ALERT_SCORE
        and
        rolling_pressure >= 75
        and
        score >= old_score + 10
    ):

        return True

    return False


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

    acceleration = (
        prediction[
            "acceleration"
        ]
    )

    if acceleration >= 10:

        trend = (
            "📈 PRESSURE ACCELERATING"
        )

    elif acceleration >= 5:

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

    if prediction[
        "rolling_available"
    ]:

        history = (

            f"🔥 10′ Pressure: "
            f"{prediction['rolling_pressure']}/100\n"

            f"🎯 SOT last 10′: "
            f"{prediction['sot_10']}\n"

            f"⚽ Shots last 10′: "
            f"{prediction['shots_10']}\n"

            f"🔥 Dangerous last 10′: "
            f"{prediction['danger_10']}\n"

            f"🏃 Attacks last 10′: "
            f"{prediction['attacks_10']}\n"

            f"🚩 Corners last 10′: "
            f"{prediction['corners_10']}\n"
        )

    else:

        history = (
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

        f"🎯 Estimated goal probability "
        f"next ~10′: "
        f"{prediction['goal_probability']}%\n"

        f"📊 Pressure score: "
        f"{prediction['score']}/100\n"

        f"⚡ Level: "
        f"{prediction['level']}\n"

        f"{trend}\n\n"

        f"{history}\n"

        f"📈 Acceleration: "
        f"{prediction['acceleration']}\n\n"

        f"CURRENT SOT: "
        f"{prediction['sot_h']}-"
        f"{prediction['sot_a']} | "

        f"SHOTS: "
        f"{prediction['shots_h']}-"
        f"{prediction['shots_a']}\n"

        f"DANGEROUS: "
        f"{prediction['danger_h']}-"
        f"{prediction['danger_a']} | "

        f"CORNERS: "
        f"{prediction['corners_h']}-"
        f"{prediction['corners_a']}\n\n"

        "⚠️ Model estimate; "
        "not calibrated until backtesting."
    )


# ============================================================
# PROCESS MATCHES
# ============================================================

def process_matches(
    matches
):

    state = load_state()

    new_state = {}

    alerts = 0

    for match in matches:

        fixture_id = str(
            match.get(
                "id"
            )
        )

        if fixture_id == "None":

            continue

        fixture_state = (
            state.get(
                fixture_id,
                {}
            )
        )

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

        print(
            f"LIVE: "
            f"{home} vs {away}"
        )

        prediction = (
            calculate_prediction(
                match,
                fixture_state
            )
        )

        if prediction is None:

            print(
                f"MINUTE: "
                f"{get_minute(match)}"
            )

            print(
                "PREDICTION: NOT READY"
            )

            if fixture_state:

                new_state[
                    fixture_id
                ] = fixture_state

            continue

        print(
            f"MINUTE: "
            f"{prediction['minute']}"
        )

        print(
            f"BASE SCORE: "
            f"{prediction['base_score']}/100"
        )

        print(
            f"FINAL SCORE: "
            f"{prediction['score']}/100"
        )

        print(
            f"ESTIMATED GOAL "
            f"PROBABILITY: "
            f"{prediction['goal_probability']}%"
        )

        print(
            f"LEVEL: "
            f"{prediction['level']}"
        )

        print(
            f"10' HISTORY AVAILABLE: "
            f"{prediction['rolling_available']}"
        )

        print(
            f"10' WINDOW AGE: "
            f"{prediction['window_age_seconds']} sec"
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

        if should_alert(
            prediction,
            fixture_state
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

                fixture_state[
                    "last_alert_minute"
                ] = prediction[
                    "minute"
                ]

                fixture_state[
                    "last_alert_score"
                ] = prediction[
                    "score"
                ]

            except Exception as error:

                print(
                    "TELEGRAM ERROR:",
                    repr(error)
                )

        # ----------------------------------------------------
        # UPDATE STATE
        # ----------------------------------------------------

        fixture_state.update({

            "score":
                prediction[
                    "score"
                ],

            "base_score":
                prediction[
                    "base_score"
                ],

            "goal_probability":
                prediction[
                    "goal_probability"
                ],

            "rolling_pressure":
                prediction[
                    "rolling_pressure"
                ],

            "acceleration":
                prediction[
                    "acceleration"
                ],

            "level":
                prediction[
                    "level"
                ],

            "last_minute":
                prediction[
                    "minute"
                ],

            "last_score":
                f"{prediction['home_score']}-"
                f"{prediction['away_score']}"
        })

        new_state[
            fixture_id
        ] = fixture_state

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

        "status":
            "live",

        "include":
            "events,stats",

        "per_page":
            500,

        "lang":
            "en"
    }

    response = requests.get(

        API_URL,

        headers=headers,

        params=params,

        timeout=30
    )

    # --------------------------------------------------------
    # RATE LIMIT
    # --------------------------------------------------------

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
    # 429
    # --------------------------------------------------------

    if response.status_code == 429:

        print(
            "API RATE LIMIT REACHED - "
            "saved state preserved"
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

    if matches is None:

        print(
            "SCAN SKIPPED - "
            "API RATE LIMIT"
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
        "TRUE 10M SNAPSHOT DELTA + "
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
