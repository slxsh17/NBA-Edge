NBA EDGE v21.1 — Current slate automatic loader

Replace website files: index.html, service-worker.js
Upload data/slate_2026-10-22.csv at exactly that GitHub path.
Your website detects today's date in America/New_York and looks for data/slate_YYYY-MM-DD.csv.
Once today's file is found, it refreshes the player pool and attempts to apply the historical projection data.
If a published slate file is unavailable, it warns; it does not download FanDuel salaries automatically.
For future dates, upload the new day’s player CSV to data/slate_YYYY-MM-DD.csv.
The loader accepts the standard NBA EDGE CSV and raw FanDuel player-list CSV headers (Id,Position,Nickname,FPPG,Salary,etc.).
You can alternatively use Choose File for a one-off manual import.
Do not publish private account details or API keys.
