import os
import time
import requests

API_KEY = os.environ["API_FOOTBALL_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

HEADERS = {
    "x-apisports-key": API_KEY
}

LEAGUES = {
    39: "Premier League",
    140: "La Liga",
    135: "Serie A",
    78: "Bundesliga",
    61: "Ligue 1",
    2: "Champions League",
    3: "Europa League",
    88: "Eredivisie",
    94: "Primeira Liga",
    197: "Super League Greece"
}

ALERT_THRESHOLD = 70
CHECK_INTERVAL = 300  # 5 minutes


def get_stat(team, name):
    for item in team.get("statistics", []):
        if item.get("type") == name:
            value = item.get("value")

            if isinstance(value, str):
                value = value.replace("%", "")

            try:
                return float(value)
            except:
                return 0

    return 0


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": message
        },
        timeout=20
    )

    print("Telegram:", response.status_code)


def scan():

    print("================================")
    print("LIVE SCAN STARTED")
    print("================================")

    response = requests.get(
        "https://v3.football.api-sports.io/fixtures",
        headers=HEADERS,
        params={"live": "all"},
        timeout=20
    )

    response.raise_for_status()

    matches = response.json().get("response", [])

    print("Live matches:", len(matches))

    selected = []

    for match in matches:

        league_id = match["league"]["id"]

        if league_id not in LEAGUES:
            continue

        status = match["fixture"]["status"]["short"]
        minute = match["fixture"]["status"].get("elapsed") or 0

        if status not in ("1H", "2H"):
            continue

        if minute < 25:
            continue

        selected.append(match)

    print("Selected:", len(selected))

    # Maximum 5 statistics requests per scan
    selected = selected[:5]

    for match in selected:

        fixture_id = match["fixture"]["id"]

        home = match["teams"]["home"]["name"]
        away = match["teams"]["away"]["name"]

        home_goals = match["goals"]["home"] or 0
        away_goals = match["goals"]["away"] or 0

        minute = match["fixture"]["status"].get("elapsed") or 0

        print(
            f"Checking {home} - {away} "
            f"({minute}')"
        )

        stats_response = requests.get(
            "https://v3.football.api-sports.io/fixtures/statistics",
            headers=HEADERS,
            params={"fixture": fixture_id},
            timeout=20
        )

        if stats_response.status_code != 200:
            print("Statistics error")
            continue

        teams = stats_response.json().get("response", [])

        if len(teams) < 2:
            print("No statistics")
            continue

        home_stats = teams[0]
        away_stats = teams[1]

        shots = (
            get_stat(home_stats, "Total Shots")
            +
            get_stat(away_stats, "Total Shots")
        )

        shots_on_target = (
            get_stat(home_stats, "Shots on Goal")
            +
            get_stat(away_stats, "Shots on Goal")
        )

        corners = (
            get_stat(home_stats, "Corner Kicks")
            +
            get_stat(away_stats, "Corner Kicks")
        )

        possession_home = get_stat(
            home_stats,
            "Ball Possession"
        )

        possession_away = get_stat(
            away_stats,
            "Ball Possession"
        )

        score = 0

        score += min(shots * 1.5, 15)

        score += min(
            shots_on_target * 4,
            30
        )

        score += min(
            corners * 1.5,
            10
        )

        possession_gap = abs(
            possession_home -
            possession_away
        )

        score += min(
            possession_gap * 0.5,
            10
        )

        if minute >= 55:
            score += 10

        if minute >= 70:
            score += 10

        if home_goals == away_goals:
            score += 10

        score = min(
            round(score),
            100
        )

        print(
            f"Goal Pressure: {score}/100"
        )

        if score < ALERT_THRESHOLD:
            continue

        if score >= 90:
            level = "EXTREME"
            emoji = "🚨"

        elif score >= 80:
            level = "STRONG"
            emoji = "🔥"

        else:
            level = "WATCH"
            emoji = "⚠️"

        message = (
            f"{emoji} {level} GOAL ALERT\n\n"
            f"⚽ {home} "
            f"{home_goals}-{away_goals} "
            f"{away}\n"
            f"🏆 {LEAGUES[match['league']['id']]}\n"
            f"⏱ {minute}'\n\n"
            f"🎯 Goal Pressure: "
            f"{score}/100\n\n"
            f"📊 Shots: "
            f"{int(shots)}\n"
            f"🎯 On target: "
            f"{int(shots_on_target)}\n"
            f"🚩 Corners: "
            f"{int(corners)}\n"
            f"📈 Possession: "
            f"{int(possession_home)}%-"
            f"{int(possession_away)}%\n\n"
            f"⚠️ Υψηλή επιθετική πίεση.\n"
            f"Δεν αποτελεί εγγύηση γκολ."
        )

        send_telegram(message)


while True:

    try:
        scan()

    except Exception as error:

        print(
            "ERROR:",
            error
        )

    print(
        "Waiting 5 minutes..."
    )

    time.sleep(CHECK_INTERVAL)
