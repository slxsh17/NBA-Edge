NBA EDGE v23 — one-upload automatic analytics

ONLY VISIBLE INPUT: Today's FanDuel salary/position CSV. Raw FanDuel exports work.

INSTALL: Upload index.html and service-worker.js to repository root.
Upload scripts/build_automatic_projections.py and
.github/workflows/update-auto-projections.yml at those exact paths.
Keep your other GitHub workflows for NBA historical stats, injuries, lineup
news, public recommendations and matchup/usage data. They publish the data
files needed by the automatic webpage coordinator.

INITIALIZE: Run GitHub Actions > NBA EDGE Automatic Projection Baselines once.
A published same-day data/slate_YYYY-MM-DD.csv is necessary for server-side
projection snapshots. If you only import the slate in your browser, the
website uses its existing automatic historical baseline from published NBA
box-score JSON files instead. Manual uploads beyond the FanDuel slate are
not required. To sync the slate between devices, publish the same daily slate
CSV as data/slate_YYYY-MM-DD.csv in GitHub.

MODEL: Projection = sample-shrunk previous/current NBA completed regular-
season minutes x fantasy-points per minute. The browser then applies its
bounded matchup/DvP, verified injury and starter rules; ownership is an
uncalibrated simulated field estimate. Missing historical data are excluded,
not assigned 30 invented minutes. DvP and usage lack complete coverage and
remain neutral where observations are insufficient.

EXTERNAL PROJECTIONS: BlueCollarDFS is NOT free for automated API access.
If your repository already has the approved licensed bridge with publication
rights, the website automatically uses its dated snapshots. This package
does not download or redistribute paywalled projections.

The GitHub schedule is best-effort, not real time. Always verify late news
and the slate lock status. No wagers or contest entries are submitted.
