#!/usr/bin/env python3
"""NBA EDGE strategy learner -> data/strategy.json.

Replays past main slates with the projections and ownership we had before tip-off
(data/history/) and FanDuel salaries (data/archive/slate_*.json), builds lineups under
different strategy settings, and scores them with what players actually scored.

  Cash:        one lineup; measured by its actual points.
  Tournament:  five lineups (each differs by 2+ players); measured by the best of the five,
               since a tournament only pays the top of the field.

Settings tried are the site's Model settings: upside weight (ceilW), leverage weight (levW)
and fade popular players (ownFade). "Simulated winners" (lineups that finished top 5% most
often in correlated simulations) are tested as their own strategy.
Your logged results (data/my_results.json) are summarized alongside.
"""
import datetime as dt
import json
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import field_sim as FS          # noqa: E402
from build_slate import canon, norm  # noqa: E402

DATA = ROOT / 'data'
OUT = DATA / 'strategy.json'
CEIL = [0.0, 0.25, 0.45, 0.65, 0.85]
LEV = [0.0, 0.15, 0.3, 0.5]
FADE = [0.0, 0.03, 0.08, 0.15]
SITE_DEFAULT = {'ceilW': 0.45, 'levW': 0.15, 'ownFade': 0.03}
N_GPP = 5


def read(p, default=None):
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {} if default is None else default


def day_pool(day):
    snap = read(DATA / 'history' / f'{day}.json').get('players') or {}
    slate = read(DATA / 'archive' / f'slate_{day}.json').get('players') or []
    pool = []
    for sp in slate:
        k = norm(sp['name']) + '|' + canon(sp['team'])
        s = snap.get(k)
        if not s or s.get('out') or (s.get('proj') or 0) <= 0 or sp.get('status') in ('OUT', 'DOUBTFUL'):
            continue
        if 'actual' not in s:          # game not graded yet
            return None
        sd = 0.28 * s['proj'] + 3 + (3 if s.get('shaky') else 0)
        pool.append({'k': k, 'name': sp['name'], 'team': canon(sp['team']), 'opp': canon(sp.get('opp')), 'position': sp['position'],
                     'salary': sp['salary'], 'proj': s['proj'], 'ceiling': s['proj'] + 1.28 * sd, 'own': s.get('own') or 0.0,
                     'actual': s.get('actual') or 0.0})
    return pool if len(pool) >= 30 else None


def build(pool, scores, n, min_unique=2):
    solver = FS.Solver(pool, 'classic', 60000)
    out = []
    for _ in range(n):
        lu = solver.best(scores)
        if not lu:
            break
        idx = [i for i, _s in lu]
        out.append(idx)
        solver.h.addRow(0, 9 - min_unique, len(idx), idx, [1.0] * len(idx))
    return out


def actual(pool, idx):
    return sum(pool[i]['actual'] for i in idx)


def main():
    days = sorted(p.stem for p in (DATA / 'history').glob('*.json'))
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    corr = read(DATA / 'model_params.json').get('corr')
    prev = read(OUT)
    ready = [d for d in days if d < today and day_pool(d)]
    if ready == prev.get('days') and '--force' not in sys.argv:
        print('No newly graded nights; keeping strategy results')
        return
    nights = []
    for day in ready:
        pool = day_pool(day)
        rate, top = FS.optimal_rate(pool, 'classic', 60000, sims=200, corr=corr, keep_top=40)
        for p in pool:
            p['lev'] = rate.get(id(p), 0.0) - p['own']
        by_name = {(p['name'], p['team']): i for i, p in enumerate(pool)}
        hind = build(pool, [p['actual'] for p in pool], 1)
        best_possible = actual(pool, hind[0]) if hind else None
        res = {}
        for c in CEIL:
            for lv in LEV:
                for f in FADE:
                    sc = [(1 - c) * p['proj'] + c * p['ceiling'] - f * p['own'] + lv * p['lev'] for p in pool]
                    lus = build(pool, sc, N_GPP)
                    if len(lus) < N_GPP:
                        continue
                    res[(c, lv, f)] = {'one': actual(pool, lus[0]), 'best5': max(actual(pool, x) for x in lus)}
        cash = build(pool, [p['proj'] for p in pool], 1)
        sim5 = []
        for t in top:
            idx = [by_name.get((q['name'], q['team'])) for q in t['players']]
            if None in idx:
                continue
            if all(len(set(idx) & set(s)) <= 7 for s in sim5):
                sim5.append(idx)
            if len(sim5) == N_GPP:
                break
        nights.append({'day': day, 'players': len(pool), 'best_possible': best_possible, 'cash': actual(pool, cash[0]) if cash else None,
                       'grid': res, 'sim_best5': max(actual(pool, x) for x in sim5) if len(sim5) == N_GPP else None})
        print(day, 'pool', len(pool), 'hindsight best', round(best_possible or 0, 1), 'cash lineup', round(nights[-1]['cash'] or 0, 1))
    if not nights:
        print('No graded nights yet')
        return
    keys = set.intersection(*(set(n['grid']) for n in nights))

    def avg(k, field):
        return statistics.mean(n['grid'][k][field] for n in nights)
    rank_gpp = sorted(keys, key=lambda k: -avg(k, 'best5'))
    rank_cash = sorted(keys, key=lambda k: -avg(k, 'one'))
    as_dict = lambda k: {'ceilW': k[0], 'levW': k[1], 'ownFade': k[2]}
    dflt = (SITE_DEFAULT['ceilW'], SITE_DEFAULT['levW'], SITE_DEFAULT['ownFade'])

    # Guard against overfitting a handful of nights: leave one night out, pick the best settings on
    # the others, score them on the night left out. Only adopt "learned" settings when that honest
    # test beats the defaults; otherwise keep the defaults. (Oct 29: settings that topped 7 nights
    # with leverage 0.5 faded Acuff and Fears, the two best plays, and scored 252 vs 354.)
    def loo(field, ref):
        if len(nights) < 4:
            return None, None
        got, base = [], []
        for j, n in enumerate(nights):
            rest = [m for i, m in enumerate(nights) if i != j]
            pick = max(keys, key=lambda k: statistics.mean(m['grid'][k][field] for m in rest))
            got.append(n['grid'][pick][field])
            base.append(n['grid'][ref][field])
        return statistics.mean(got), statistics.mean(base)
    loo_g, loo_gd = loo('best5', dflt) if dflt in keys else (None, None)
    gpp_pick = rank_gpp[0] if (loo_g is not None and loo_g > loo_gd + 3) else (dflt if dflt in keys else rank_gpp[0])
    cash0 = (0.0, 0.0, 0.0)
    loo_c, loo_cd = loo('one', cash0) if cash0 in keys else (None, None)
    cash_pick = rank_cash[0] if (loo_c is not None and loo_c > loo_cd + 3) else (cash0 if cash0 in keys else rank_cash[0])
    sims = [n['sim_best5'] for n in nights if n['sim_best5'] is not None]
    mine = read(DATA / 'my_results.json').get('days', [])
    entries = [c for d in mine for c in d.get('contests', [])]
    out = {
        'schema': 1, 'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
        'nights': len(nights), 'days': [n['day'] for n in nights],
        'avg_hindsight_best': round(statistics.mean(n['best_possible'] for n in nights), 1),
        'cash': {'best': as_dict(cash_pick), 'best_avg': round(avg(cash_pick, 'one'), 1), 'in_sample_top': as_dict(rank_cash[0]),
                 'leave_one_out': {'learned': round(loo_c, 1) if loo_c else None, 'projection_only': round(loo_cd, 1) if loo_cd else None},
                 'projection_only_avg': round(statistics.mean(n['cash'] for n in nights), 1)},
        'tournament': {'best': as_dict(gpp_pick), 'best_avg_best_of_5': round(avg(gpp_pick, 'best5'), 1), 'in_sample_top': as_dict(rank_gpp[0]),
                       'leave_one_out': {'learned': round(loo_g, 1) if loo_g else None, 'defaults': round(loo_gd, 1) if loo_gd else None},
                       'site_default_avg_best_of_5': round(avg(dflt, 'best5'), 1) if dflt in keys else None,
                       'sim_winners_avg_best_of_5': round(statistics.mean(sims), 1) if len(sims) == len(nights) else None,
                       'top5': [{**as_dict(k), 'avg_best_of_5': round(avg(k, 'best5'), 1)} for k in rank_gpp[:5]]},
        'your_results': {'nights': len(mine), 'entries': len(entries), 'cashed': sum(1 for c in entries if (c.get('won') or 0) > 0),
                         'fees': sum(c.get('fee') or 0 for c in entries), 'won': sum(c.get('won') or 0 for c in entries)},
        'caution': ('Small sample: settings are a guide, not a rule, until there are 20+ nights.' if len(nights) < 20 else ''),
        'method': 'Replays past main slates with pre-tip projections and ownership; lineups scored by actual FanDuel points. '
                  'Tournament = best of 5 lineups (2+ players different).',
        'log': (prev.get('log') or [])[-30:],
    }
    out['log'].append({'date': out['updated_at'][:10], 'nights': len(nights), 'gpp': out['tournament']['best'], 'cash': out['cash']['best']})
    OUT.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    print(json.dumps({k: out[k] for k in ('nights', 'avg_hindsight_best', 'cash', 'tournament', 'your_results')}, indent=1))


if __name__ == '__main__':
    main()
