NBA EDGE v3 — FREE DATA EDITION

Works on GitHub Pages. Still accepts CSV player pools and calculates simulated 9-player lineups using approximate search.
NEW: Free NBA daily schedule/game status snapshot refreshed by a GitHub Actions workflow every ~30 minutes (schedule jobs can be delayed or paused). Unofficial ESPN endpoint may fail.

UPLOAD / REPLACE THE INDIVIDUAL WEBSITE FILES IN YOUR GITHUB REPOSITORY ROOT:
 index.html, service-worker.js, manifest.webmanifest, icons, scripts/update_games.py, .github/workflows/update-games.yml
Note: uploading nested folders using GitHub mobile web UI can be tricky. The site runs without the workflow; if you cannot upload the folders, the schedule section will show a clear "unavailable" notice. For fully automatic free schedule updates, upload the nested workflow and script as correct paths (best via desktop GitHub or git).

In repository Settings > Actions > General ensure Actions are enabled and workflow has permission to read/write repository contents if required. Under Actions, run "Free NBA schedule refresh" manually once to create data/games.json. On GitHub Pages, source should be main / (root). Scheduled Actions may be delayed.

IMPORTANT: ESPN public endpoint is unofficial; verify terms and availability before relying on it. No FanDuel salary feed, live player projections, injury ingest, or independent ownership modeling is claimed. They require additional data work; use CSV import for now. No API secrets go in client-side code.
