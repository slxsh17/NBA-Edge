NBA EDGE v22 - Starter & Minutes News + Optional External Projection Blend

WHAT TO UPLOAD TO GITHUB (keep the folders/paths intact):
  index.html                                  replace repo root file
  service-worker.js                          replace repo root file
  scripts/update_lineup_news.py              create or replace
  .github/workflows/update-lineup-news.yml   create or replace

Do NOT overwrite your existing injury, historical statistics, DvP/usage,
consensus, or other updater workflows.

ACTIVATE:
  1. Commit files on the main branch.
  2. GitHub > Actions > NBA EDGE Starters & Minutes News > Run workflow > main.
  3. On a successful run, check data/lineup_news.json in GitHub.
  4. Open https://slxsh17.github.io/NBA-Edge/?v=22 (refresh browser cache).
  5. Find "Starting Lineups & Minutes News" near the top; check timestamps,
     confirmed versus projected teams, and the linked news sources.

UPDATES:
  GitHub scheduled workflow attempts to collect public lineups/headlines every
  30 minutes. While open, the page checks the snapshot every 3 minutes.
  These are not instantaneous updates and GitHub schedules can be delayed.

PROJECTION RULES:
  CONFIRMED lineups only: if all 5 names uniquely match imported teammates,
  at most +2 minutes for a starter with 3+ observed games and a historical
  baseline of 8-27.9 minutes. Historical regular starters stay unchanged.
  Projected lineups and news headlines do NOT change projected minutes.
  Stale or missing feeds cause no role adjustments. No athlete is marked OUT
  from starters alone; the separate injury feed handles injury statuses.

X/TWITTER:
  This free edition does NOT have an authenticated X/Twitter API and does NOT
  monitor every public post. The dashboard links to direct X search for
  manual verification. Automated news uses public RSS headlines only.

BLUECOLLARDFS / OTHER PROJECTIONS:
  The site accepts manually supplied/authorized fantasy-point projection CSVs
  in the new Optional Outside Projections control. File example:
     name,projection
     Tyrese Maxey,47.0
  Match against THIS slate; default weight 25%, adjustable 0-50%.
  No premium BlueCollar content is scraped or accessed automatically.

TESTED:
  JS syntax (all five inline scripts); Python compilation; 4 parser
  unit tests; Chromium DOM smoke test with 68 imported Oct 22 players;
  confirmed and projected statuses; role change only when 5 names match.
  Actual GitHub workflow and public RotoWire/Google News responses remain to
  be verified on the user's GitHub runner.
