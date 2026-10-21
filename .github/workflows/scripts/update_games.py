
import json
import datetime
import pathlib
import urllib.request
from zoneinfo import ZoneInfo

now = datetime.datetime.now(
    ZoneInfo("America/New_York")
)
date = now.strftime("%Y%m%d")

url = (
    "https://site.api.espn.com/apis/site/v2/"
    "sports/basketball/nba/scoreboard?dates="
    + date
)

request = urllib.request.Request(
    url, headers={"Accept": "application/json"}
)

with urllib.request.urlopen(request, timeout=25) as response:
    data = json.load(response)

games = []

for event in data.get("events", []):
    competition = event.get("competitions", [{}])[0]
    competitors = competition.get("competitors", [])

    def team_name(side):
        for team in competitors:
            if team.get("homeAway") == side:
                return team.get("team", {}).get(
                    "displayName", "Unknown"
                )
        return "Unknown"

    games.append({
        "away": team_name("away"),
        "home": team_name("home"),
        "time": event.get("date", ""),
        "status": event.get("status", {})
            .get("type", {})
            .get("description", "Scheduled")
    })

output = {
    "date": date,
    "updated_at": now.isoformat(),
    "source": "ESPN unofficial scoreboard",
    "games": games
}

path = pathlib.Path("data/games.json")
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(output, indent=2) + "\n")

print("Updated", len(games), "NBA games")
