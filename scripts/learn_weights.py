#!/usr/bin/env python3
"""NBA EDGE learner -> data/learned_weights.json.

Every graded player-game in data/history/ stores the parts of its projection
(base minutes, points per minute, Vegas/matchup/injury/usage/starter/blowout
adjustments, minutes method) plus what actually happened. This script refits
how much each part should count:

  weights  vegas, matchup, injury, usage, starter, blowout   in [0, 2]
  scale    overall calibration                                in [0.85, 1.15]
  minutes_mult  correction per minutes rule (e.g. role-drop)  in [0.75, 1.25]

Guardrails (so it can't learn noise):
  * Penalty for moving away from 1.0 that shrinks as data grows.
  * Walk-forward check: fit on older days, test on the most recent days. New
    weights are adopted only if they beat the current ones on days they never saw.
  * Adopted weights move halfway from the current values toward the fit.
"""
import datetime as dt
import json
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
from build_slate import DEFAULT_WEIGHTS, final_projection  # noqa: E402

DATA = ROOT / 'data'
OUT = DATA / 'learned_weights.json'
BOUNDS = {'vegas': (0, 2), 'matchup': (0, 2), 'injury': (0, 2), 'usage': (0, 2),
          'starter': (0, 2), 'blowout': (0, 2), 'scale': (0.85, 1.15), 'outside': (0, 1), 'travel': (-0.05, 0.02)}
PRIOR = {'outside': 0.0, 'travel': 0.0}   # penalty pulls each weight toward its prior (1.0 unless listed)
MM_BOUNDS = (0.75, 1.25)
MIN_ROWS = 300          # don't learn anything below this
MIN_METHOD_ROWS = 40    # minutes correction needs this many rows for that rule
HOLDOUT_DAYS = 2
BLEND = 0.5


def load_rows():
    rows = []
    for path in sorted((DATA / 'history').glob('*.json')):
        snap = json.loads(path.read_text(encoding='utf-8'))
        for s in snap.get('players', {}).values():
            if s.get('out') or not s.get('rate') or s.get('base_minutes') is None or 'actual' not in s:
                continue
            if s.get('actual_min', 0) <= 0:     # DNPs are a minutes/news problem, graded separately
                continue
            rows.append({**s, 'date': snap['date']})
    return rows


def mae(rows, W):
    return statistics.mean(abs(r['actual'] - final_projection(r, W)[1]) for r in rows)


def objective(rows, W, lam):
    pen = sum((W.get(k, PRIOR.get(k, 1.0)) - PRIOR.get(k, 1.0)) ** 2 for k in BOUNDS)
    pen += sum((v - 1.0) ** 2 for v in W['minutes_mult'].values())
    return mae(rows, W) + lam * pen


def fit(rows, start):
    """Coordinate search on a shrinking grid. Pure Python, no dependencies."""
    lam = 60.0 / max(len(rows), 1)
    W = json.loads(json.dumps(start))
    methods = {}
    for r in rows:
        methods[r.get('method') or 'other'] = methods.get(r.get('method') or 'other', 0) + 1
    learn_mm = [m for m, n in methods.items() if n >= MIN_METHOD_ROWS]
    for m in learn_mm:
        W['minutes_mult'].setdefault(m, 1.0)
    best = objective(rows, W, lam)
    for step in (0.2, 0.1, 0.05, 0.02):
        improved = True
        while improved:
            improved = False
            for k in list(BOUNDS) + ['mm:' + m for m in learn_mm]:
                for d in (step, -step):
                    trial = json.loads(json.dumps(W))
                    if k.startswith('mm:'):
                        lo, hi = MM_BOUNDS
                        trial['minutes_mult'][k[3:]] = round(min(hi, max(lo, trial['minutes_mult'][k[3:]] + d)), 3)
                    else:
                        lo, hi = BOUNDS[k]
                        trial[k] = round(min(hi, max(lo, trial[k] + d)), 3)
                    val = objective(rows, trial, lam)
                    if val < best - 1e-6:
                        W, best, improved = trial, val, True
    return W


def blend(cur, new, a):
    out = {k: round(cur.get(k, PRIOR.get(k, 1.0)) + a * (new[k] - cur.get(k, PRIOR.get(k, 1.0))), 3) for k in BOUNDS}
    mm = {}
    for m in set(cur['minutes_mult']) | set(new['minutes_mult']):
        c, n = cur['minutes_mult'].get(m, 1.0), new['minutes_mult'].get(m, 1.0)
        mm[m] = round(c + a * (n - c), 3)
    out['minutes_mult'] = mm
    return out


def describe(old, new):
    names = {'vegas': 'Vegas', 'matchup': 'Matchup', 'injury': 'Injury minutes', 'usage': 'Usage bump',
             'starter': 'Starter bump', 'blowout': 'Blowout cut', 'scale': 'Overall scale', 'outside': 'Outside projection blend', 'travel': 'Travel (per 1,000 km)'}
    ch = []
    for k, n in names.items():
        o = old.get(k, PRIOR.get(k, 1.0))
        if abs(new[k] - o) >= 0.02:
            ch.append(f'{n} {o:.2f} → {new[k]:.2f}')
    for m, v in new['minutes_mult'].items():
        o = old['minutes_mult'].get(m, 1.0)
        if abs(v - o) >= 0.02:
            ch.append(f'Minutes ({m}) {o:.2f} → {v:.2f}')
    return ch


def main():
    prev = json.loads(OUT.read_text(encoding='utf-8')) if OUT.exists() else {}
    # Learn once per day (first pipeline run of the morning), so weights never shift mid-slate.
    from zoneinfo import ZoneInfo
    et = dt.datetime.now(ZoneInfo('America/New_York'))
    last = prev.get('updated_at')
    if last and '--force' not in sys.argv:
        last_et = dt.datetime.fromisoformat(last).astimezone(ZoneInfo('America/New_York'))
        # Learn once per morning, after every game from the night before has a final box score.
        if et.hour < 8:
            print('Waiting until 8am ET so late games are final; keeping weights')
            return
        if last_et.date() == et.date() and last_et.hour >= 8:
            print('Already learned this morning; keeping weights')
            return
    cur = {**DEFAULT_WEIGHTS, **(prev.get('weights') or {})}
    cur['minutes_mult'] = dict(cur.get('minutes_mult') or {})
    for k in BOUNDS:
        cur.setdefault(k, PRIOR.get(k, 1.0))
    rows = load_rows()
    days = sorted({r['date'] for r in rows})
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    status = {'updated_at': now, 'samples': len(rows), 'days': len(days)}
    if len(rows) < MIN_ROWS or len(days) < HOLDOUT_DAYS + 2:
        out = {**prev, **status, 'weights': cur,
               'decision': f'Waiting for more data ({len(rows)} graded player-games over {len(days)} days; needs {MIN_ROWS} over {HOLDOUT_DAYS + 2}+).'}
        OUT.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
        print(out['decision'])
        return
    test_days = set(days[-HOLDOUT_DAYS:])
    train = [r for r in rows if r['date'] not in test_days]
    test = [r for r in rows if r['date'] in test_days]
    fitted = fit(train, cur)
    base = {**DEFAULT_WEIGHTS, 'minutes_mult': {}}
    v_def, v_cur, v_fit = mae(test, base), mae(test, cur), mae(test, fitted)
    validation = {'test_days': sorted(test_days), 'test_rows': len(test), 'default_mae': round(v_def, 3),
                  'current_mae': round(v_cur, 3), 'fitted_mae': round(v_fit, 3)}
    log = prev.get('log', [])
    if v_fit < v_cur - 0.02:
        full = fit(rows, cur)                    # refit on everything, then move halfway
        new = blend(cur, full, BLEND)
        changes = describe(cur, new)
        decision = (f'Adopted: on {len(test)} held-out player-games the refit missed by {v_fit:.2f} '
                    f'vs {v_cur:.2f} with current weights.')
        log.append({'date': now[:10], 'adopted': True, 'changes': changes, 'heldout_gain': round(v_cur - v_fit, 3)})
    else:
        new = cur
        decision = (f'Kept current weights: the refit didn’t beat them on held-out days '
                    f'({v_fit:.2f} vs {v_cur:.2f}).')
        log.append({'date': now[:10], 'adopted': False, 'changes': [], 'heldout_gain': round(v_cur - v_fit, 3)})
    all_mae = {'default': round(mae(rows, base), 3), 'learned': round(mae(rows, new), 3)}
    out = {'schema': 1, **status, 'weights': new, 'previous': cur, 'validation': validation,
           'in_sample_mae': all_mae, 'decision': decision, 'log': log[-30:],
           'method': 'Coordinate search minimizing average miss with a pull toward 1.0; adopted only if it beats current weights on the most recent days held out from fitting; moves halfway.'}
    OUT.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    print(decision)
    for c in describe(cur, new):
        print(' ', c)


if __name__ == '__main__':
    main()
