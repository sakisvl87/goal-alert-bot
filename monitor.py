import os
import time
import json
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests


API_KEY = os.environ["FIVE_DOLLAR_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

PORT = int(os.environ.get("PORT", "10000"))
CHECK_INTERVAL = 600

API_URL = "https://api.5dollarfootballapi.com/v1/fixtures"

STATE_FILE = Path("goal_state.json")


class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        body = b"Goal Alert Live - OK\n"

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

        self.wfile.write(body)

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

    print("TELEGRAM ALERT: SENT")


def get_live_matches():

    headers = {
        "Authorization": f"Bearer {API_KEY}"
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

    return data.get("data", [])


def process_matches(matches):

    previous = load_state()

    current = {}

    for match in matches:

        fixture_id = str(
            match.get("id")
        )

        teams = match.get(
            "teams",
            {}
        )

        home = teams.get(
            "home",
            {}
        ).get(
            "name",
            "Home"
        )

        away = teams.get(
            "away",
            {}
        ).get(
            "name",
            "Away"
        )

        goals = match.get(
            "goals",
            {}
        )

        home_score = goals.get(
            "home"
        )

        away_score = goals.get(
            "away"
        )

        home_score = (
            0
            if home_score is None
            else home_score
        )

        away_score = (
            0
            if away_score is None
            else away_score
        )

        status = match.get(
            "status",
            ""
        )

        league = match.get(
            "league",
            {}
        ).get(
            "name",
            ""
        )

        current[fixture_id] = {
            "home": home,
            "away": away,
            "home_score": home_score,
            "away_score": away_score
        }

        old = previous.get(
            fixture_id
        )

        # First time we see this match:
        # save score but DO NOT send alert.
        if old is None:

            print(
                f"NEW MATCH: "
                f"{home} - {away} "
                f"{home_score}-{away_score}"
            )

            continue

        old_home = old.get(
            "home_score",
            0
        )

        old_away = old.get(
            "away_score",
            0
        )

        goal_detected = (
            home_score > old_home
            or
            away_score > old_away
        )

        if not goal_detected:
            continue

        # Determine which team scored.
        if home_score > old_home:
            scorer = home
        else:
            scorer = away

        message = (
            "⚽ GOAL!\n\n"
            f"{home} {home_score} - "
            f"{away_score} {away}\n\n"
            f"🏆 {league}\n"
            f"🎯 Scorer team: {scorer}\n"
            f"📡 Live alert"
        )

        print(
            f"GOAL DETECTED: "
            f"{home} {home_score}-"
            f"{away_score} {away}"
        )

        send_telegram(message)

    save_state(current)

    print(
        f"STATE UPDATED: "
        f"{len(current)} live matches"
    )


def scan():

    print(
        "\n=============================="
    )

    print(
        "LIVE SCAN STARTED"
    )

    matches = get_live_matches()

    print(
        f"LIVE MATCHES: {len(matches)}"
    )

    process_matches(matches)

    print(
        "LIVE SCAN FINISHED"
    )


if __name__ == "__main__":

    threading.Thread(
        target=start_server,
        daemon=True
    ).start()

    print(
        "GOAL ALERT BOT STARTED"
    )

    print(
        f"CHECK INTERVAL: "
        f"{CHECK_INTERVAL} seconds"
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
            "Waiting 60 seconds..."
        )

        time.sleep(
            CHECK_INTERVAL
        )
