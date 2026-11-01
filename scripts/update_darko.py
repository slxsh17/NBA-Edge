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


def parse(page):
    """The Fantasy Lab table: rank, player (+position tag), team, then one column per stat."""
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
