NBA EDGE v21 — Field Ownership Research

Upload these exact files to the existing GitHub repository main branch:
  index.html
  service-worker.js
  data/ownership_history.json (create the data/ folder if needed)

Do NOT upload the original raw contest standings to a public GitHub repo: they contain entrant names and lineup data. This package only includes aggregated anonymous player ownership data.

HOW TO USE
1. Open the site and import the current slate CSV. Apply historical player statistics.
2. Inspect the Field Ownership Lab; historical October 21 contest actual data is separate from forecasts.
3. Optional: download the external ownership CSV template and fill it with legitimately accessible, SAME-SLATE ownership projections from websites or tools, with a source for each player.
4. Press Estimate field ownership. Model performs diversified field simulations, blends external numeric forecasts where supplied, and normalizes eligible-player ownership to about 900% (9 players/entry).
5. Check the slate date, correct player names, availability, and source freshness. This is an exploratory research estimate, not validated ownership.

The first contest is only one 245-entry sample. Do not evaluate ownership accuracy by using its own outcome as pregame data. Upload additional aggregated contests and use chronological out-of-sample tests before trusting the model.
