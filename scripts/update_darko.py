#!/usr/bin/env python3
"""NBA EDGE: DARKO per-game box-score projections -> data/darko.json.

DARKO (darko.app, by Kostya Medvedovsky) is a public Bayesian player-projection model built on
every box score, with stat-specific stabilization, aging curves and daily updates. Its Fantasy
Lab page lists each player's projected minutes and per-game PTS/REB/AST/STL/BLK/3PM/TOV. We turn
those into FanDuel points per minute (pts + 1.2 reb + 1.5 ast + 3 stl + 3 blk - tov) and use it
as a second opinion on each player's scoring rate. How much it counts is learned from graded
nights (learn_weights.py 'darko'); it starts at 0 and is only adopted if it beats the current
model on held-out days.

Fetched at most every 6 hours; a failed fetch keeps the previous file.
"""
import datetime as dt
import html
import json
import pathlib
import re
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
OUT = DATA / 'darko.json'
URL = 'https://www.darko.app/projections'
ALIASES = {'GS': 'GSW', 'NO': 'NOP', 'NY': 'NYK', 'PHO': 'PHX', 'SA': 'SAS', 'UTAH': 'UTA', 'WSH': 'WAS', 'BRK': 'BKN'}
FD = {'PTS': 1.0, 'REB': 1.2, 'AST': 1.5, 'STL': 3.0, 'BLK': 3.0, 'TOV': -1.0}


def fetch(url):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36 NBA-EDGE-personal',
        'Accept': 'text/html'})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode('utf-8', 'replace')


def cells(row_html):
    out = []
    for c in re.findall(r'<td[^>]*>(.*?)</td>', row_html, re.S):
        out.append(re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', c))).strip())
    return out


def num(v):
    v = (v or '').replace('%', '').replace('+', '').strip()
    try:
        return float(v)
    except ValueError:
        return None


REC = re.compile(r'\{nba_id:(\d+),player_name:"((?:[^"\\]|\\.)*)",team_name:"([^"]*)",tm_id:(\d+),position:"([^"]*)",date:"([^"]*)",(x_[^{}]*)\}')
KV = re.compile(r'(x_[a-z0-9_]+):(-?[\d.]+(?:e-?\d+)?)')


def parse_data(page):
    """The page embeds DARKO's full projection set (every player, not just the 100 shown):
    expected minutes, team pace and per-100-possession rates. Per-game stat = rate * pace * min / 4800."""
    best = {}
    for m in REC.finditer(page):
        nba_id, name, team_name, _tm, pos, date, rest = m.groups()
        x = {k: float(v) for k, v in KV.findall(rest)}
        if 'x_minutes' not in x or 'x_pace' not in x or 'x_pts_100' not in x:
            continue
        if nba_id in best and best[nba_id]['date'] >= date:
            continue
        poss = x['x_pace'] / 4800.0          # possessions per minute played
        per100 = {'pts': x.get('x_pts_100', 0), 'reb': x.get('x_orb_100', 0) + x.get('x_drb_100', 0),
                  'ast': x.get('x_ast_100', 0), 'stl': x.get('x_stl_100', 0), 'blk': x.get('x_blk_100', 0),
                  'tov': x.get('x_tov_100', 0)}
        fd_100 = per100['pts'] + 1.2 * per100['reb'] + 1.5 * per100['ast'] + 3 * per100['stl'] + 3 * per100['blk'] - per100['tov']
        mins = x['x_minutes']
        best[nba_id] = {'nba_id': nba_id, 'name': html.unescape(name.encode().decode('unicode_escape') if '\\' in name else name),
                        'team': TEAMS.get(team_name, team_name), 'pos': pos, 'date': date, 'min': round(mins, 2), 'pace': round(x['x_pace'], 2),
                        **{k + '_100': round(v, 3) for k, v in per100.items()},
                        'fga_100': x.get('x_fga_100'), 'fta_100': x.get('x_fta_100'), 'fg3a_100': x.get('x_fg3a_100'),
                        'fd_per_min': round(fd_100 * poss, 4), 'fd': round(fd_100 * poss * mins, 2)}
    return sorted(best.values(), key=lambda r: -r['fd'])


TEAMS = {'Atlanta Hawks': 'ATL', 'Boston Celtics': 'BOS', 'Brooklyn Nets': 'BKN', 'Charlotte Hornets': 'CHA',
         'Chicago Bulls': 'CHI', 'Cleveland Cavaliers': 'CLE', 'Dallas Mavericks': 'DAL', 'Denver Nuggets': 'DEN',
         'Detroit Pistons': 'DET', 'Golden State Warriors': 'GSW', 'Houston Rockets': 'HOU', 'Indiana Pacers': 'IND',
         'LA Clippers': 'LAC', 'Los Angeles Clippers': 'LAC', 'Los Angeles Lakers': 'LAL', 'Memphis Grizzlies': 'MEM',
         'Miami Heat': 'MIA', 'Milwaukee Bucks': 'MIL', 'Minnesota Timberwolves': 'MIN', 'New Orleans Pelicans': 'NOP',
         'New York Knicks': 'NYK', 'Oklahoma City Thunder': 'OKC', 'Orlando Magic': 'ORL', 'Philadelphia 76ers': 'PHI',
         'Phoenix Suns': 'PHX', 'Portland Trail Blazers': 'POR', 'Sacramento Kings': 'SAC', 'San Antonio Spurs': 'SAS',
         'Toronto Raptors': 'TOR', 'Utah Jazz': 'UTA', 'Washington Wizards': 'WAS'}


def parse(page):
    """Full embedded data first; the visible 100-row table as a fallback."""
    rows = parse_data(page)
    if rows:
        return ['embedded'], rows
    return parse_table(page)


def parse_table(page):
    """The visible Fantasy Lab table: rank, player (+position tag), team, one column per stat."""
    tables = re.findall(r'<table[^>]*>(.*?)</table>', page, re.S)
    for t in tables:
        head = [re.sub(r'[^A-Z0-9%/+]', '', re.sub(r'<[^>]+>', ' ', h).upper())
                for h in re.findall(r'<th[^>]*>(.*?)</th>', t, re.S)]
        if not {'PTS', 'REB', 'AST'} <= set(head):
            continue
        rows = []
        for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', re.split(r'</thead>', t, 1)[-1], re.S):
            c = cells(tr)
            if len(c) < len(head) - 1:
                continue
            c = c[-len(head):] if len(c) >= len(head) else [''] * (len(head) - len(c)) + c
            row = dict(zip(head, c))
            link = re.search(r'href="/player/(\d+)"[^>]*>([^<]+)</a>', tr)
            if not link:
                continue
            pos = re.search(r'position-tag[^>]*>([^<]+)<', tr)
            team = (row.get('TEAM') or '').split()[-1:] or ['']
            team = ALIASES.get(team[0].upper(), team[0].upper())
            stats = {k: num(row.get(k)) for k in ('MIN', 'PTS', 'REB', 'AST', 'STL', 'BLK', '3PM', 'TOV', 'FP/G')}
            if stats['MIN'] is None or stats['PTS'] is None:
                continue
            fd = sum(w * (stats.get(k) or 0.0) for k, w in FD.items())
            rows.append({'nba_id': link.group(1), 'name': html.unescape(link.group(2)).strip(), 'team': team,
                         'pos': pos.group(1).strip() if pos else None,
                         'min': stats['MIN'], 'pts': stats['PTS'], 'reb': stats['REB'], 'ast': stats['AST'],
                         'stl': stats['STL'], 'blk': stats['BLK'], 'tpm': stats['3PM'], 'tov': stats['TOV'],
                         'fd': round(fd, 2), 'fd_per_min': round(fd / stats['MIN'], 4) if stats['MIN'] else None})
        if rows:
            return head, rows
    return None, []


def main():
    prev = {}
    try:
        prev = json.loads(OUT.read_text(encoding='utf-8'))
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(prev['updated_at'])
        if age < dt.timedelta(hours=6) and '--force' not in sys.argv:
            print(f'DARKO fetched {int(age.total_seconds() // 60)} min ago; skipping')
            return 0
    except (OSError, ValueError, KeyError):
        pass
    try:
        head, rows = parse(fetch(URL))
    except Exception as e:  # keep the previous file
        print(f'::warning::DARKO fetch failed: {e}')
        return 0
    if len(rows) < 100:
        print(f'::warning::DARKO page parsed only {len(rows)} players (columns {head}); keeping previous file')
        return 0
    doc = {'schema': 1, 'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
           'source': 'DARKO DPM Fantasy Lab (darko.app/projections): projected minutes and per-game box score',
           'columns': head, 'players': rows}
    OUT.write_text(json.dumps(doc, separators=(',', ':')) + '\n', encoding='utf-8')
    print(f'DARKO: {len(rows)} players; top {rows[0]["name"]} {rows[0]["fd"]} FD/g in {rows[0]["min"]} min')
    return 0


if __name__ == '__main__':
    sys.exit(main())
