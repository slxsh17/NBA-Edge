#!/usr/bin/env python3
"""NBA EDGE: grade the optimizer's own lineups against what happened -> data/lineup_results.json.

For every slate recorded by record_lineups.py (main + every single game):
  * actual FanDuel score of each original lineup (cash / tournament / simulated winner),
  * the best lineup possible in hindsight on that slate (how much was left on the table),
  * where the actual score landed in the lineup's own simulated range (10th-90th percentile),
  * which players sank or carried it.
Past slates that were played before recording started are rebuilt from the pre-tip
projection snapshots (data/history) and FanDuel salaries (data/archive), marked "rebuilt".

Learning from it:
  * calibration.sd_scale: how wide outcomes really are vs the simulator's spread. Fit from every
    graded player (actual vs projection, scaled by the spread the sims assume) so that the
    simulated 10-90% range covers about 80% of real outcomes. Used by all simulations.
  * by_strategy: running record of cash vs tournament vs simulated-winner lineups, read by
    learn_strategy.py and shown on the site.
"""
import datetime as dt
import json
import pathlib
import statistics
import sys
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import field_sim as FS          # noqa: E402
import record_lineups as RL     # noqa: E402
from build_slate import canon, norm, single_heur  # noqa: E402

DATA = ROOT / 'data'
OUT = DATA / 'lineup_results.json'


def read(p, default=None):
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {} if default is None else default


def spread(proj, shaky):
    return 0.28 * proj + 3 + (3 if shaky else 0)


def actual_map(day):
    """name|team -> actual FD points, for teams whose game is final (DNP = 0)."""
    snap = read(DATA / 'history' / f'{day}.json').get('players') or {}
    out = {k: s['actual'] for k, s in snap.items() if 'actual' in s}
    teams = {k.split('|')[1] for k in out}
    d = day.replace('-', '')
    for r in read(DATA / 'player_stats.json').get('game_logs', []):
        if str(r.get('date')) == d:
            k = norm(r.get('name')) + '|' + canon(r.get('team'))
            out.setdefault(k, float(r.get('fantasy_points') or 0))
            teams.add(canon(r.get('team')))
    return out, teams


def rebuild(day, corr, tset):
    """Rebuild a past day's original lineups from pre-tip snapshots (only used for days
    before live recording existed)."""
    snap = read(DATA / 'history' / f'{day}.json').get('players') or {}
    if not snap:
        return {}
    slates = {}

    def pool_from(rows, own_from_heur=False):
        pool = []
        for r in rows:
            k = norm(r['name']) + '|' + canon(r['team'])
            s = snap.get(k)
            if not s or s.get('out') or (s.get('proj') or 0) <= 0 or r.get('status') in ('OUT', 'DOUBTFUL'):
                continue
            sd = spread(s['proj'], s.get('shaky'))
            pool.append({'name': r['name'], 'team': canon(r['team']), 'opp': canon(r.get('opp')), 'position': r['position'],
                         'salary': r['salary'], 'proj': s['proj'], 'ceiling': s['proj'] + 1.28 * sd, 'own': s.get('own') or 0.0})
        if own_from_heur:
            for p, o in zip(pool, single_heur([(p['proj'], p['salary']) for p in pool])):
                p['own'] = o
        return pool

    main = read(DATA / 'archive' / f'slate_{day}.json').get('players') or []
    pool = pool_from(main)
    if len(pool) >= 30:
        rate, top = FS.optimal_rate(pool, 'classic', 60000, sims=150, corr=corr, keep_top=10)
        for p in pool:
            p['lev'] = rate.get(id(p), 0.0) - p['own']
        lus = RL.lineups_for(pool, 'classic', top, tset, corr)
        if lus:
            slates['main'] = {'label': 'Main slate', 'format': 'classic', 'rebuilt': True, 'lineups': lus}
    for sg in read(DATA / 'archive' / f'single_{day}.json').get('slates', []):
        pool = pool_from([{**r, 'team': r['team']} for r in sg['players']], own_from_heur=True)
        if len(pool) < 8:
            continue
        rate, top = FS.optimal_rate(pool, 'single', 60000, sims=150, corr=corr, keep_top=10)
        for p in pool:
            p['lev'] = rate.get(id(p), 0.0) - p['own']
        lus = RL.lineups_for(pool, 'single', top, tset, corr)
        if lus:
            slates['single:' + '@'.join(sorted((canon(sg['away']), canon(sg['home']))))] = {
                'label': sg['label'], 'format': 'single', 'rebuilt': True, 'lineups': lus}
    return slates


def hindsight(day, key, act):
    """Best possible lineup on the slate with actual scores."""
    if key == 'main':
        rows, fmt = read(DATA / 'archive' / f'slate_{day}.json').get('players') or [], 'classic'
    else:
        pair = key.split(':', 1)[1]
        sg = next((x for x in read(DATA / 'archive' / f'single_{day}.json').get('slates', [])
                   if '@'.join(sorted((canon(x['away']), canon(x['home'])))) == pair), None)
        rows, fmt = (sg or {}).get('players') or [], 'single'
    pool = []
    for r in rows:
        k = norm(r['name']) + '|' + canon(r['team'])
        if k in act and r.get('salary'):
            pool.append({'name': r['name'], 'team': canon(r['team']), 'position': r['position'], 'salary': r['salary'], 'proj': act[k]})
    pool = [p for p in pool if p['proj'] > 0]
    if len(pool) < FS.FORMATS[fmt]['size']:
        return None
    lu = FS.Solver(pool, fmt, 60000).best([p['proj'] for p in pool])
    if not lu:
        return None
    mult = FS.FORMATS[fmt]['mult']
    return {'score': round(sum(pool[i]['proj'] * mult.get(s, 1.0) for i, s in lu), 1),
            'players': [{'name': pool[i]['name'], 'team': pool[i]['team'], 'slot': s} for i, s in lu]}


def grade_lineup(lu, act):
    rows = []
    for x in lu['players']:
        k = norm(x['name']) + '|' + canon(x['team'])
        if k not in act:
            return None
        rows.append({**x, 'actual': act[k]})
    score = round(sum(r['actual'] * r.get('mult', 1.0) for r in rows), 1)
    s = lu.get('sim') or {}
    band = None
    if s:
        band = 'below 10th pct' if score < s['p10'] else 'above 90th pct' if score > s['p90'] else 'within range'
    diffs = sorted(rows, key=lambda r: (r['actual'] - r['proj']) * r.get('mult', 1.0))
    return {'strategy': lu['strategy'], 'proj': lu['proj'], 'actual': score, 'sim': s, 'band': band,
            'busts': [{'name': r['name'], 'proj': r['proj'], 'actual': r['actual']} for r in diffs[:2]],
            'boosts': [{'name': r['name'], 'proj': r['proj'], 'actual': r['actual']} for r in diffs[-2:][::-1]],
            'players': [{'name': r['name'], 'team': r['team'], 'slot': r['slot'], 'proj': r['proj'], 'actual': r['actual']} for r in rows]}


def calibration(days):
    """sd_scale so the simulated 10-90% band covers ~80% of real player outcomes."""
    zs = []
    for day in days:
        for s in (read(DATA / 'history' / f'{day}.json').get('players') or {}).values():
            if 'actual' in s and not s.get('out') and (s.get('proj') or 0) >= 8 and (s.get('actual_min') or 0) > 0:
                zs.append(abs(s['actual'] - s['proj']) / spread(s['proj'], s.get('shaky')))
    if len(zs) < 200:
        return None, len(zs)
    zs.sort()
    # |z| at the 80th percentile should equal 1.2816 for a correctly-sized normal spread
    scale = zs[int(0.8 * (len(zs) - 1))] / 1.2816
    return round(min(1.6, max(0.7, scale)), 3), len(zs)


def main():
    today = dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat()
    prev = read(OUT)
    corr = {**(read(DATA / 'model_params.json').get('corr') or {}), **(prev.get('calibration') or {})}
    tset = (read(DATA / 'strategy.json').get('tournament') or {}).get('best') or {'ceilW': 0.45, 'levW': 0.15, 'ownFade': 0.03}
    graded = {(r['date'], r['slate']): r for r in prev.get('slates', [])}
    hist_days = sorted(p.stem for p in (DATA / 'history').glob('*.json'))
    (DATA / 'lineups').mkdir(exist_ok=True)
    for day in hist_days:
        if day >= today:
            continue
        path = DATA / 'lineups' / f'{day}.json'
        rec = read(path, {'date': day, 'slates': {}})
        missing_main = 'main' not in rec['slates']
        if missing_main and not any((day, k) in graded for k in ('main',)):
            built = rebuild(day, corr, tset)
            for k, v in built.items():
                rec['slates'].setdefault(k, v)
            if built:
                path.write_text(json.dumps(rec, separators=(',', ':')) + '\n', encoding='utf-8')
        act, _teams = actual_map(day)
        for key, sl in rec['slates'].items():
            if (day, key) in graded and graded[(day, key)].get('final'):
                continue
            res = [g for g in (grade_lineup(lu, act) for lu in sl['lineups']) if g]
            if len(res) < len(sl['lineups']):
                continue                       # some games not final yet
            best = hindsight(day, key, act)
            graded[(day, key)] = {'date': day, 'slate': key, 'label': sl['label'], 'format': sl['format'], 'rebuilt': bool(sl.get('rebuilt')),
                                  'final': True, 'hindsight_best': best, 'lineups': res}
            print('graded', day, key, [(g['strategy'], g['proj'], g['actual']) for g in res], 'best', best and best['score'])
    rows = sorted(graded.values(), key=lambda r: (r['date'], r['slate']))
    by = {}
    for r in rows:
        for g in r['lineups']:
            b = by.setdefault((r['format'], g['strategy']), [])
            b.append({'actual': g['actual'], 'proj': g['proj'], 'pct_best': (g['actual'] / r['hindsight_best']['score'] * 100) if r.get('hindsight_best') else None,
                      'band': g['band'], 'p50': (g.get('sim') or {}).get('p50')})
    summary = []
    for (fmt, strat), xs in sorted(by.items()):
        pb = [x['pct_best'] for x in xs if x['pct_best'] is not None]
        summary.append({'format': fmt, 'strategy': strat, 'slates': len(xs), 'avg_actual': round(statistics.mean(x['actual'] for x in xs), 1),
                        'avg_proj': round(statistics.mean(x['proj'] for x in xs), 1),
                        'avg_pct_of_best': round(statistics.mean(pb), 1) if pb else None,
                        'within_sim_range': round(100 * sum(x['band'] == 'within range' for x in xs) / len(xs)),
                        'below_range': round(100 * sum(x['band'] == 'below 10th pct' for x in xs) / len(xs)),
                        'above_range': round(100 * sum(x['band'] == 'above 90th pct' for x in xs) / len(xs))})
    scale, n = calibration([d for d in hist_days if d < today][-30:])
    calib = dict(prev.get('calibration') or {})
    if scale:
        calib['sd_scale'] = scale
    # Optimizer's curse: players the optimizer picks were the ones whose projections ran
    # hottest, so they land below projection more than the average player. Measured per
    # player in our own lineups; it re-centers each lineup's simulated range (it doesn't change
    # which lineup is best, only how honest the expected score is).
    for fmt in ('classic', 'single'):
        diffs = [(pl['actual'] - pl['proj']) for r in rows[-60:] if r['format'] == fmt for g in r['lineups'] for pl in g['players']]
        if len(diffs) >= 40:
            calib['shift_' + fmt] = round(max(-10.0, min(5.0, statistics.mean(diffs))), 2)
    out = {'schema': 1, 'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
           'calibration': calib, 'calibration_players': n, 'summary': summary, 'slates': rows[-200:],
           'method': 'Original lineups frozen at each slate\'s first tip (or rebuilt from pre-tip snapshots for older days), '
                     'scored with actual FanDuel points. Hindsight best = best possible lineup on that slate. '
                     'sd_scale sizes simulated outcome spreads so 80% of real outcomes land inside the 10-90% band.'}
    old = read(OUT)
    if {k: v for k, v in old.items() if k != 'updated_at'} != {k: v for k, v in out.items() if k != 'updated_at'}:
        OUT.write_text(json.dumps(out, indent=0) + '\n', encoding='utf-8')
    print(json.dumps({'calibration': calib, 'players': n, 'summary': summary}, indent=1))


if __name__ == '__main__':
    main()
