#!/usr/bin/env python3
"""NBA EDGE: rebuild past days with the full model (only pre-game information) and
store them as history snapshots marked backtest=True, with actual results attached.
Never overwrites a real (live) snapshot. Usage: python scripts/backtest.py [YYYY-MM-DD ...]
(default: every archived slate before today)."""
import datetime as dt
import json
import pathlib
import sys
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import build_slate as S            # noqa: E402
import grade_projections as G      # noqa: E402

DATA = ROOT / 'data'


def main(days):
    today = dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat()
    if not days:
        days = sorted(p.stem.replace('slate_', '') for p in (DATA / 'archive').glob('slate_*.json'))
    logs = G.read(DATA / 'player_stats.json').get('game_logs', [])
    (DATA / 'history').mkdir(parents=True, exist_ok=True)
    for day in days:
        if day >= today:
            continue
        path = DATA / 'history' / f'{day}.json'
        old = G.read(path)
        if old and not old.get('backtest'):
            print(day, 'has a live snapshot; skipping')
            continue
        out = S.build(day, backtest=True, weights={})   # default weights: the learner refits from components
        if not out:
            continue
        snap = {'date': day, 'backtest': True,
                'note': 'Full model rebuilt using only information from before this date (archived slate, earlier box scores; no live injury feed or confirmed lineups).',
                'players': {G.norm(p['name']) + '|' + p['team']: G.component_record(p) for p in out['players'] if p['proj'] > 0 or p.get('out')}}
        if not G.attach_actuals(snap, logs) and 'actual' not in json.dumps(old):
            print(day, 'no box scores available; skipping')
            continue
        if old:   # keep actuals captured earlier if box scores have rolled off
            for k, s in snap['players'].items():
                if 'actual' not in s and k in old.get('players', {}) and 'actual' in old['players'][k]:
                    s['actual'], s['actual_min'] = old['players'][k]['actual'], old['players'][k]['actual_min']
        path.write_text(json.dumps(snap, separators=(',', ':')) + '\n', encoding='utf-8')
        res = G.grade_day(snap, logs)
        print(day, res and {k: res[k] for k in ('players', 'mae', 'naive_mae', 'bias', 'minutes_mae')})


if __name__ == '__main__':
    main(sys.argv[1:])
