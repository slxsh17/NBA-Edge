#!/usr/bin/env python3
"""NBA EDGE field simulation, leverage and pivots (LeVertical-style).

Field ownership: most of the field builds with optimizers on consensus projections.
We simulate that: hundreds of "public" builds on consensus projections with a little
noise each, and count how often each player shows up. This naturally reproduces the
Player A (37.5 proj, 60% owned) vs Player B (36.5 proj, 30% owned) imbalance.
The rest of the field picks by feel (value/salary heuristic); `field_mix` sets the split.

Optimal rate: simulate actual outcomes from OUR projections and their spread, and count
how often each player is in the best possible lineup.

Leverage = optimal rate - field ownership. Pivots: near-identical options (position,
salary, projection) that the field owns far less.
"""
import math
import random

import highspy

FORMATS = {
    'classic': {'need': {'PG': 1, 'SG': 1, 'SF': 1, 'PF': 1, 'C': 1, 'G/F': 1, 'F/C': 1, 'UTIL': 2}, 'mult': {}, 'sal': {},
                'team_max': 4, 'size': 9},
    'single': {'need': {'MVP': 1, 'UTIL': 5}, 'mult': {'MVP': 1.5}, 'sal': {'MVP': 1.5}, 'team_max': 5, 'size': 6},
}
DEFAULT_OWN = {'field_noise': 0.15, 'consensus_w': 0.8, 'field_mix': 0.7}


def can_fill(pos, slot, fmt):
    if fmt == 'single' or slot == 'UTIL':
        return True
    a = pos.split('/')
    if slot == 'G/F':
        return any(x in ('PG', 'SG', 'SF', 'PF') for x in a)
    if slot == 'F/C':
        return any(x in ('SF', 'PF', 'C') for x in a)
    return slot in a


class Solver:
    """Lineup MILP built once per pool; each simulation only swaps the objective.

    Classic: one binary per player. Positions are guaranteed fillable with Hall's
    condition (every set T of fixed slots has at least |T| chosen players who can fill
    some slot in T), which keeps the model tiny and fast. Slots are assigned afterwards.
    Single game: a binary per player for UTIL and one for MVP.
    """

    def __init__(self, pool, fmt='classic', cap=60000):
        F = FORMATS[fmt]
        self.pool, self.fmt, self.F = pool, fmt, F
        h = highspy.Highs()
        h.silent()
        h.setOptionValue('mip_rel_gap', 0.002)
        h.changeObjectiveSense(highspy.ObjSense.kMaximize)
        n = len(pool)
        self.ncols = n if fmt == 'classic' else 2 * n      # single: [UTIL..., MVP...]
        for _ in range(self.ncols):
            h.addCol(0.0, 0.0, 1.0, 0, [], [])
        h.changeColsIntegrality(self.ncols, list(range(self.ncols)), [highspy.HighsVarType.kInteger] * self.ncols)

        def row(idx, vals, lo, hi):
            h.addRow(lo, hi, len(idx), idx, vals)
        by_t = {}
        for i, p in enumerate(pool):
            by_t.setdefault(p['team'], []).append(i)
        if fmt == 'classic':
            row(list(range(n)), [1.0] * n, F['size'], F['size'])
            fixed = [s for s in F['need'] if s != 'UTIL']
            seen = set()
            for mask in range(1, 1 << len(fixed)):
                T = [fixed[k] for k in range(len(fixed)) if mask >> k & 1]
                idx = tuple(i for i, p in enumerate(pool) if any(can_fill(p['position'], t, fmt) for t in T))
                need = len(T)
                if (idx, need) in seen:
                    continue
                seen.add((idx, need))
                row(list(idx), [1.0] * len(idx), need, 1e9)
            for idx in by_t.values():
                row(idx, [1.0] * len(idx), 0, F['team_max'])
            row(list(range(n)), [float(p['salary']) for p in pool], 0, cap)
        else:
            row(list(range(n)), [1.0] * n, F['need']['UTIL'], F['need']['UTIL'])
            row(list(range(n, 2 * n)), [1.0] * n, 1, 1)
            for i in range(n):
                row([i, n + i], [1.0, 1.0], 0, 1)
            for idx in by_t.values():
                both = idx + [n + i for i in idx]
                row(both, [1.0] * len(both), 0, F['team_max'])
            row(list(range(2 * n)), [float(p['salary']) for p in pool] + [p['salary'] * F['sal']['MVP'] for p in pool], 0, cap)
        self.h = h

    def best(self, scores):
        n = len(self.pool)
        costs = list(scores) if self.fmt == 'classic' else list(scores) + [x * self.F['mult']['MVP'] for x in scores]
        self.h.changeColsCost(self.ncols, list(range(self.ncols)), costs)
        self.h.run()
        if self.h.getModelStatus() != highspy.HighsModelStatus.kOptimal:
            return []
        x = self.h.getSolution().col_value
        if self.fmt == 'single':
            return [(i, 'UTIL') for i in range(n) if x[i] > 0.5] + [(i, 'MVP') for i in range(n) if x[n + i] > 0.5]
        chosen = [i for i in range(n) if x[i] > 0.5]
        return assign_slots(chosen, self.pool)


def assign_slots(chosen, pool):
    """Match chosen players to classic slots (augmenting paths); UTIL takes the rest."""
    slots = ['PG', 'SG', 'SF', 'PF', 'C', 'G/F', 'F/C']
    owner = {}

    def try_assign(i, seen):
        for s in slots:
            if s in seen or not can_fill(pool[i]['position'], s, 'classic'):
                continue
            seen.add(s)
            if s not in owner or try_assign(owner[s], seen):
                owner[s] = i
                return True
        return False
    for i in chosen:
        try_assign(i, set())
    placed = {i: s for s, i in owner.items()}
    return [(i, placed.get(i, 'UTIL')) for i in chosen]


def trim_pool(players, key, fmt, keep=110):
    """Players who could plausibly be optimal: best by projection and by value."""
    live = [p for p in players if p.get(key, 0) > 0]
    if len(live) <= keep:
        return live
    by_proj = sorted(live, key=lambda p: -p[key])[:keep // 2]
    by_val = sorted(live, key=lambda p: -p[key] / max(p['salary'], 1000))[:keep // 2]
    seen, out = set(), []
    for p in by_proj + by_val:
        if id(p) not in seen:
            seen.add(id(p))
            out.append(p)
    return out


def field_ownership(players, fmt='classic', cap=60000, params=None, sims=250, seed=7):
    """Simulated field ownership (%), keyed by id(player)."""
    P = {**DEFAULT_OWN, **(params or {})}
    for p in players:
        ext = p.get('ext')
        p['_cons'] = (P['consensus_w'] * ext + (1 - P['consensus_w']) * p['proj']) if ext is not None and p['proj'] > 0 else p['proj']
    pool = trim_pool(players, '_cons', fmt)
    if len(pool) < FORMATS[fmt]['size']:
        return {}
    solver = Solver(pool, fmt, cap)
    rng = random.Random(seed)
    counts = [0] * len(pool)
    done = 0
    for _ in range(sims):
        scores = [p['_cons'] * math.exp(P['field_noise'] * rng.gauss(0, 1)) for p in pool]
        lu = solver.best(scores)
        if not lu:
            continue
        done += 1
        for i, _s in lu:
            counts[i] += 1
    return {id(p): 100.0 * counts[i] / max(done, 1) for i, p in enumerate(pool)}


DEFAULT_CORR = {'game': 0.277, 'team': 0.176}   # measured on 2025-26: opponents +0.08, teammates +0.11


def optimal_rate(players, fmt='classic', cap=60000, sims=250, seed=11, corr=None, keep_top=40):
    """Play the slate out `sims` times with correlated outcomes and solve the best lineup each
    time. A shared game factor lifts both teams in a shootout; a team factor lifts teammates
    together. Returns ({id(player): % of winning lineups}, [most frequent winning lineups])."""
    C = {**DEFAULT_CORR, **(corr or {})}
    rg, rt = C['game'], C['team']
    ri = math.sqrt(max(0.0, 1 - rg * rg - rt * rt))
    pool = trim_pool(players, 'proj', fmt)
    if len(pool) < FORMATS[fmt]['size']:
        return {}, []
    solver = Solver(pool, fmt, cap)
    rng = random.Random(seed)
    games = sorted({tuple(sorted((p['team'], p.get('opp') or p['team']))) for p in pool})
    counts = [0] * len(pool)
    lineups = {}
    outcomes = []
    done = 0
    for _ in range(sims):
        G = {g: rng.gauss(0, 1) for g in games}
        T = {t: rng.gauss(0, 1) for t in {p['team'] for p in pool}}
        scores = []
        for p in pool:
            sd = max(1.0, (p.get('ceiling', p['proj'] * 1.4) - p['proj']) / 1.28)
            g = G[tuple(sorted((p['team'], p.get('opp') or p['team'])))]
            z = rg * g + rt * T[p['team']] + ri * rng.gauss(0, 1)
            scores.append(max(0.0, p['proj'] + sd * z))
        lu = solver.best(scores)
        if not lu:
            continue
        done += 1
        outcomes.append(scores)
        for i, _s in lu:
            counts[i] += 1
        key = tuple(sorted(lu))
        lineups[key] = lineups.get(key, 0) + 1
    rate = {id(p): 100.0 * counts[i] / max(done, 1) for i, p in enumerate(pool)}
    # Tournament test: every candidate lineup is scored on every simulated night; count how
    # often it lands in the top 5% of all candidates that night.
    mult = FORMATS[fmt]['mult']
    cands = list(lineups)
    hits = [0] * len(cands)
    for sc in outcomes:
        vals = [sum(sc[i] * mult.get(sl, 1.0) for i, sl in key) for key in cands]
        cut = sorted(vals)[int(len(vals) * 0.95)] if len(vals) >= 20 else max(vals)
        for j, v in enumerate(vals):
            if v >= cut:
                hits[j] += 1
    order = sorted(range(len(cands)), key=lambda j: -hits[j])
    top = []
    for j in order[:keep_top]:
        key, n = cands[j], lineups[cands[j]]
        top.append({'top5': round(100.0 * hits[j] / max(len(outcomes), 1), 1), 'freq': round(100.0 * n / max(done, 1), 2),
                    'proj': round(sum(pool[i]['proj'] * mult.get(sl, 1.0) for i, sl in key), 1),
                    'players': [{'name': pool[i]['name'], 'team': pool[i]['team'], 'slot': sl} for i, sl in key]})
    return rate, top


def blend_ownership(players, sim, P, total):
    """Field = optimizer share (simulation) + feel share (existing heuristic), scaled to `total`%."""
    mix = P['field_mix']
    out = {}
    for p in players:
        if p['proj'] <= 0:
            continue
        out[id(p)] = mix * sim.get(id(p), 0.0) + (1 - mix) * (p.get('own_heur') or 0.0)
    s = sum(out.values()) or 1.0
    return {k: v * total / s for k, v in out.items()}


def pivots(players, own_key='own', proj_key='proj'):
    """For chalky players: near-identical alternatives the field owns far less (Player B)."""
    out = {}
    live = [p for p in players if p.get(proj_key, 0) > 0 and not p.get('out')]
    for a in live:
        oa = a.get(own_key) or 0
        if oa < 18:
            continue
        pa = set(a['position'].split('/'))
        cands = []
        for b in live:
            if b is a or b['team'] == a['team'] and False:
                continue
            ob = b.get(own_key) or 0
            if not pa & set(b['position'].split('/')):
                continue
            if abs(b['salary'] - a['salary']) > 400 or b[proj_key] < a[proj_key] - 2.5 or oa - ob < 12:
                continue
            cands.append((a[proj_key] - b[proj_key], -(oa - ob), b))
        cands.sort(key=lambda c: (c[0] > 1.0, c[1]))
        if cands:
            out[id(a)] = [c[2] for c in cands[:2]]
    return out
