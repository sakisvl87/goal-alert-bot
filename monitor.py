import os
import time
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import requests
API_KEY = os.environ["FIVE_DOLLAR_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
PORT = int(os.environ.get("PORT", "10000"))
CHECK_INTERVAL = 600
API_URL = "https://api.5dollarfootballapi.com/v1/fixtures"
STATE_FILE = Path("prediction_state.json")
MIN_ALERT_SCORE = 70
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"Goal Alert Live - OK\n"
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
        body = b"Goal Alert Live - OK\n"
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
def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(
            STATE_FILE.read_text()
        )
    except Exception:
        return {}
def save_state(state):
    STATE_FILE.write_text(
        json.dumps(
            state,
            indent=2
        )
    )
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
def number(value, default=0):
    try:
        return float(value)
    except (
        TypeError,
        ValueError
    ):
        return float(default)
def pair(stats, key):
    value = stats.get(
        key,
        {}
    )
    if not isinstance(
        value,
        dict
    ):
        return 0, 0
    return (
        number(
            value.get("home")
        ),
        number(
            value.get("away")
        )
    )
def get_minute(match):
    kickoff = match.get(
        "kickoff_utc"
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
                datetime.now(
                    timezone.utc
                )
                - dt
            ).total_seconds() / 60
            if 0 <= elapsed <= 130:
                return int(elapsed)
        except (
            TypeError,
            ValueError
        ):
            pass
    events = match.get(
        "events",
        []
    ) or []
    minutes = []
    for event in events:
        minute = event.get(
            "minute"
        )
        if minute is not None:
            minutes.append(
                number(minute)
            )
    if minutes:
        return int(
            max(minutes)
        )
    return 0
def prediction_score(match):
    stats = match.get(
        "statistics"
    ) or {}
    if not stats:
        return None
    minute = get_minute(
        match
    )
    if minute < 55:
        return None
    if minute > 100:
        return None
    sot_h, sot_a = pair(
        stats,
        "shots_on_target"
    )
    off_h, off_a = pair(
        stats,
        "shots_off_target"
    )
    attacks_h, attacks_a = pair(
        stats,
        "attacks"
    )
    danger_h, danger_a = pair(
        stats,
        "dangerous_attacks"
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
    total_sot = (
        sot_h
        + sot_a
    )
    total_shots = (
        shots_h
        + shots_a
    )
    total_danger = (
        danger_h
        + danger_a
    )
    total_attacks = (
        attacks_h
        + attacks_a
    )
    corners = match.get(
        "corners"
    ) or {}
    corner_h = number(
        corners.get("home")
    )
    corner_a = number(
        corners.get("away")
    )
    total_corners = (
        corner_h
        + corner_a
    )
    cards = match.get(
        "cards"
    ) or {}
    home_cards = cards.get(
        "home",
        {}
    )
    away_cards = cards.get(
        "away",
        {}
    )
    red_h = number(
        home_cards.get("red")
    )
    red_a = number(
        away_cards.get("red")
    )
    goals = match.get(
        "goals"
    ) or {}
    score_h = int(
        number(
            goals.get("home")
        )
    )
    score_a = int(
        number(
            goals.get("away")
        )
    )
    # --------------------------------
    # GOAL PRESSURE MODEL
    # --------------------------------
    score = 0.0
    # Shots on target — 30%
    score += (
        min(
            total_sot / 8.0,
            1.0
        )
        * 30
    )
    # Dangerous attacks — 25%
    score += (
        min(
            total_danger / 60.0,
            1.0
        )
        * 25
    )
    # Total shots — 15%
    score += (
        min(
            total_shots / 16.0,
            1.0
        )
        * 15
    )
    # Corners — 10%
    score += (
        min(
            total_corners / 8.0,
            1.0
        )
        * 10
    )
    # Attacks — 5%
    score += (
        min(
            total_attacks / 130.0,
            1.0
        )
        * 5
    )
    # Match minute
    if minute >= 65:
        score += 10
    elif minute >= 55:
        score += 6
    # Score state
    total_goals = (
        score_h
        + score_a
    )
    if total_goals == 0:
        score += 5
    elif total_goals == 1:
        score += 3
    # Red card bonus
    if (
        red_h
        + red_a
    ) > 0:
        score += 4
    # Dangerous attack imbalance
    danger_max = max(
        danger_h,
        danger_a
    )
    danger_min = min(
        danger_h,
        danger_a
    )
    if (
        danger_max >= 35
        and
        danger_max
        >= danger_min * 1.6
    ):
        score += 4
    # SOT imbalance
    sot_max = max(
        sot_h,
        sot_a
    )
    sot_min = min(
        sot_h,
        sot_a
    )
    if (
        sot_max >= 4
        and
        sot_max
        >= sot_min + 3
    ):
        score += 4
    # Possession only as a small bonus
    if (
        max(
            poss_h,
            poss_a
        ) >= 60
        and
        total_sot >= 5
    ):
        score += 2
    score = min(
        round(score),
        99
    )
    return {
        "score": score,
        "minute": minute,
        "home_score": score_h,
        "away_score": score_a,
        "sot_h": int(sot_h),
        "sot_a": int(sot_a),
        "shots_h": int(shots_h),
        "shots_a": int(shots_a),
        "danger_h": int(danger_h),
        "danger_a": int(danger_a),
        "poss_h": int(poss_h),
        "poss_a": int(poss_a),
        "corners_h": int(corner_h),
        "corners_a": int(corner_a),
        "red_h": int(red_h),
        "red_a": int(red_a)
    }
def signal_label(score):
    if score >= 90:
        return "🚨 EXTREME"
    if score >= 80:
        return "🔴 VERY HIGH"
    return "🟠 HIGH"
def process_matches(matches):
    state = load_state()
    new_state = {}
    alerts = 0
    for match in matches:
        fixture_id = str(
            match.get("id")
        )
        prediction = prediction_score(
            match
        )
        if prediction is None:
            continue
        new_state[fixture_id] = {
            "score":
                prediction["score"],
            "minute":
                prediction["minute"],
            "home_score":
                prediction["home_score"],
            "away_score":
                prediction["away_score"]
        }
        old = state.get(
            fixture_id,
            {}
        )
        old_score = number(
            old.get("score"),
            0
        )
        # Alert only when first reaching 70+
        if (
            prediction["score"]
            < MIN_ALERT_SCORE
        ):
            continue
        if old_score >= MIN_ALERT_SCORE:
            continue
        teams = match.get(
            "teams"
        ) or {}
        home = (
            teams.get("home")
            or {}
        ).get(
            "name",
            "Home"
        )
        away = (
            teams.get("away")
            or {}
        ).get(
            "name",
            "Away"
        )
        league = (
            match.get("league")
            or {}
        ).get(
            "name",
            ""
        )
        p = prediction
        message = (
            "🔥 GOAL PREDICTION\n\n"
            f"{p['minute']}′ — "
            f"{home} "
            f"{p['home_score']} - "
            f"{p['away_score']} "
            f"{away}\n"
            f"🏆 {league}\n\n"
            f"🎯 Goal pressure: "
            f"{p['score']}%\n"
            f"{signal_label(p['score'])}\n\n"
            f"📊 SOT: "
            f"{p['sot_h']}-"
            f"{p['sot_a']}\n"
            f"⚽ Shots: "
            f"{p['shots_h']}-"
            f"{p['shots_a']}\n"
            f"🔥 Dangerous attacks: "
            f"{p['danger_h']}-"
            f"{p['danger_a']}\n"
            f"🚩 Corners: "
            f"{p['corners_h']}-"
            f"{p['corners_a']}\n"
            f"📊 Possession: "
            f"{p['poss_h']}%-"
            f"{p['poss_a']}%\n"
            f"🟥 Red cards: "
            f"{p['red_h']}-"
            f"{p['red_a']}\n\n"
            "⏱️ Signal horizon: "
            "next ~10 minutes"
        )
        print(
            f"GOAL PREDICTION: "
            f"{home} - {away} "
            f"= {p['score']}%"
        )
        send_telegram(
            message
        )
        alerts += 1
    save_state(
        new_state
    )
    print(
        f"PREDICTION STATE UPDATED: "
        f"{len(new_state)} matches, "
        f"{alerts} alerts"
    )
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
def scan():
    print(
        "\n=============================="
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
        f"{MIN_ALERT_SCORE}%"
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
