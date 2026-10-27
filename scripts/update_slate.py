#!/usr/bin/env python3
"""NBA EDGE: publish today's FanDuel main-slate player pool automatically.

Source: DailyFantasyFuel's free public FanDuel NBA projections page, which lists
each main-slate player's FanDuel salary, position(s), team and injury tag.
Only roster facts are taken (name, team, position, salary, status) — not DFF's
projections. Writes data/slate_YYYY-MM-DD.csv in NBA EDGE's own CSV format; the
website already loads that file automatically. A manual CSV upload on the site
still overrides it (e.g. for early/late/turbo slates).
"""
import csv
import datetime as dt
import html
import io
import pathlib
import re
import sys
import urllib.request
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
ALIASES = {'GS': 'GSW', 'NO': 'NOP', 'NY': 'NYK', 'PHO': 'PHX', 'SA': 'SAS',
           'UTAH': 'UTA', 'WSH': 'WAS', 'BRK': 'BKN'}
STATUS = {'O': 'OUT', 'OUT': 'OUT', 'Q': 'QUESTIONABLE', 'GTD': 'QUESTIONABLE',
          'D': 'DOUBTFUL', 'P': 'ACTIVE', '': 'ACTIVE'}
POSITIONS = {'PG', 'SG', 'SF', 'PF', 'C'}
FIELDS = ['name', 'team', 'position', 'salary', 'minutes', 'fp_per_min', 'ownership',
          'matchup_factor', 'usage_factor', 'foul_lost_min', 'blowout_lost_min', 'ceiling_sd', 'status']
ROW = re.compile(r'<tr[^>]*class="[^"]*projections-listing[^"]*"[^>]*>', re.S)
ATTR = re.compile(r'(data-[a-z_]+)=\s*"([^"]*)"')


def fetch(url):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36 NBA-EDGE-personal',
        'Accept': 'text/html'})
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.read().decode('utf-8', 'replace')


def parse(page, day):
    if not re.search(r"window\.slate\s*=\s*'Main'", page):
        raise ValueError('page is not the Main slate')
    players, seen = [], set()
    for tag in ROW.findall(page):
        a = {k: html.unescape(v).strip() for k, v in ATTR.findall(tag)}
        if a.get('data-start_date') != day:
            continue
        name = a.get('data-name', '')
        team = a.get('data-team', '').upper()
        team = ALIASES.get(team, team)
        pos = [p for p in (a.get('data-pos', ''), a.get('data-pos_alt', '')) if p in POSITIONS]
        try:
            salary = int(float(a.get('data-salary', '0')))
        except ValueError:
            continue
        if not name or not team or not pos or not 3000 <= salary <= 20000:
            continue
        key = (name.lower(), team)
        if key in seen:
            continue
        seen.add(key)
        status = STATUS.get(a.get('data-inj', '').upper(), 'ACTIVE')
        players.append({'name': name, 'team': team, 'position': '/'.join(dict.fromkeys(pos)),
                        'salary': salary, 'minutes': 0, 'fp_per_min': 0, 'ownership': '',
                        'matchup_factor': 1, 'usage_factor': 1, 'foul_lost_min': 0,
                        'blowout_lost_min': 0, 'ceiling_sd': 8, 'status': status})
    teams = {p['team'] for p in players}
    if len(players) < 18 or len(teams) < 2:
        raise ValueError(f'only {len(players)} players / {len(teams)} teams found for {day}')
    return sorted(players, key=lambda p: -p['salary'])


def main():
    day = dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat()
    url = f'https://www.dailyfantasyfuel.com/nba/projections/fanduel/{day}/'
    try:
        players = parse(fetch(url), day)
    except Exception as e:  # keep whatever is already published; never write a partial slate
        # Fail the workflow (red X in GitHub Actions) so a broken source is visible.
        print(f'::error::No slate published for {day}: {e}')
        return 1
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator='\n')
    w.writeheader()
    w.writerows(players)
    DATA.mkdir(parents=True, exist_ok=True)
    out = DATA / f'slate_{day}.csv'
    out.write_text(buf.getvalue(), encoding='utf-8')
    # Keep the repo tidy: only the last 7 daily slates.
    for old in sorted(DATA.glob('slate_*.csv'))[:-7]:
        old.unlink()
    outs = sum(p['status'] == 'OUT' for p in players)
    print(f'Published {out.name}: {len(players)} players, {len({p["team"] for p in players})} teams, {outs} OUT')
    return 0


if __name__ == '__main__':
    sys.exit(main())
