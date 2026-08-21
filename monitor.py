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

# Persistent path can be supplied by Render/environment.
# If not supplied, use local working directory.
DATA_DIR = Path(os.environ.get("DATA_DIR", "."))
DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = DATA_DIR / "prediction_state.json"
SNAPSHOT_FILE = DATA_DIR / "statistics_snapshots.json"

SNAPSHOT_INTERVAL = CHECK_INTERVAL
SNAPSHOT_RETENTION_SECONDS = 45 * 60
TARGET_WINDOW_SECONDS = 10 * 60
WINDOW_TOLERANCE_SECONDS = 180

REQUEST_TIMEOUT = 30
TELEGRAM_TIMEOUT = 20


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
# FILE HELPERS
# ============================================================

def load_json(path, default):
    if not path.exists():
        return default

    try:
        with path.open(
            "r",
            encoding="utf-8"
        ) as file:
            data = json.load(file)

        return data

    except Exception as error:
        print(
            f"LOAD ERROR {path}:",
            repr(error)
        )

        return default


def save_json(path, data):
    temp_path = path.with_suffix(
        path.suffix + ".tmp"
    )

    try:
        with temp_path.open(
            "w",
            encoding="utf-8"
        ) as file:
            json.dump(
                data,
                file,
                indent=2,
                ensure_ascii=False
            )

        temp_path.replace(path)

    except Exception as error:
        print(
            f"SAVE ERROR {path}:",
            repr(error)
        )

        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass


# ============================================================
# STATE
# ============================================================

def load_state():
    return load_json(
        STATE_FILE,
        {}
    )


def save_state(state):
    save_json(
        STATE_FILE,
        state
    )


# ============================================================
# SNAPSHOTS
# ============================================================

def load_snapshots():
    data = load_json(
        SNAPSHOT_FILE,
        {}
    )

    if not isinstance(data, dict):
        return {}

    return data


def save_snapshots(snapshots):
    save_json(
        SNAPSHOT_FILE,
        snapshots
    )


# ============================================================
# GENERIC HELPERS
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

    return 0.0, 0.0


def team_name(match, side):
    teams = match.get("teams") or {}
    team = teams.get(side) or {}

    return (
        team.get("name")
        or team.get("short_name")
        or team.get("shortName")
        or side.title()
    )


def fixture_id(match):
    value = match.get("id")

    if value is None:
        return None

    return str(value)


# ============================================================
# SCORE
# ============================================================

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

            if 0 <= elapsed <= 140:
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

            age = (
                current_minute
                - minute
            )

            if 0 <= age <= 10:
                result[
                    "recent_corners"
                ] += count

        # ----------------------------------------------------
        # RED CARD
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

            age = (
                current_minute
                - minute
            )

            if 0 <= age <= 15:
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

            age = (
                current_minute
                - minute
            )

            if 0 <= age <= 10:
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

    shots_h = (
        sot_h
        + off_h
    )

    shots_a = (
        sot_a
        + off_a
    )

    corners_h = events[
        "corners_home"
    ]

    corners_a = events[
        "corners_away"
    ]

    home_score, away_score = get_score(
        match
    )

    return {
        "attacks_h": attacks_h,
        "attacks_a": attacks_a,
        "danger_h": dangerous_h,
        "danger_a": dangerous_a,
        "sot_h": sot_h,
        "sot_a": sot_a,
        "shots_h": shots_h,
        "shots_a": shots_a,
        "poss_h": poss_h,
        "poss_a": poss_a,
        "corners_h": corners_h,
        "corners_a": corners_a,
        "goals_h": home_score,
        "goals_a": away_score
    }


# ============================================================
# SNAPSHOT CREATION
# ============================================================

def create_snapshot(match):

    stats = extract_statistics(
        match
    )

    return {
        "timestamp": time.time(),
        "minute": get_minute(match),
        "stats": stats
    }


# ============================================================
# SNAPSHOT CLEANUP
# ============================================================

def cleanup_snapshots(snapshots):

    now = time.time()

    for fid in list(
        snapshots.keys()
    ):

        entries = snapshots.get(
            fid
        )

        if not isinstance(
            entries,
            list
        ):
            snapshots[fid] = []
            continue

        cleaned = []

        for item in entries:

            if not isinstance(
                item,
                dict
            ):
                continue

            timestamp = number(
                item.get(
                    "timestamp"
                ),
                0
            )

            if (
                timestamp > 0
                and now - timestamp
                <= SNAPSHOT_RETENTION_SECONDS
            ):
                cleaned.append(item)

        snapshots[fid] = cleaned


# ============================================================
# SAVE SNAPSHOT
# ============================================================

def store_snapshot(
    snapshots,
    match
):

    fid = fixture_id(
        match
    )

    if fid is None:
        return

    snapshot = create_snapshot(
        match
    )

    entries = snapshots.setdefault(
        fid,
        []
    )

    if not isinstance(
        entries,
        list
    ):
        entries = []
        snapshots[fid] = entries

    now = snapshot[
        "timestamp"
    ]

    # Avoid duplicate snapshots
    # from an accidental duplicate scan.
    if entries:

        last_timestamp = number(
            entries[-1].get(
                "timestamp"
            ),
            0
        )

        if (
            now - last_timestamp
            < 30
        ):
            entries[-1] = snapshot
        else:
            entries.append(
                snapshot
            )

    else:
        entries.append(
            snapshot
        )


# ============================================================
# FIND TRUE 10-MINUTE SNAPSHOT
# ============================================================

def find_10m_snapshot(
    snapshots,
    fid
):

    entries = snapshots.get(
        fid,
        []
    )

    if not isinstance(
        entries,
        list
    ):
        return None, 0

    if not entries:
        return None, 0

    now = time.time()

    target_time = (
        now
        - TARGET_WINDOW_SECONDS
    )

    candidates = []

    for snapshot in entries:

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

        age = now - timestamp

        if (
            abs(
                timestamp
                - target_time
            )
            <= WINDOW_TOLERANCE_SECONDS
        ):
            candidates.append(
                (
                    abs(
                        timestamp
                        - target_time
                    ),
                    snapshot,
                    age
                )
            )

    if not candidates:
        return None, 0

    candidates.sort(
        key=lambda x: x[0]
    )

    _, snapshot, age = candidates[0]

    return snapshot, int(age)


# ============================================================
# 10-MINUTE DELTA
# ============================================================

def calculate_10m_delta(
    current,
    previous
):

    if previous is None:
        return {
            "available": False,
            "pressure": 0,
            "sot": 0,
            "shots": 0,
            "dangerous": 0,
            "corners": 0,
            "attacks": 0,
            "goals": 0,
            "age": 0
        }

    current_stats = current[
        "stats"
    ]

    previous_stats = previous[
        "stats"
    ]

    def delta(name):
        return max(
            0,
            number(
                current_stats.get(
                    name
                ),
                0
            )
            - number(
                previous_stats.get(
                    name
                ),
                0
            )
        )

    current_sot = (
        number(
            current_stats.get(
                "sot_h"
            )
        )
        + number(
            current_stats.get(
                "sot_a"
            )
        )
    )

    previous_sot = (
        number(
            previous_stats.get(
                "sot_h"
            )
        )
        + number(
            previous_stats.get(
                "sot_a"
            )
        )
    )

    current_shots = (
        number(
            current_stats.get(
                "shots_h"
            )
        )
        + number(
            current_stats.get(
                "shots_a"
            )
        )
    )

    previous_shots = (
        number(
            previous_stats.get(
                "shots_h"
            )
        )
        + number(
            previous_stats.get(
                "shots_a"
            )
        )
    )

    current_dangerous = (
        number(
            current_stats.get(
                "danger_h"
            )
        )
        + number(
            current_stats.get(
                "danger_a"
            )
        )
    )

    previous_dangerous = (
        number(
            previous_stats.get(
                "danger_h"
            )
        )
        + number(
            previous_stats.get(
                "danger_a"
            )
        )
    )

    current_corners = (
        number(
            current_stats.get(
                "corners_h"
            )
        )
        + number(
            current_stats.get(
                "corners_a"
            )
        )
    )

    previous_corners = (
        number(
            previous_stats.get(
                "corners_h"
            )
        )
        + number(
            previous_stats.get(
                "corners_a"
            )
        )
    )

    current_attacks = (
        number(
            current_stats.get(
                "attacks_h"
            )
        )
        + number(
            current_stats.get(
                "attacks_a"
            )
        )
    )

    previous_attacks = (
        number(
            previous_stats.get(
                "attacks_h"
            )
        )
        + number(
            previous_stats.get(
                "attacks_a"
            )
        )
    )

    current_goals = (
        number(
            current_stats.get(
                "goals_h"
            )
        )
        + number(
            current_stats.get(
                "goals_a"
            )
        )
    )

    previous_goals = (
        number(
            previous_stats.get(
                "goals_h"
            )
        )
        + number(
            previous_stats.get(
                "goals_a"
            )
        )
    )

    delta_sot = max(
        0,
        current_sot - previous_sot
    )

    delta_shots = max(
        0,
        current_shots - previous_shots
    )

    delta_dangerous = max(
        0,
        current_dangerous
        - previous_dangerous
    )

    delta_corners = max(
        0,
        current_corners
        - previous_corners
    )

    delta_attacks = max(
        0,
        current_attacks
        - previous_attacks
    )

    delta_goals = max(
        0,
        current_goals
        - previous_goals
    )

    # --------------------------------------------------------
    # TRUE 10-MINUTE PRESSURE
    # --------------------------------------------------------

    pressure = 0.0

    pressure += min(
        delta_sot / 3,
        1
    ) * 35

    pressure += min(
        delta_dangerous / 25,
        1
    ) * 30

    pressure += min(
        delta_shots / 7,
        1
    ) * 20

    pressure += min(
        delta_corners / 4,
        1
    ) * 10

    pressure += min(
        delta_attacks / 40,
        1
    ) * 5

    if delta_goals > 0:
        pressure += 10

    pressure = min(
        round(pressure),
        100
    )

    return {
        "available": True,
        "pressure": pressure,
        "sot": int(delta_sot),
        "shots": int(delta_shots),
        "dangerous": int(delta_dangerous),
        "corners": int(delta_corners),
        "attacks": int(delta_attacks),
        "goals": int(delta_goals)
    }


# ============================================================
# CURRENT PRESSURE MODEL
# ============================================================

def calculate_base_score(
    match
):

    minute = get_minute(
        match
    )

    if minute < 15:
        return None

    if minute > 125:
        return None

    stats = extract_statistics(
        match
    )

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

    total_goals = (
        stats["goals_h"]
        + stats["goals_a"]
    )

    score = 0.0

    # --------------------------------------------------------
    # SHOTS ON TARGET
    # --------------------------------------------------------

    score += min(
        total_sot / 8,
        1
    ) * 30

    # --------------------------------------------------------
    # DANGEROUS ATTACKS
    # --------------------------------------------------------

    score += min(
        total_dangerous / 60,
        1
    ) * 25

    # --------------------------------------------------------
    # SHOTS
    # --------------------------------------------------------

    score += min(
        total_shots / 18,
        1
    ) * 15

    # --------------------------------------------------------
    # CORNERS
    # --------------------------------------------------------

    score += min(
        total_corners / 9,
        1
    ) * 10

    # --------------------------------------------------------
    # ATTACKS
    # --------------------------------------------------------

    score += min(
        total_attacks / 130,
        1
    ) * 5

    # --------------------------------------------------------
    # TIME
    # --------------------------------------------------------

    if 15 <= minute < 30:

        if total_sot >= 3:
            score += 6

        if total_dangerous >= 20:
            score += 4

        if total_shots >= 7:
            score += 3

    elif 30 <= minute < 45:

        if total_sot >= 3:
            score += 7

        if total_dangerous >= 25:
            score += 5

        if total_shots >= 8:
            score += 3

    elif 45 <= minute < 60:

        score += 5

        if total_sot >= 4:
            score += 6

        if total_dangerous >= 30:
            score += 5

    else:

        score += 10

        if total_sot >= 4:
            score += 6

        if total_dangerous >= 35:
            score += 5

    # --------------------------------------------------------
    # SCORE STATE
    # --------------------------------------------------------

    if total_goals == 0:
        score += 6

    elif total_goals == 1:
        score += 3

    # --------------------------------------------------------
    # EVENTS
    # --------------------------------------------------------

    events = analyse_events(
        match
    )

    if total_corners >= 5:
        score += 3

    if events[
        "recent_corners"
    ] >= 2:
        score += 4

    if events[
        "recent_red_cards"
    ] > 0:
        score += 5

    missed_penalties = (
        events[
            "missed_penalty_home"
        ]
        + events[
            "missed_penalty_away"
        ]
    )

    if missed_penalties > 0:
        score += 4

    # --------------------------------------------------------
    # POSSESSION
    # --------------------------------------------------------

    possession_difference = abs(
        stats["poss_h"]
        - stats["poss_a"]
    )

    if possession_difference >= 20:
        score += 2

    return min(
        round(score),
        99
    )


# ============================================================
# FINAL PREDICTION
# ============================================================

def calculate_prediction(
    match,
    snapshots
):

    minute = get_minute(
        match
    )

    base_score = calculate_base_score(
        match
    )

    if base_score is None:
        return None

    fid = fixture_id(
        match
    )

    current_snapshot = create_snapshot(
        match
    )

    previous_snapshot, window_age = (
        find_10m_snapshot(
            snapshots,
            fid
        )
    )

    ten_minute = calculate_10m_delta(
        current_snapshot,
        previous_snapshot
    )

    # --------------------------------------------------------
    # TRUE 10M ACCELERATION
    # --------------------------------------------------------

    if ten_minute[
        "available"
    ]:

        acceleration = (
            ten_minute[
                "pressure"
            ]
            - 50
        )

        acceleration_bonus = 0

        if ten_minute[
            "pressure"
        ] >= 80:
            acceleration_bonus = 10

        elif ten_minute[
            "pressure"
        ] >= 65:
            acceleration_bonus = 7

        elif ten_minute[
            "pressure"
        ] >= 50:
            acceleration_bonus = 4

        elif ten_minute[
            "pressure"
        ] <= 20:
            acceleration_bonus = -5

    else:

        acceleration = 0
        acceleration_bonus = 0

    # --------------------------------------------------------
    # FINAL SCORE
    # --------------------------------------------------------

    final_score = (
        base_score
        + acceleration_bonus
    )

    final_score = max(
        0,
        min(
            round(final_score),
            99
        )
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
    # ESTIMATED MODEL PROBABILITY
    # NOT A TRUE STATISTICAL PROBABILITY
    # --------------------------------------------------------

    estimated_probability = min(
        90,
        round(
            30
            + final_score * 0.4
        )
    )

    stats = extract_statistics(
        match
    )

    return {
        "score": final_score,
        "base_score": base_score,
        "estimated_probability":
            estimated_probability,
        "level": level,
        "minute": minute,
        "home_score":
            int(stats["goals_h"]),
        "away_score":
            int(stats["goals_a"]),
        "sot_h":
            int(stats["sot_h"]),
        "sot_a":
            int(stats["sot_a"]),
        "shots_h":
            int(stats["shots_h"]),
        "shots_a":
            int(stats["shots_a"]),
        "danger_h":
            int(stats["danger_h"]),
        "danger_a":
            int(stats["danger_a"]),
        "attacks_h":
            int(stats["attacks_h"]),
        "attacks_a":
            int(stats["attacks_a"]),
        "poss_h":
            int(stats["poss_h"]),
        "poss_a":
            int(stats["poss_a"]),
        "corners_h":
            int(stats["corners_h"]),
        "corners_a":
            int(stats["corners_a"]),
        "ten_minute_available":
            ten_minute[
                "available"
            ],
        "ten_minute_age":
            int(window_age),
        "ten_minute_pressure":
            int(
                ten_minute[
                    "pressure"
                ]
            ),
        "ten_minute_sot":
            int(
                ten_minute[
                    "sot"
                ]
            ),
        "ten_minute_shots":
            int(
                ten_minute[
                    "shots"
                ]
            ),
        "ten_minute_dangerous":
            int(
                ten_minute[
                    "dangerous"
                ]
            ),
        "ten_minute_corners":
            int(
                ten_minute[
                    "corners"
                ]
            ),
        "ten_minute_attacks":
            int(
                ten_minute[
                    "attacks"
                ]
            ),
        "ten_minute_goals":
            int(
                ten_minute[
                    "goals"
                ]
            ),
        "acceleration":
            round(
                acceleration,
                1
            ),
        "acceleration_bonus":
            acceleration_bonus
    }


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    response = requests.post(
        (
            "https://api.telegram.org/"
            f"bot{TELEGRAM_TOKEN}/sendMessage"
        ),
        data={
            "chat_id": CHAT_ID,
            "text": message
        },
        timeout=TELEGRAM_TIMEOUT
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

    if acceleration >= 15:
        trend = (
            "📈 VERY STRONG 10M PRESSURE"
        )

    elif acceleration >= 5:
        trend = (
            "📈 10M PRESSURE RISING"
        )

    elif acceleration <= -10:
        trend = (
            "📉 10M PRESSURE FALLING"
        )

    else:
        trend = (
            "➡️ 10M PRESSURE STABLE"
        )

    history = (
        "AVAILABLE"
        if prediction[
            "ten_minute_available"
        ]
        else "NOT YET AVAILABLE"
    )

    return (
        "🔥 GOAL PREDICTION\n\n"

        f"⏱️ {prediction['minute']}′\n"
        f"{home} "
        f"{prediction['home_score']}-"
        f"{prediction['away_score']} "
        f"{away}\n\n"

        f"🎯 FINAL SCORE: "
        f"{prediction['score']}/100\n"

        f"📊 BASE SCORE: "
        f"{prediction['base_score']}/100\n"

        f"📈 EST. GOAL SIGNAL: "
        f"{prediction['estimated_probability']}%\n"

        f"⚡ LEVEL: "
        f"{prediction['level']}\n\n"

        f"🕐 10′ HISTORY: "
        f"{history}\n"

        f"🔥 10′ PRESSURE: "
        f"{prediction['ten_minute_pressure']}/100\n"

        f"🎯 10′ SOT: "
        f"{prediction['ten_minute_sot']}\n"

        f"⚽ 10′ SHOTS: "
        f"{prediction['ten_minute_shots']}\n"

        f"🔥 10′ DANGEROUS: "
        f"{prediction['ten_minute_dangerous']}\n"

        f"🚩 10′ CORNERS: "
        f"{prediction['ten_minute_corners']}\n\n"

        f"{trend}\n"
        f"⚡ Acceleration: "
        f"{prediction['acceleration']}\n"
        f"➕ Bonus: "
        f"{prediction['acceleration_bonus']}\n\n"

        f"🎯 SOT: "
        f"{prediction['sot_h']}-"
        f"{prediction['sot_a']}\n"

        f"⚽ Shots: "
        f"{prediction['shots_h']}-"
        f"{prediction['shots_a']}\n"

        f"🔥 Dangerous: "
        f"{prediction['danger_h']}-"
        f"{prediction['danger_a']}\n"

        f"🚩 Corners: "
        f"{prediction['corners_h']}-"
        f"{prediction['corners_a']}\n\n"

        "⚠️ Model signal only — "
        "not a guaranteed probability."
    )


# ============================================================
# ALERT CONTROL
# ============================================================

def should_alert(
    prediction,
    previous_state
):

    score = prediction[
        "score"
    ]

    old_score = number(
        previous_state.get(
            "score"
        ),
        0
    )

    old_alert_score = number(
        previous_state.get(
            "last_alert_score"
        ),
        0
    )

    # --------------------------------------------------------
    # Never alert before we have a real 10M window,
    # unless this is an extremely strong current signal.
    # --------------------------------------------------------

    if not prediction[
        "ten_minute_available"
    ]:

        if score < 90:
            return False

        if old_alert_score >= 90:
            return False

        return True

    # --------------------------------------------------------
    # Standard alert
    # --------------------------------------------------------

    if score < MIN_ALERT_SCORE:
        return False

    # First alert
    if old_alert_score < MIN_ALERT_SCORE:
        return True

    # Significant improvement
    if score >= old_alert_score + 8:
        return True

    # Very strong 10-minute pressure
    if (
        prediction[
            "ten_minute_pressure"
        ] >= 80
        and score >= 80
        and old_score < score
    ):
        return True

    return False


# ============================================================
# PROCESS MATCHES
# ============================================================

def process_matches(
    matches
):

    state = load_state()
    snapshots = load_snapshots()

    cleanup_snapshots(
        snapshots
    )

    new_state = {}
    alerts = 0

    for match in matches:

        fid = fixture_id(
            match
        )

        if fid is None:
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
            f"LIVE: "
            f"{home} vs {away}"
        )

        print(
            f"MINUTE: {minute}"
        )

        # ----------------------------------------------------
        # Calculate prediction BEFORE storing current snapshot
        # ----------------------------------------------------

        prediction = calculate_prediction(
            match,
            snapshots
        )

        if prediction is None:

            print(
                "PREDICTION: NOT READY"
            )

            # Still store snapshot.
            store_snapshot(
                snapshots,
                match
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
            "ESTIMATED GOAL "
            f"PROBABILITY: "
            f"{prediction['estimated_probability']}%"
        )

        print(
            f"LEVEL: "
            f"{prediction['level']}"
        )

        print(
            "10' HISTORY AVAILABLE: "
            f"{prediction['ten_minute_available']}"
        )

        print(
            "10' WINDOW AGE: "
            f"{prediction['ten_minute_age']} sec"
        )

        print(
            "10' PRESSURE: "
            f"{prediction['ten_minute_pressure']}/100"
        )

        print(
            "10' SOT: "
            f"{prediction['ten_minute_sot']}"
        )

        print(
            "10' SHOTS: "
            f"{prediction['ten_minute_shots']}"
        )

        print(
            "10' DANGEROUS: "
            f"{prediction['ten_minute_dangerous']}"
        )

        print(
            "10' CORNERS: "
            f"{prediction['ten_minute_corners']}"
        )

        print(
            "ACCELERATION: "
            f"{prediction['acceleration']}"
        )

        print(
            "ACCELERATION BONUS: "
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

        old_state = state.get(
            fid,
            {}
        )

        if should_alert(
            prediction,
            old_state
        ):

            message = build_message(
                match,
                prediction
            )

            try:

                send_telegram(
                    message
                )

                prediction[
                    "last_alert_score"
                ] = prediction[
                    "score"
                ]

                alerts += 1

            except Exception as error:

                print(
                    "TELEGRAM ERROR:",
                    repr(error)
                )

                prediction[
                    "last_alert_score"
                ] = number(
                    old_state.get(
                        "last_alert_score"
                    ),
                    0
                )

        else:

            prediction[
                "last_alert_score"
            ] = number(
                old_state.get(
                    "last_alert_score"
                ),
                0
            )

        # ----------------------------------------------------
        # Save state
        # ----------------------------------------------------

        new_state[fid] = prediction

        # ----------------------------------------------------
        # IMPORTANT:
        # Store current snapshot AFTER calculating delta.
        # ----------------------------------------------------

        store_snapshot(
            snapshots,
            match
        )

    cleanup_snapshots(
        snapshots
    )

    save_snapshots(
        snapshots
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
        timeout=REQUEST_TIMEOUT
    )

    remaining = response.headers.get(
        "X-RateLimit-Remaining"
    )

    reset = response.headers.get(
        "X-RateLimit-Reset"
    )

    print(
        "API RATE LIMIT REMAINING: "
        f"{remaining}"
    )

    if reset:
        print(
            "API RATE LIMIT RESET: "
            f"{reset}"
        )

    # --------------------------------------------------------
    # 429 HANDLING
    # --------------------------------------------------------

    if response.status_code == 429:

        print(
            "API RATE LIMIT HIT - "
            "USING PREVIOUS STATE"
        )

        return []

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

    print(
        f"LIVE MATCHES: "
        f"{len(matches)}"
    )

    if matches:

        process_matches(
            matches
        )

    else:

        print(
            "NO LIVE MATCH DATA "
            "AVAILABLE THIS SCAN"
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
