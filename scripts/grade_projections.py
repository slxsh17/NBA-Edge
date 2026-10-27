#!/usr/bin/env python3
"""NBA EDGE: nightly accuracy report -> data/accuracy.json.

1. Snapshot: on every pipeline run, copy today's projections into
   data/history/YYYY-MM-DD.json, but only for players whose game has NOT started.
   So each player's stored number is the last projection before his tip-off.
2. Grade: for past days, compare snapshots to actual FanDuel points from box
   scores (data/player_stats.json). Players who didn't play are counted separately.
   The model is compared with a simple baseline: the player's plain average FP.
"""
import datetime as dt
import json
import pathlib
import re
import statistics
import unicodedata
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
HIST = DATA / 'history'
ALIASES = {'GS': 'GSW', 'NO': 'NOP', 'NY': 'NYK', 'PHO': 'PHX', 'SA': 'SAS', 'UTAH': 'UTA', 'WSH': 'WAS', 'BRK': 'BKN'}


def canon(t):
    t = str(t or '').strip().upper()
    return ALIASES.get(t, t)


def norm(s):
    return re.sub(r'[^a-z0-9]', '', unicodedata.normalize('NFKD', str(s or '')).lower())


def read(path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {} if default is None else default


def snapshot(now):
    today = read(DATA / 'today.json')
    day = today.get('date')
    if not day or not today.get('players'):
        return
    tip = {}
    for g in today.get('games', []):
        if g.get('time'):
            t = dt.datetime.fromisoformat(g['time'].replace('Z', '+00:00'))
            tip[g['home']] = tip[g['away']] = t
    HIST.mkdir(parents=True, exist_ok=True)
    path = HIST / f'{day}.json'
    snap = read(path, {'date': day, 'players': {}})
    changed = False
    for p in today['players']:
        start = tip.get(p['team'])
        if start is None or now >= start:   # game started (or unknown time): freeze
            continue
        key = norm(p['name']) + '|' + p['team']
        rec = {'name': p['name'], 'team': p['team'], 'salary': p['salary'], 'proj': p['proj'],
               'naive': p.get('naive', 0), 'minutes': p['minutes'], 'out': bool(p.get('out')),
               'own': p.get('own')}
        if snap['players'].get(key) != rec:
            snap['players'][key] = rec
            changed = True
    if changed:
        path.write_text(json.dumps(snap, separators=(',', ':')) + '\n', encoding='utf-8')
    for old in sorted(HIST.glob('*.json'))[:-45]:
        old.unlink()


def grade_day(snap, logs):
    day = snap['date'].replace('-', '')
    actual = {}
    for r in logs:
        if str(r.get('date')) == day:
            actual[norm(r.get('name')) + '|' + canon(r.get('team'))] = (float(r.get('minutes') or 0), float(r.get('fantasy_points') or 0))
    if not actual:
        return None
    rows, missed = [], []
    for key, s in snap['players'].items():
        if s['out'] or s['proj'] <= 0:
            continue
        a = actual.get(key)
        if a is None or a[0] <= 0:
            if s['proj'] >= 15:
                missed.append({'name': s['name'], 'team': s['team'], 'proj': s['proj']})
            continue
        rows.append({'name': s['name'], 'team': s['team'], 'salary': s['salary'], 'proj': s['proj'],
                     'naive': s['naive'], 'actual': a[1], 'minutes': a[0], 'proj_min': s['minutes']})
    if len(rows) < 10:
        return None
    err = [r['actual'] - r['proj'] for r in rows]
    nerr = [r['actual'] - r['naive'] for r in rows if r['naive']]
    corr = statistics.correlation([r['proj'] for r in rows], [r['actual'] for r in rows]) if len(rows) > 2 else None
    worst = sorted(rows, key=lambda r: -abs(r['actual'] - r['proj']))[:6]
    return {'date': snap['date'], 'backtest': bool(snap.get('backtest')), 'players': len(rows),
            'mae': round(statistics.mean(abs(e) for e in err), 2),
            'naive_mae': round(statistics.mean(abs(e) for e in nerr), 2) if nerr else None,
            'bias': round(statistics.mean(err), 2),
            'minutes_mae': round(statistics.mean(abs(r['minutes'] - r['proj_min']) for r in rows), 2),
            'correlation': round(corr, 3) if corr is not None else None,
            'did_not_play': missed[:10], 'did_not_play_count': len(missed),
            'biggest_misses': [{'name': r['name'], 'team': r['team'], 'proj': r['proj'], 'actual': r['actual']} for r in worst]}


def main():
    now = dt.datetime.now(dt.timezone.utc)
    snapshot(now)
    today = now.astimezone(ZoneInfo('America/New_York')).date().isoformat()
    logs = read(DATA / 'player_stats.json').get('game_logs', [])
    report = read(DATA / 'accuracy.json', {'schema': 1, 'days': []})
    graded = {d['date']: d for d in report.get('days', [])}
    for path in sorted(HIST.glob('*.json')) if HIST.exists() else []:
        day = path.stem
        if day >= today:
            continue
        res = grade_day(read(path), logs)
        if res:
            graded[day] = res      # regrade while box scores are still in the 7-day window
    days = sorted(graded.values(), key=lambda d: d['date'])[-30:]
    last7 = days[-7:]
    summary = None
    if last7:
        n = sum(d['players'] for d in last7)
        w = lambda k: round(sum(d[k] * d['players'] for d in last7 if d.get(k) is not None) / max(1, sum(d['players'] for d in last7 if d.get(k) is not None)), 2)
        summary = {'days': len(last7), 'players': n, 'mae': w('mae'), 'naive_mae': w('naive_mae'), 'bias': w('bias'),
                   'minutes_mae': w('minutes_mae')}
    out = {'schema': 1, 'updated_at': now.isoformat(timespec='seconds'), 'summary_7d': summary, 'days': days,
           'method': 'Last projection before each tip-off vs actual FanDuel points. Baseline = player\'s plain average FP. DNPs excluded from error, listed separately.'}
    old = read(DATA / 'accuracy.json')
    if {k: v for k, v in old.items() if k != 'updated_at'} != {k: v for k, v in out.items() if k != 'updated_at'}:
        (DATA / 'accuracy.json').write_text(json.dumps(out, separators=(',', ':')) + '\n', encoding='utf-8')
        print('accuracy.json updated:', summary)
    else:
        print('accuracy unchanged')


if __name__ == '__main__':
    main()
