#!/usr/bin/env python3
"""NBA EDGE · DraftKings (paper mode): grade past DK lineups and projections with actual DK points.

Actual DraftKings points come from this season's ESPN box scores (data/season_logs.json), which
include threes made. A night is graded once every pool player's game is final and has threes data.
Writes data/dk/results.json:
  per night  - each lineup's projected vs actual score; player projection error (ours and, as a
               benchmark, DailyFantasyFuel's public DK projection for the same players)
  summary    - averages over graded nights (builds made after tip-off are excluded)
"""
import datetime as dt
import json
import statistics as S
from zoneinfo import ZoneInfo

from build_slate import norm
from dk_common import DK, dk_points, load, load_logs


def main():
    today = dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat()
    logs = load_logs()
    by_date = {}
    for r in logs:
        by_date.setdefault(str(r.get('date')), {})[norm(r.get('name'))] = r
    path = DK / 'results.json'
    res = load(path, {'schema': 1, 'nights': []}) or {'schema': 1, 'nights': []}
    graded = {n['date'] for n in res['nights']}
    changed = False
    for f in sorted(DK.glob('lineups_*.json')):
        doc = load(f)
        day = doc.get('date') if doc else None
        if not day or day >= today or day in graded:
            continue
        rows = by_date.get(day.replace('-', ''), {})
        if not rows:
            print(f'{day}: no box scores yet')
            continue
        if any(r.get('fg3m') is None for r in rows.values()):
            print(f'{day}: box scores lack threes made yet; waiting')
            continue

        def actual(name):
            r = rows.get(norm(name))
            return round(dk_points(r), 2) if r else 0.0
        errs, dff_errs = [], []
        for p in doc['pool']:
            r = rows.get(norm(p['name']))
            if not r or (r.get('minutes') or 0) <= 0:
                continue
            a = dk_points(r)
            errs.append(a - p['proj'])
            if p.get('dff_proj'):
                dff_errs.append(a - p['dff_proj'])
        lus = []
        for lu in doc['lineups']:
            act = round(sum(actual(p['name']) for p in lu['players']), 2)
            lus.append({'strategy': lu['strategy'], 'proj': lu['proj'], 'actual': act,
                        'players': [{'name': p['name'], 'proj': p['proj'], 'actual': actual(p['name'])} for p in lu['players']]})
        night = {'date': day, 'late_build': bool(doc.get('late_build')), 'players_graded': len(errs),
                 'player_mae': round(S.mean(abs(e) for e in errs), 2) if errs else None,
                 'player_bias': round(S.mean(errs), 2) if errs else None,
                 'dff_mae_same_players': round(S.mean(abs(e) for e in dff_errs), 2) if dff_errs else None,
                 'lineups': lus}
        res['nights'].append(night)
        changed = True
        print(f"{day}: player MAE {night['player_mae']} (DFF {night['dff_mae_same_players']}); "
              + ', '.join(f"{l['strategy']} {l['proj']}->{l['actual']}" for l in lus))
    if not changed:
        print('No new DK nights to grade')
        return
    res['nights'].sort(key=lambda n: n['date'])
    ok = [n for n in res['nights'] if not n['late_build']]

    def avg(xs):
        xs = [x for x in xs if x is not None]
        return round(S.mean(xs), 2) if xs else None
    cash = [l for n in ok for l in n['lineups'] if l['strategy'] == 'cash']
    gpp_best = [max((l['actual'] for l in n['lineups'] if l['strategy'] == 'tournament'), default=None) for n in ok]
    res['summary'] = {'nights': len(ok), 'player_mae': avg(n['player_mae'] for n in ok),
                      'player_bias': avg(n['player_bias'] for n in ok),
                      'dff_mae_same_players': avg(n['dff_mae_same_players'] for n in ok),
                      'cash_avg_proj': avg(l['proj'] for l in cash), 'cash_avg_actual': avg(l['actual'] for l in cash),
                      'tournament_best_avg_actual': avg(gpp_best),
                      'note': 'Paper results only. Judge after ~3 weeks; compare cash scores with real DK double-up lines.'}
    res['updated_at'] = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    path.write_text(json.dumps(res, indent=1) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
