import os
import requests

SPORTMONKS_TOKEN = os.environ["SPORTMONKS_API_TOKEN"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

# Sportmonks live scores
url = "https://api.sportmonks.com/v3/football/livescores"

response = requests.get(
    url,
    params={"api_token": SPORTMONKS_TOKEN},
    timeout=20
)

response.raise_for_status()
data = response.json()

matches = data.get("data", [])

print(f"Live matches found: {len(matches)}")

for match in matches:
    print(match)
