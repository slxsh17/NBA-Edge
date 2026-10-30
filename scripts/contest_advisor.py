#!/usr/bin/env python3
"""NBA EDGE contest advisor: which FanDuel contests a lineup belongs in.

1. Simulated field. Builds ~1,200 field lineups for the slate the way the public builds them:
   optimizer lineups on public (consensus) projections with noise, plus by-feel lineups drawn
   in proportion to projected ownership. Field ownership comes out close to the ownership model.
2. Simulated nights. Plays the slate out 300 times with the correlated outcome model (game and
   team factors, player spreads sized by ceiling and the learned calibration) and scores every
   field lineup each night. That gives each night's cash lines:
     double_up  top 43.5% (FanDuel "Big $2 Double Up (Top N Win)")
     fifty      top 50%
     gpp_cash   top ~22% (tournament min-cash)
     top1       top 1%
3. Publishes data/contest_sims.json: per-night cash lines + per-player simulated scores, so the
   site can score ANY lineup (built, simulated winner or entered) against tonight's field.
4. History (data/contest_history.json): for every graded night, rebuilds the field from the
   pre-tip projections and ownership, scores it with ACTUAL results, and checks whether NBA
   EDGE's cash and tournament lineups would have beaten each line. Real lines from your
   screenshots are compared with the estimate to keep it honest.
Also reads FanDuel contest economics (entries, fees, prize pools) from data/ownership/.
"""
import datetime as dt
import json
import math
import pathlib
import random
import re
import statistics
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import field_sim as FS          # noqa: E402
from build_slate import canon, norm  # noqa: E402

DATA = ROOT / 'data'
NIGHTS = 300
FIELD = 1200
LINES = {'double_up': 0.565, 'fifty': 0.50, 'gpp_cash': 0.78, 'top1': 0.99}
# Cash lines you've sent from FanDuel (screenshots), to check the simulated lines against.
# Field sharpness (92% optimizer lineups, 6.5% noise) was fitted so the Oct 29 Double Up line
# comes out at the real 368.3: FanDuel cash-game fields are sharp. On Oct 27 the same field put
# your 302 at the 69th percentile; it actually finished 83rd percentile in two smaller
# single-entry tournaments, so for casual contests these numbers are on the cautious side.
OBSERVED = {'2026-10-29': {'double_up': 368.3, 'layup_20max_cash': 362.4, 'block_se_cash': 361.7},
            '2026-10-27': {'your_302_percentile_small_rebound': 81.6, 'your_302_percentile_assist_se': 83.0}}


def read(p, default=None):
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {} if default is None else default


def slots():
    return [s for s, n in FS.FORMATS['classic']['need'].items() for _ in range(n)]


def feel_lineup(pool, rng, w):
    """By-feel lineup: players drawn in proportion to ownership, positions and cap respected,
    spending most of the cap like real entries do."""
    order = ['C', 'PG', 'SG', 'SF', 'PF', 'F/C', 'G/F', 'UTIL', 'UTIL']
    min_sal = min(p['salary'] for p in pool)
    for _ in range(60):
        chosen, left = [], 60000
        ok = True
        for k, sl in enumerate(order):
            rem = len(order) - k - 1
            cands = [i for i, p in enumerate(pool) if i not in chosen and FS.can_fill(p['position'], sl, 'classic')
                     and p['salary'] <= left - rem * min_sal]
            if not cands:
                ok = False
                break
            ws = [w[i] for i in cands]
            i = rng.choices(cands, weights=ws)[0]
            chosen.append(i)
            left -= pool[i]['salary']
        if ok and left <= 2500:
            tc = {}
            if all((tc.__setitem__(pool[i]['team'], tc.get(pool[i]['team'], 0) + 1) or tc[pool[i]['team']] <= 4) for i in chosen):
                return tuple(sorted(chosen))
    return None


def build_field(pool, n=FIELD, seed=7, opt_share=0.92, noise=0.065):
    rng = random.Random(seed)
    solver = FS.Solver(pool, 'classic', 60000)
    base = [(p.get('ext') if p.get('ext') else p['proj']) * 0.5 + p['proj'] * 0.5 for p in pool]
    field = []
    for _ in range(int(n * opt_share)):
        lu = solver.best([b * math.exp(noise * rng.gauss(0, 1)) for b in base])
        if lu:
            field.append(tuple(sorted(i for i, _ in lu)))
    w = [max(p.get('own') or 0, 0.3) ** 1.1 for p in pool]
    while len(field) < n:
        lu = feel_lineup(pool, rng, w)
        if lu:
            field.append(lu)
    return field


def outcomes(pool, corr, nights=NIGHTS, seed=11):
    C = {**FS.DEFAULT_CORR, **(corr or {})}
    rg, rt = C['game'], C['team']
    ri = math.sqrt(max(0.0, 1 - rg * rg - rt * rt))
    sc = C.get('sd_scale', 1.0)
    rng = np.random.default_rng(seed)
    games = sorted({tuple(sorted((p['team'], p.get('opp') or p['team']))) for p in pool})
    teams = sorted({p['team'] for p in pool})
    gi = np.array([games.index(tuple(sorted((p['team'], p.get('opp') or p['team'])))) for p in pool])
    ti = np.array([teams.index(p['team']) for p in pool])
    proj = np.array([p['proj'] for p in pool])
    sd = sc * np.maximum(1.0, (np.array([p.get('ceiling') or p['proj'] * 1.4 for p in pool]) - proj) / 1.28)
    G = rng.standard_normal((nights, len(games)))
    T = rng.standard_normal((nights, len(teams)))
    E = rng.standard_normal((nights, len(pool)))
    Z = rg * G[:, gi] + rt * T[:, ti] + ri * E
    return np.maximum(0.0, proj + sd * Z)


def lines_from(scores):
    s = np.sort(scores, axis=-1)
    n = s.shape[-1]
    return {k: s[..., min(n - 1, int(q * n))] for k, q in LINES.items()}


def contest_economics():
    """Rake and field size by contest type, from FanDuel contests LineStar tracked."""
    rows = []
    for f in sorted((DATA / 'ownership').glob('*.json')):
        for s in read(f).get('slates', []):
            for c in s.get('contests', []):
                name, fee, n = c.get('name') or '', c.get('fee') or 0, c.get('entries') or 0
                m = re.match(r'\$([\d.]+)([KM]?)', name)
                pool = (c.get('extra') or {}).get('Purse')
                if not pool and m:
                    pool = float(m.group(1)) * {'': 1, 'K': 1e3, 'M': 1e6}[m.group(2)]
                if not (fee and n and pool):
                    continue
                kind = 'Double Up' if 'Double Up' in name else 'Penny tournament (Piggy Bank)' if fee < 0.25 else 'Tournament'
                top = re.search(r'Top ([\d,]+) Win', name)
                rows.append({'kind': kind, 'fee': fee, 'entries': n, 'rake': 1 - pool / (fee * n),
                             'win_share': int(top.group(1).replace(',', '')) / n if top else None})
    out = {}
    for kind in sorted({r['kind'] for r in rows}):
        rs = [r for r in rows if r['kind'] == kind]
        out[kind] = {'contests': len(rs), 'avg_rake_pct': round(100 * statistics.mean(r['rake'] for r in rs), 1),
                     'typical_entries': int(statistics.median(r['entries'] for r in rs)),
                     'fees': sorted({r['fee'] for r in rs})}
        ws = [r['win_share'] for r in rs if r['win_share']]
        if ws:
            out[kind]['share_that_wins_pct'] = round(100 * statistics.mean(ws), 1)
            out[kind]['pays'] = '2x entry'
    return out


def past_pool(day):
    snap = read(DATA / 'history' / f'{day}.json').get('players') or {}
    slate = read(DATA / 'archive' / f'slate_{day}.json').get('players') or []
    pool = []
    for sp in slate:
        s = snap.get(norm(sp['name']) + '|' + canon(sp['team']))
        if not s or s.get('out') or (s.get('proj') or 0) <= 0 or sp.get('status') in ('OUT', 'DOUBTFUL'):
            continue
        if 'actual' not in s:
            return None
        sd = 0.28 * s['proj'] + 3 + (3 if s.get('shaky') else 0)
        pool.append({'name': sp['name'], 'team': canon(sp['team']), 'opp': canon(sp.get('opp')), 'position': sp['position'],
                     'salary': sp['salary'], 'proj': s['proj'], 'ceiling': s['proj'] + 1.28 * sd, 'own': s.get('own') or 0.0,
                     'ext': s.get('ext'), 'actual': s['actual']})
    return pool if len(pool) >= 30 else None


def history():
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    lr = {(r['date'], r['slate']): r for r in read(DATA / 'lineup_results.json').get('slates', [])}
    prev = {n['date']: n for n in read(DATA / 'contest_history.json').get('nights', [])}
    nights = []
    for f in sorted((DATA / 'history').glob('*.json')):
        day = f.stem
        if day >= today or (day, 'main') not in lr:
            continue
        if day in prev and prev[day].get('version') == 3:
            nights.append(prev[day])
            continue
        pool = past_pool(day)
        if not pool:
            continue
        field = build_field(pool, n=800, seed=sum(map(ord, day)))
        act = np.array([p['actual'] for p in pool])
        fs = np.array([act[list(lu)].sum() for lu in field])
        L = {k: round(float(v), 1) for k, v in lines_from(fs).items()}
        L['best_in_field'] = round(float(fs.max()), 1)
        r = lr[(day, 'main')]
        mine = {g['strategy']: g['actual'] for g in r['lineups']}
        # What the simulator would have said before tip-off: our cash lineup minus the Double Up
        # line on each simulated night (no bias correction). Used to calibrate the site's odds.
        key = {(p['name'], p['team']): i for i, p in enumerate(pool)}
        cash = next((g for g in r['lineups'] if g['strategy'] == 'cash'), None)
        ids = [key.get((x['name'], canon(x['team']))) for x in (cash or {}).get('players', [])]
        diffs = None
        if cash and None not in ids:
            corr = {**(read(DATA / 'model_params.json').get('corr') or {}), 'sd_scale': (read(DATA / 'lineup_results.json').get('calibration') or {}).get('sd_scale', 1.0)}
            O = outcomes(pool, corr, nights=NIGHTS, seed=2)
            fsim = O[:, np.array(field)].sum(axis=2)
            line = np.sort(fsim, axis=1)[:, int(LINES['double_up'] * fsim.shape[1])]
            diffs = [round(float(x), 1) for x in O[:, ids].sum(axis=1) - line]
        res = {'date': day, 'version': 3, 'cash_vs_line_sim': diffs, 'sim_lines': L, 'observed_lines': OBSERVED.get(day),
               'hindsight_best': (r.get('hindsight_best') or {}).get('score'),
               'nba_edge': {k: {'score': v, 'beats_double_up': v >= L['double_up'], 'beats_gpp_cash': v >= L['gpp_cash'],
                                'field_percentile': round(float((fs < v).mean() * 100), 1)} for k, v in mine.items()}}
        nights.append(res)
        print(day, 'lines', L, 'observed', OBSERVED.get(day), {k: (v['score'], v['field_percentile']) for k, v in res['nba_edge'].items()})
    # Odds calibration: the per-player shift that makes the simulator's pre-tip Double Up odds for
    # our cash lineup match how often it actually beat the line (least squares on win/loss).
    cal = [(np.array(n['cash_vs_line_sim']), n['nba_edge']['cash']['beats_double_up']) for n in nights
           if n.get('cash_vs_line_sim') and 'cash' in n['nba_edge']]
    odds_shift = None
    if len(cal) >= 5:
        best = min((sum((float((d + 9 * sh >= 0).mean()) - y) ** 2 for d, y in cal), sh) for sh in np.arange(-6.0, 0.01, 0.1))
        odds_shift = round(float(best[1]), 1)
    summ = {'odds_shift_per_player': odds_shift, 'odds_calibration_nights': len(cal)}
    for strat in ('cash', 'tournament', 'sim_winner'):  # noqa
        xs = [n['nba_edge'][strat] for n in nights if strat in n['nba_edge']]
        if xs:
            summ[strat] = {'nights': len(xs), 'double_up_wins': sum(x['beats_double_up'] for x in xs),
                           'gpp_cashes': sum(x['beats_gpp_cash'] for x in xs),
                           'avg_field_percentile': round(statistics.mean(x['field_percentile'] for x in xs), 1)}
    return nights, summ


# FanDuel contest types, from the 74 contests LineStar tracked (Oct 20-29: entries, fees, prize
# pools, "Top N Win") and the contests you've entered (screenshots). field: which simulated field
# the contest is scored against. Small single-entry contests were softer than Double Ups: on Oct 29
# their cash lines were 340-362 vs 368.3 for the Big Double Up.
CATALOG = [
    {'id': 'double_up', 'name': 'Big Double Up', 'examples': 'Big $2 / $5 Double Up', 'fees': [2, 5, 10], 'field': 'sharp',
     'kind': 'double', 'share': 0.435, 'mult': 2.0, 'max': 1, 'entries': 1500},
    {'id': 'fifty', 'name': '50/50', 'examples': '50/50s', 'fees': [1, 2, 5], 'field': 'sharp', 'kind': 'double', 'share': 0.5, 'mult': 1.74, 'max': 1, 'entries': 500},
    {'id': 'se', 'name': 'Single-entry tournament', 'examples': 'Dribbler $1, Block $2, Assist $5, Dime $10', 'fees': [1, 2, 5, 10],
     'field': 'casual', 'kind': 'gpp', 'share': 0.2, 'rake': 0.156, 'max': 1, 'entries': 800},
    {'id': 'small', 'name': 'Small-field tournament', 'examples': 'Small Rebound $12 (~50 entries)', 'fees': [12], 'field': 'casual',
     'kind': 'gpp', 'share': 0.2, 'rake': 0.15, 'max': 1, 'entries': 50},
    {'id': 'me20', 'name': 'Multi-entry tournament (20-25 max)', 'examples': "Layup $4, Pick N' Roll $2.22", 'fees': [2.22, 4], 'field': 'casual',
     'kind': 'gpp', 'share': 0.22, 'rake': 0.156, 'max': 20, 'entries': 8000},
    {'id': 'penny', 'name': 'Piggy Bank Shot', 'examples': '$0.05, 150 max', 'fees': [0.05], 'field': 'sharp', 'kind': 'gpp',
     'share': 0.22, 'rake': 0.131, 'max': 150, 'entries': 100000},
    {'id': 'shot', 'name': 'The Shot', 'examples': '$6-$9, 150 max', 'fees': [6, 9], 'field': 'sharp', 'kind': 'gpp',
     'share': 0.22, 'rake': 0.156, 'max': 150, 'entries': 25000},
]
QS = [0.5, 1, 2, 3, 5, 7.5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 56.5, 60, 65, 70, 75, 78, 80, 82, 85, 88, 90,
      92, 94, 95, 96, 97, 98, 99, 99.5, 99.8, 99.9, 99.95]


def payout(c, q):
    """Prize as a multiple of the entry fee for finishing in the top fraction q of contest c.
    Double Ups pay a flat multiple. Tournaments: min cash 2x at the cash line, rising as a power
    law to the top, sized so the total paid equals the prize pool (entry fees minus FanDuel's cut)."""
    if c['kind'] == 'double':
        return c['mult'] if q <= c['share'] else 0.0
    if q > c['share']:
        return 0.0
    b = 1 - 2 * c['share'] / (1 - c['rake'])
    return 2.0 * (max(q, 1.0 / c['entries']) / c['share']) ** -b


def contest_ev(lineup_scores, quants):
    """lineup_scores: our score per night; quants: {field: nights x len(QS)}. Returns per contest
    expected return per $1, chance to cash, and the spread of returns."""
    out = {}
    for c in CATALOG:
        Q = quants[c['field']]
        ms = []
        for n, sc in enumerate(lineup_scores):
            pct = float(np.interp(sc, Q[n], QS, left=0.0, right=99.99))
            ms.append(payout(c, max(1e-6, 1 - pct / 100)))
        ms = np.array(ms)
        out[c['id']] = {'ev': round(float(ms.mean()), 3), 'cash': round(float((ms > 0).mean()), 3), 'sd': round(float(ms.std()), 3)}
    return out


def live(corr, odds_shift=None):
    today = read(DATA / 'today.json')
    if not today.get('players'):
        return None
    pool = [p for p in today['players'] if p.get('main') and p['proj'] > 0 and not p.get('out')]
    if len(pool) < 30:
        return None
    import hashlib
    sig = hashlib.sha1(json.dumps([today['date'], corr, odds_shift] + [[p['name'], p['team'], p['salary'], round(p['proj'], 1), round(p.get('own') or 0)] for p in pool]).encode()).hexdigest()
    if read(DATA / 'contest_sims.json').get('sig') == sig:
        return 'same'
    field = build_field(pool)
    casual = build_field(pool, seed=9, opt_share=0.65, noise=0.18)
    own_sim = np.zeros(len(pool))
    for lu in field:
        own_sim[list(lu)] += 1
    own_sim = own_sim / len(field) * 100
    O = outcomes(pool, corr)
    idx = np.array(field)
    fscores = O[:, idx].sum(axis=2)                     # nights x field
    L = lines_from(fscores)
    cscores = O[:, np.array(casual)].sum(axis=2)
    quants = {'sharp': np.percentile(fscores, QS, axis=1).T, 'casual': np.percentile(cscores, QS, axis=1).T}
    # NBA EDGE's own lineups for tonight, run through every contest type
    shift = (odds_shift or 0.0)
    rec = read(DATA / 'lineups' / f"{today['date']}.json").get('slates', {}).get('main', {})
    key = {(p['name'], p['team']): i for i, p in enumerate(pool)}
    evals = {}
    for lu in rec.get('lineups', []):
        ids = [key.get((x['name'], x['team'])) for x in lu['players']]
        if None in ids:
            continue
        sc = O[:, ids].sum(axis=1) + 9 * shift
        evals[lu['strategy']] = contest_ev(sc, quants)
    keep = [i for i, p in enumerate(pool) if p['proj'] >= 6]
    corr_own = float(np.corrcoef(own_sim, [p.get('own') or 0 for p in pool])[0, 1])
    return {'sig': sig, 'odds_shift_per_player': odds_shift, 'date': today['date'], 'built_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
            'nights': NIGHTS, 'field_size': len(field), 'field_own_vs_model_corr': round(corr_own, 2),
            'line_shares': LINES,
            'lines': {k: [int(round(float(x))) for x in v] for k, v in L.items()},
            'lines_median': {k: round(float(np.median(v)), 1) for k, v in L.items()},
            'catalog': CATALOG, 'qs': QS,
            'quants': {f: [[int(round(float(x))) for x in row] for row in Q] for f, Q in quants.items()},
            'nba_edge_contest_ev': evals,
            'players': [norm(pool[i]['name']) + '|' + pool[i]['team'] for i in keep],
            'scores': [[int(round(float(O[n, i]))) for i in keep] for n in range(NIGHTS)]}


def main():
    corr = {**(read(DATA / 'model_params.json').get('corr') or {}), **(read(DATA / 'lineup_results.json').get('calibration') or {})}
    nights, summ = history()
    econ = contest_economics()
    hist = {'schema': 1, 'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
            'economics': econ, 'summary': summ, 'nights': nights,
            'method': 'Field rebuilt from pre-tip projections and ownership, scored with actual FanDuel points; '
                      'lines are the field percentiles each contest pays.'}
    (DATA / 'contest_history.json').write_text(json.dumps(hist, indent=1) + '\n', encoding='utf-8')
    print(json.dumps({'economics': econ, 'summary': summ}, indent=1))
    lv = live(corr, summ.get('odds_shift_per_player'))
    if lv == 'same':
        print('live: projections unchanged; keeping contest_sims.json')
    elif lv:
        old = read(DATA / 'contest_sims.json')
        (DATA / 'contest_sims.json').write_text(json.dumps(lv, separators=(',', ':')) + '\n', encoding='utf-8')
        print('live', lv['date'], 'lines (median):', lv['lines_median'], 'field own corr', lv['field_own_vs_model_corr'])


if __name__ == '__main__':
    main()
