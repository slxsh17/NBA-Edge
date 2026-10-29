#!/usr/bin/env python3
"""NBA EDGE: record the optimizer's own lineups for every slate -> data/lineups/YYYY-MM-DD.json.

Runs after each projection rebuild. For the main slate and every single-game slate it builds
the lineups NBA EDGE would play right now:
  cash        best projected lineup
  tournament  learned tournament settings (data/strategy.json: upside, leverage, fade)
  sim_winner  the lineup that finished top-5% most often in correlated simulations
and simulates each one 2,000 times (same correlated model as the tournament sims) to get its
expected range (10th / 50th / 90th percentile).

A slate's record keeps updating until its first game tips, then it is frozen: that frozen
lineup is the "original lineup" graded by grade_lineups.py after the games.
"""
import datetime as dt
import json
import math
import pathlib
import random
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import field_sim as FS          # noqa: E402

DATA = ROOT / 'data'
OUT = DATA / 'lineups'
SIMS = 2000


def read(p, default=None):
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {} if default is None else default


def tip(t):
    return dt.datetime.fromisoformat(t.replace('Z', '+00:00')) if t else None


def sd_of(p, scale):
    return scale * max(1.0, (p.get('ceiling', p['proj'] * 1.4) - p['proj']) / 1.28)


def simulate(lineup, corr, seed=5, shift=0.0):
    """Distribution of a lineup's total under correlated outcomes (game + team + player)."""
    C = {**FS.DEFAULT_CORR, **(corr or {})}
    rg, rt = C['game'], C['team']
    ri = math.sqrt(max(0.0, 1 - rg * rg - rt * rt))
    scale = C.get('sd_scale', 1.0)
    rng = random.Random(seed)
    games = sorted({tuple(sorted((x['team'], x.get('opp') or x['team']))) for x in lineup})
    teams = sorted({x['team'] for x in lineup})
    tot = []
    for _ in range(SIMS):
        G = {g: rng.gauss(0, 1) for g in games}
        T = {t: rng.gauss(0, 1) for t in teams}
        s = 0.0
        for x in lineup:
            z = rg * G[tuple(sorted((x['team'], x.get('opp') or x['team'])))] + rt * T[x['team']] + ri * rng.gauss(0, 1)
            s += x['mult'] * max(0.0, x['proj'] + shift + sd_of(x, scale) * z)
        tot.append(s)
    tot.sort()
    q = lambda f: round(tot[int(f * (len(tot) - 1))], 1)
    return {'p10': q(0.1), 'p50': q(0.5), 'p90': q(0.9), 'mean': round(sum(tot) / len(tot), 1)}


def build(pool, fmt, scores):
    pool = [p for p in pool if p['proj'] > 0]
    if len(pool) < FS.FORMATS[fmt]['size']:
        return None
    lu = FS.Solver(pool, fmt, 60000).best(scores(pool))
    return [(pool[i], slot) for i, slot in lu] if lu else None


def pack(picks, fmt, strategy, corr, extra=None):
    mult = FS.FORMATS[fmt]['mult']
    rows = [{'name': p['name'], 'team': p['team'], 'opp': p.get('opp'), 'slot': s, 'salary': p['salary'], 'proj': round(p['proj'], 2),
             'ceiling': p.get('ceiling'), 'own': p.get('own'), 'mult': mult.get(s, 1.0)} for p, s in picks]
    order = list(FS.FORMATS[fmt]['need'])
    rows.sort(key=lambda r: order.index(r['slot']))
    out = {'strategy': strategy, 'players': rows, 'proj': round(sum(r['proj'] * r['mult'] for r in rows), 1),
           'salary': sum(int(r['salary'] * (1.5 if r['slot'] == 'MVP' else 1)) for r in rows),
           'sim': simulate(rows, corr, shift=(corr or {}).get('shift_' + fmt, 0.0))}
    return {**out, **(extra or {})}


def lineups_for(pool, fmt, sim_top, tset, corr):
    c, lv, f = tset['ceilW'], tset['levW'], tset['ownFade']
    res = []
    cash = build(pool, fmt, lambda ps: [p['proj'] for p in ps])
    if cash:
        res.append(pack(cash, fmt, 'cash', corr))
    gpp = build(pool, fmt, lambda ps: [(1 - c) * p['proj'] + c * (p.get('ceiling') or p['proj']) - f * (p.get('own') or 0) + lv * (p.get('lev') or 0) for p in ps])
    if gpp:
        res.append(pack(gpp, fmt, 'tournament', corr, {'settings': tset}))
    if sim_top:
        by = {(p['name'], p['team']): p for p in pool}
        t = sim_top[0]
        picks = [(by.get((q['name'], q['team'])), q['slot']) for q in t['players']]
        if all(p for p, _ in picks):
            res.append(pack(picks, fmt, 'sim_winner', corr, {'top5_rate': t.get('top5')}))
    return res


def main():
    today = read(DATA / 'today.json')
    day = today.get('date')
    if not day or not today.get('players'):
        return
    now = dt.datetime.now(dt.timezone.utc)
    OUT.mkdir(exist_ok=True)
    path = OUT / f'{day}.json'
    rec = read(path, {'date': day, 'slates': {}})
    corr = {**(read(DATA / 'model_params.json').get('corr') or {}), **(read(DATA / 'lineup_results.json').get('calibration') or {})}
    st = read(DATA / 'strategy.json')
    tset = (st.get('tournament') or {}).get('best') or {'ceilW': 0.45, 'levW': 0.15, 'ownFade': 0.03}
    players = today['players']
    changed = False

    def put(key, label, fmt, lock, pool, sim_top):
        nonlocal changed
        if lock is None or now >= lock:
            return                     # started: keep the frozen original
        lus = lineups_for(pool, fmt, sim_top, tset, corr)
        if not lus:
            return
        new = {'label': label, 'format': fmt, 'lock': lock.isoformat(), 'recorded_at': now.isoformat(timespec='seconds'), 'lineups': lus}
        old = rec['slates'].get(key)
        if not old or [l['players'] for l in old['lineups']] != [l['players'] for l in lus]:
            rec['slates'][key] = new
            changed = True

    main_games = today.get('games', [])
    locks = [tip(g['time']) for g in main_games if g.get('time')]
    main_pool = [p for p in players if p.get('main') and p['proj'] > 0 and not p.get('out')]
    put('main', 'Main slate', 'classic', min(locks) if locks else None, main_pool, today.get('sim_lineups'))
    by_key = {}
    for p in players:
        by_key[FS_key(p)] = p
    for sg in today.get('single_games', []):
        pool = []
        for r in sg['players']:
            b = by_key.get(r['k'])
            if b and b['proj'] > 0 and not b.get('out') and r.get('status') not in ('OUT', 'DOUBTFUL'):
                pool.append({**b, 'salary': r['salary'], 'own': r.get('own'), 'lev': r.get('lev')})
        put('single:' + '@'.join(sorted((sg['away'], sg['home']))), sg['label'], 'single', tip(sg.get('time')), pool, sg.get('sim_lineups'))
    if changed:
        path.write_text(json.dumps(rec, separators=(',', ':')) + '\n', encoding='utf-8')
        print('recorded', {k: [(l['strategy'], l['proj']) for l in v['lineups']] for k, v in rec['slates'].items()})
    for old in sorted(OUT.glob('*.json'))[:-120]:
        old.unlink()


def FS_key(p):
    from build_slate import norm
    return norm(p['name']) + '|' + p['team']


if __name__ == '__main__':
    main()
