#!/usr/bin/env python3
"""NBA EDGE public-data projection baseline (no premium/scraped forecasts).

Builds transparent, sample-shrunk expected minutes and FanDuel fantasy FP/min
from 2025-26 and 2026-27 completed regular-season box scores, for the CURRENT
published salary slate only. DvP/usage adjustment happens on the website from
`data/matchup_usage.json`, and live injury/starter updates remain separate.
No data -> no fabricated projection.
"""
import csv
import datetime as dt
import json
import pathlib
import re
import statistics
import unicodedata
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'


def norm(value):
    value = unicodedata.normalize('NFKD', str(value or ''))
    return re.sub(r'[^a-z0-9]', '', value.lower())


def load(name):
    file = DATA / name
    return json.loads(file.read_text(encoding='utf-8')) if file.exists() else {}


def positive(value):
    try:
        x = float(value)
        return x if 0 < x < 150 else 0
    except (ValueError, TypeError):
        return 0


def raw_players(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        for row in csv.DictReader(stream):
            l = {k.strip().lower(): v for k,v in row.items() if k}
            name = l.get('nickname') or l.get('name') or l.get('player')
            team = (l.get('team') or '').strip().upper()
            try:
                salary = int(float(l.get('salary') or 0))
            except ValueError:
                salary = 0
            if not name or not team or salary < 2000:
                continue
            yield {'name':name, 'team':team,'salary':salary}


def current_logs(rows):
    # Some existing collectors include preseason. Do not use it for current-season projections.
    return [r for r in rows if re.match(r'^2026(10(?:2[0-9]|3[01])|11|12)|^2027', str(r.get('date',''))) and
            positive(r.get('minutes')) > 0 and r.get('fantasy_points') is not None]


def group_logs(rows):
    result = {}
    for r in rows:
        key = norm(r.get('name'))
        if not key:
            continue
        mins = positive(r.get('minutes'))
        try: fp = float(r.get('fantasy_points'))
        except (ValueError, TypeError): continue
        if not mins or fp < -10 or fp > 140: continue
        holder = result.setdefault(key, {'minutes': [], 'fp': [], 'names': set()})
        holder['minutes'].append(mins)
        holder['fp'].append(fp)
        holder['names'].add(r.get('name',''))
    return result


def stats_group(g):
    if not g or not g['minutes']:
        return None
    n = len(g['minutes'])
    avgmin = statistics.mean(g['minutes'])
    fp_per_min = sum(g['fp']) / sum(g['minutes'])
    return {'games':n,'minutes':avgmin,'fppm':fp_per_min}


def aggregate_prior(old):
    grouped = group_logs(old.get('game_logs', []))
    prior = {k: stats_group(v) for k,v in grouped.items()}
    # Backfill may have only aggregate data; use if game logs are not retained.
    for p in old.get('players', []):
        key = norm(p.get('name'))
        if key not in prior and positive(p.get('avg_minutes')) and positive(p.get('fantasy_points_per_minute')):
            prior[key]={'games':int(p.get('games') or 0), 'minutes':float(p['avg_minutes']),
                        'fppm':float(p['fantasy_points_per_minute'])}
    return prior


def build():
    today = dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat()
    csvpath = DATA / ('slate_' + today + '.csv')
    if not csvpath.exists():
        print('No published slate for', today, '— nothing to publish, preserving previous snapshot.')
        return None
    old = load('last_season_stats.json')
    recent = load('player_stats.json')
    prior = aggregate_prior(old)
    current = {k:stats_group(v) for k,v in group_logs(current_logs(recent.get('game_logs', []))).items()}
    output = []; covered=0
    for p in raw_players(csvpath):
        k=norm(p['name'])
        a,b=prior.get(k),current.get(k)
        # Three historical or one current regular-season games are needed for a supported baseline.
        if a and a['games'] < 3: a=None
        if b and b['games'] < 1: b=None
        entry={**p, 'previous_games':a['games'] if a else 0,
               'current_games':b['games'] if b else 0,
               'expected_minutes':None, 'fp_per_minute':None, 'base_projected_fp':None,
               'coverage':'missing'}
        if a or b:
            # Shrink first few games toward last season; no 30 minute placeholders.
            w = min(.80, b['games']/(b['games']+15)) if (a and b) else (1 if b else 0)
            mins=(a['minutes']*(1-w) if a else 0) + (b['minutes']*w if b else 0)
            rate=(a['fppm']*(1-w) if a else 0) + (b['fppm']*w if b else 0)
            # Clearly flagged conservative sanity limits, not validated minutes models.
            mins=min(40,max(1,mins));rate=min(2.7,max(0.1,rate))
            entry.update(expected_minutes=round(mins,2), fp_per_minute=round(rate,4),
                         base_projected_fp=round(mins*rate,2),
                         coverage='last+current' if a and b else ('last-season' if a else 'current-only'),
                         current_weight=round(w,3))
            covered+=1
        output.append(entry)
    out={'schema':1,'slate_date':today,'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),
         'source':'Completed NBA box scores published via ESPN unofficial NBA data feed; not a third-party proprietary forecast',
         'model':'Sample-shrunk historical minutes x observed FanDuel fantasy points per minute; DvP, usage, starter and injury effects applied separately in browser',
         'eligible_history':covered,'slate_players':len(output),
         'previous_coverage_days':old.get('date_count'),
         'previous_expected_days':old.get('expected_date_count'),
         'players':output}
    return out


def main():
    result=build()
    if result is None:return
    DATA.mkdir(exist_ok=True)
    (DATA/'automatic_projections.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print('Generated', result['slate_date'], 'baseline projections:',result['eligible_history'],'of',result['slate_players'])


if __name__=='__main__':main()
