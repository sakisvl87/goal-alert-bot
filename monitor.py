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
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_HEAD(self):
        body = b"Goal Alert Live - OK\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
    def log_message(self, *args):
        pass
def start_server():
    server = ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler
    )
    print(f"Health server listening on port {PORT}")
    server.serve_forever()
def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}
def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2)
    )
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
    print("TELEGRAM PREDICTION: SENT")
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
    value = get_value(stats, *names)
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
def get_team_name(match, side):
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
    return (
        int(number(
            get_value(
                goals,
                "home",
                "Home"
            )
        )),
        int(number(
            get_value(
                goals,
                "away",
                "Away"
            )
        ))
    )
def get_minute(match):
    kickoff = match.get("kickoff_utc")
    if kickoff:
        try:
            dt = datetime.fromisoformat(
                kickoff.replace(
                    "Z",
                    "+00:00"
                )
            )
            elapsed = (
                datetime.now(timezone.utc) - dt
            ).total_seconds() / 60
            if 0 <= elapsed <= 130:
                return int(elapsed)
        except Exception:
            pass
    events = match.get("events") or []
    minutes = []
    for event in events:
        minute = (
            event.get("minute")
            if isinstance(event, dict)
            else None
        )
        if minute is not None:
            minutes.append(number(minute))
    if minutes:
        return int(max(minutes))
    return 0
def diagnostic_dump(match):
    home = get_team_name(
        match,
        "home"
    )
    away = get_team_name(
        match,
        "away"
    )
    home_score, away_score = get_score(
        match
    )
    minute = get_minute(
        match
    )
    stats = match.get("statistics") or {}
    print("")
    print("======================================")
    print("LIVE MATCH DIAGNOSTIC")
    print("======================================")
    print(
        f"MATCH: {home} {home_score}-{away_score} {away}"
    )
    print(
        f"MINUTE: {minute}"
    )
    print(
        f"STATISTICS TYPE: {type(stats).__name__}"
    )
    if isinstance(stats, dict):
        print(
            "STATISTICS KEYS:"
        )
        print(
            list(stats.keys())
        )
        for key, value in stats.items():
            print(
                f"STAT [{key}] = {value}"
            )
    elif isinstance(stats, list):
        print(
            f"STATISTICS LIST LENGTH: {len(stats)}"
        )
        for item in stats:
            print(
                f"STAT ITEM: {item}"
            )
    else:
        print(
            f"STATISTICS RAW: {stats}"
        )
    print(
        "EVENTS COUNT:",
        len(match.get("events") or [])
    )
    print(
        "======================================"
    )
def prediction_score(match):
    stats = match.get("statistics") or {}
    if not stats:
        return None
    minute = get_minute(match)
    if minute < 55 or minute > 100:
        return None
    # Try the common API field names.
    sot_h, sot_a = pair(
        stats,
        "shots_on_target",
        "shotsOnTarget",
        "Shots on Target"
    )
    off_h, off_a = pair(
        stats,
        "shots_off_target",
        "shotsOffTarget",
        "Shots off Target"
    )
    attacks_h, attacks_a = pair(
        stats,
        "attacks",
        "Attacks"
    )
    danger_h, danger_a = pair(
        stats,
        "dangerous_attacks",
        "dangerousAttacks",
        "Dangerous Attacks"
    )
    poss_h, poss_a = pair(
        stats,
        "possession",
        "Possession"
    )
    corners_h, corners_a = pair(
        stats,
        "corners",
        "Corners"
    )
    shots_h = sot_h + off_h
    shots_a = sot_a + off_a
    total_sot = sot_h + sot_a
    total_shots = shots_h + shots_a
    total_danger = danger_h + danger_a
    total_corners = corners_h + corners_a
    total_attacks = attacks_h + attacks_a
    score_h, score_a = get_score(
        match
    )
    pressure = 0
    pressure += min(
        total_sot / 8,
        1
    ) * 30
    pressure += min(
        total_danger / 60,
        1
    ) * 25
    pressure += min(
        total_shots / 16,
        1
    ) * 15
    pressure += min(
        total_corners / 8,
        1
    ) * 10
    pressure += min(
        total_attacks / 130,
        1
    ) * 5
    if minute >= 65:
        pressure += 10
    elif minute >= 55:
        pressure += 6
    if score_h + score_a == 0:
        pressure += 5
    elif score_h + score_a == 1:
        pressure += 3
    pressure = min(
        round(pressure),
        99
    )
    return {
        "score": pressure,
        "minute": minute,
        "home_score": score_h,
        "away_score": score_a,
        "sot_h": int(sot_h),
        "sot_a": int(sot_a),
        "shots_h": int(shots_h),
        "shots_a": int(shots_a),
        "danger_h": int(danger_h),
        "danger_a": int(danger_a),
        "corners_h": int(corners_h),
        "corners_a": int(corners_a),
        "poss_h": int(poss_h),
        "poss_a": int(poss_a)
    }
def process_matches(matches):
    state = load_state()
    new_state = {}
    alerts = 0
    for match in matches:
        # IMPORTANT:
        # Print the real API statistics before calculating anything.
        diagnostic_dump(match)
        fixture_id = str(
            match.get("id")
        )
        prediction = prediction_score(
            match
        )
        if prediction is None:
            print(
                "PREDICTION: NO USABLE STATS / "
                "MATCH BELOW TIME FILTER"
            )
            continue
        new_state[fixture_id] = {
            "score": prediction["score"],
            "minute": prediction["minute"],
            "home_score": prediction["home_score"],
            "away_score": prediction["away_score"]
        }
        print(
            "CALCULATED PRESSURE:",
            prediction["score"],
            "%"
        )
        old = state.get(
            fixture_id,
            {}
        )
        old_score = number(
            old.get("score"),
            0
        )
        if prediction["score"] < MIN_ALERT_SCORE:
            continue
        if old_score >= MIN_ALERT_SCORE:
            continue
        home = get_team_name(
            match,
            "home"
        )
        away = get_team_name(
            match,
            "away"
        )
        message = (
            "🔥 GOAL PREDICTION\n\n"
            f"{prediction['minute']}′ — "
            f"{home} "
            f"{prediction['home_score']}-"
            f"{prediction['away_score']} "
            f"{away}\n\n"
            f"🎯 Goal pressure: "
            f"{prediction['score']}%\n\n"
            f"📊 SOT: "
            f"{prediction['sot_h']}-"
            f"{prediction['sot_a']}\n"
            f"⚽ Shots: "
            f"{prediction['shots_h']}-"
            f"{prediction['shots_a']}\n"
            f"🔥 Dangerous attacks: "
            f"{prediction['danger_h']}-"
            f"{prediction['danger_a']}\n"
            f"🚩 Corners: "
            f"{prediction['corners_h']}-"
            f"{prediction['corners_a']}\n"
            f"📊 Possession: "
            f"{prediction['poss_h']}%-"
            f"{prediction['poss_a']}%\n\n"
            "⏱️ Next ~10 minutes"
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
    response.raise_for_status()
    data = response.json()
    if data.get("success") != 1:
        raise RuntimeError(
            f"API error: {data}"
        )
    return data.get(
        "data",
        []
    )
def scan():
    print("")
    print("==============================")
    print("GOAL PREDICTION SCAN STARTED")
    matches = get_live_matches()
    print(
        f"LIVE MATCHES: {len(matches)}"
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
