#!/usr/bin/env python3
"""NBA EDGE: actual FanDuel ownership after each slate -> data/ownership/YYYY-MM-DD.json.

Source: LineStar's public (logged-out) FanDuel NBA data feed. For each finished day it
lists real FanDuel contests (entries, fee, type) and each player's % owned, plus
LineStar's own projected ownership. Logged-out access names only ~23 players per day;
we use exactly what is shown (no attempt to identify the hidden ones). That is enough
for the ownership model to grade and tune itself night after night.
"""
import datetime as dt
import json
import pathlib
import re
import sys
import time
import unicodedata
import urllib.request
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / 'data' / 'ownership'
URL = 'https://www.linestarapp.com/DesktopModules/DailyFantasyApi/API/Fantasy/GetSalariesV5?sport=2&site=2&periodId={pid}'
ALIASES = {'GS': 'GSW', 'NO': 'NOP', 'NY': 'NYK', 'PHO': 'PHX', 'SA': 'SAS', 'UTAH': 'UTA', 'WSH': 'WAS', 'BRK': 'BKN'}
SEASON_START = '2026-10-20'


def canon(t):
    t = str(t or '').upper()
    return ALIASES.get(t, t)


def fetch(pid):
    req = urllib.request.Request(URL.format(pid=pid), headers={'User-Agent': 'Mozilla/5.0 NBA-EDGE-personal', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode('utf-8'))


def period_date(name):
    return dt.datetime.strptime(name, '%b %d, %Y').date().isoformat()


def parse(d):
    own = d.get('Ownership') or {}
    sc = json.loads(d.get('SalaryContainerJson') or '{}')
    named = {s['Id']: s for s in sc.get('Salaries', [])}
    slates = []
    for s in own.get('Slates', []):
        req = json.loads(s.get('LineupRequirementsJson') or '[]')
        single = any(r.get('PointMultiplier', 1) != 1 for r in req)
        contests = [c for c in own.get('ContestResults', []) if c['Contest'].get('GroupId') == s['DfsSlateId']]
        if not contests:
            continue
        slate_ids = {str(c['Contest'].get('SlateId')) for c in contests}
        proj = {}
        for sid in slate_ids:
            for r in (own.get('Projected') or {}).get(sid, []):
                proj[r['SalaryId']] = r['Owned']
        # Entry-weighted average across tournaments (fall back to all contests).
        gpp = [c for c in contests if c['Contest'].get('ContestType') == 4] or contests
        tot, wsum = {}, {}
        for c in gpp:
            w = max(1, c['Contest'].get('EntryCount') or 1)
            for r in c.get('OwnershipData', []):
                tot[r['SalaryId']] = tot.get(r['SalaryId'], 0) + w * r['Owned']
                wsum[r['SalaryId']] = wsum.get(r['SalaryId'], 0) + w
        players = []
        for sal_id, s_ in named.items():
            if sal_id not in tot and sal_id not in proj:
                continue
            players.append({'name': unicodedata.normalize('NFC', s_['Name']), 'team': canon(s_.get('PTEAM')), 'salary': s_.get('SAL'),
                            'position': s_.get('POS'),
                            'actual_own': round(tot[sal_id] / wsum[sal_id], 2) if sal_id in tot else None,
                            'linestar_proj_own': proj.get(sal_id)})
        slates.append({'name': s.get('SlateName'), 'type': 'single' if single else 'classic', 'games': s.get('Games'),
                       'salary_cap': s.get('SalaryCap'), 'start_utc': s.get('SlateDateUtc'),
                       'contests': [{'name': c['Contest'].get('ContestName'), 'entries': c['Contest'].get('EntryCount'),
                                     'fee': c['Contest'].get('EntryFee'), 'type': c['Contest'].get('ContestType'),
                                     # every other small field LineStar gives (scores, payout lines) for strategy learning
                                     'extra': {k: v for k, v in c['Contest'].items() if isinstance(v, (int, float)) or (isinstance(v, str) and len(v) < 60)}}
                                    for c in contests],
                       'players': players})
    return slates


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    today = dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat()
    try:
        cur = fetch(-1)
    except Exception as e:
        print(f'::warning::ownership feed unavailable: {e}')
        return 0
    periods = [(period_date(p['Name']), p['Id']) for p in cur.get('Periods', []) if p.get('Name')]
    wanted = [(day, pid) for day, pid in periods if SEASON_START <= day < today and not (OUT / f'{day}.json').exists()]
    if '--date' in sys.argv:
        days = set(sys.argv[sys.argv.index('--date') + 1:])
        wanted = [(day, pid) for day, pid in periods if day in days]
    for day, pid in sorted(wanted)[-10:]:
        try:
            slates = parse(fetch(pid))
        except Exception as e:
            print(f'::warning::{day}: {e}')
            continue
        if not any(p['actual_own'] is not None for s in slates for p in s['players']):
            print(day, 'no actual ownership yet')
            continue
        doc = {'schema': 1, 'date': day, 'source': 'LineStar public FanDuel NBA feed (logged-out view: named players only)',
               'fetched_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'), 'slates': slates}
        (OUT / f'{day}.json').write_text(json.dumps(doc, indent=0) + '\n', encoding='utf-8')
        print(day, ', '.join(f"{s['name']}: {sum(p['actual_own'] is not None for p in s['players'])} players" for s in slates))
        time.sleep(2)
    return 0


if __name__ == '__main__':
    sys.exit(main())
