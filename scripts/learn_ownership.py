#!/usr/bin/env python3
"""NBA EDGE ownership learner -> data/ownership_params.json.

Grades the ownership model against real FanDuel ownership (data/ownership/, from
fetch_ownership.py) and tunes it:
  field_noise   how much public optimizer builds disagree with each other
  consensus_w   how much the field follows consensus (DailyFantasyFuel) vs our numbers
  field_mix     share of the field that builds with optimizers vs by feel
  chalk_power   how strongly ownership concentrates on the top plays
Adopted only if better than the current settings on the most recent held-out day.
Also reports LineStar's own projected ownership error on the same players as a benchmark.
"""
import datetime as dt
import json
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import field_sim as FS          # noqa: E402
from build_slate import apply_consensus, canon, chalk, norm  # noqa: E402

DATA = ROOT / 'data'
OUT = DATA / 'ownership_params.json'
NOISE = [0.05, 0.1, 0.15, 0.22, 0.32]
CONS = [0.0, 0.5, 0.8, 1.0]
MIX = [round(0.3 + 0.1 * k, 1) for k in range(8)]
POWER = [0.8, 1.0, 1.2, 1.4, 1.6]
EXT = [0.0, 0.25, 0.5, 0.75]     # weight on public projected ownership (LineStar history; + Fantasy Team Advice live)
MENTION = [0.0]                 # pick-article mentions: learned once a few days of archived consensus exist
SIMS = 120


def read(p):
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def day_data(day):
    """Main-slate players with our projection/heuristic, plus named actual ownership."""
    own = read(DATA / 'ownership' / f'{day}.json')
    slate = read(DATA / 'archive' / f'slate_{day}.json')
    snap = read(DATA / 'history' / f'{day}.json')
    main = next((s for s in own.get('slates', []) if s['type'] == 'classic' and s['name'] == 'Main'), None)
    if not main or not slate.get('players') or not snap.get('players'):
        return None
    players = []
    for sp in slate['players']:
        k = norm(sp['name']) + '|' + canon(sp['team'])
        s = snap['players'].get(k)
        if not s or s.get('out') or s.get('proj', 0) <= 0 or sp.get('status') in ('OUT', 'DOUBTFUL'):
            continue
        players.append({'k': k, 'name': sp['name'], 'team': canon(sp['team']), 'position': sp['position'], 'salary': sp['salary'],
                        'proj': s['proj'], 'ext': sp.get('ext_proj'), 'own_heur': s.get('own') or 0.0})
    actual = {norm(p['name']) + '|' + canon(p['team']): p for p in main['players'] if p.get('actual_own') is not None}
    cons = {k: {'consensus_own': a.get('linestar_proj_own'), 'mentions': 0} for k, a in actual.items() if a.get('linestar_proj_own') is not None}
    arch = read(DATA / 'consensus' / f'{day}.json')
    for c in arch.get('players', []):
        k = norm(c['name']) + '|' + canon(c['team'])
        cur = cons.setdefault(k, {'consensus_own': None, 'mentions': 0})
        cur['mentions'] = c.get('mentions', 0)
        if c.get('fta_own') is not None:
            vals = [v for v in (cur['consensus_own'], c['fta_own']) if v is not None]
            cur['consensus_own'] = sum(vals) / len(vals)
    if len(players) < 30 or not actual:
        return None
    return {'day': day, 'players': players, 'actual': actual, 'cons': cons}


def predictions(d, noise, cons, mix, power, cache, ext=0.0, ment=0.0):
    key = (d['day'], noise, cons)
    if key not in cache:
        cache[key] = FS.field_ownership(d['players'], 'classic', 60000, {'field_noise': noise, 'consensus_w': cons}, sims=SIMS)
    sim = cache[key]
    own = chalk(FS.blend_ownership(d['players'], sim, {'field_mix': mix}, 900), power, 900)
    own = apply_consensus(own, d['players'], d.get('cons', {}), {'external_w': ext, 'mention_w': ment}, 900)
    return {p['k']: own.get(id(p), 0.0) for p in d['players']}


def errors(days, params, cache):
    errs = []
    for d in days:
        pred = predictions(d, params['field_noise'], params['consensus_w'], params['field_mix'], params['chalk_power'], cache,
                           params.get('external_w', 0.0), params.get('mention_w', 0.0))
        for k, a in d['actual'].items():
            if k in pred:
                errs.append(abs(pred[k] - a['actual_own']))
    return statistics.mean(errs) if errs else None


def fit(days, cache):
    best, best_e = None, 1e9
    for noise in NOISE:
        for cons in CONS:
            for mix in MIX:
                for power in POWER:
                    for ext in EXT:
                        for ment in MENTION:
                            p = {'field_noise': noise, 'consensus_w': cons, 'field_mix': mix, 'chalk_power': power,
                                 'external_w': ext, 'mention_w': ment}
                            e = errors(days, p, cache)
                            if e is not None and e < best_e:
                                best, best_e = p, e
    return best, best_e


def main():
    global MENTION
    if len(list((DATA / 'consensus').glob('*.json'))) >= 3 if (DATA / 'consensus').exists() else False:
        MENTION = [0.0, 0.1, 0.2]
    prev = read(OUT)
    files = sorted(p.stem for p in (DATA / 'ownership').glob('*.json')) if (DATA / 'ownership').exists() else []
    if prev.get('days_used') == files and '--force' not in sys.argv:
        print('No new ownership results; keeping settings')
        return
    days = [d for d in (day_data(x) for x in files) if d]
    cur = {**FS.DEFAULT_OWN, 'chalk_power': 1.0, 'external_w': 0.0, 'mention_w': 0.0, **(prev.get('params') or {})}
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    if len(days) < 2:
        OUT.write_text(json.dumps({**prev, 'params': cur, 'updated_at': now, 'days_used': files,
                                   'decision': f'Waiting for more results ({len(days)} usable day(s)).'}, indent=1) + '\n')
        print('Waiting for more ownership results')
        return
    cache = {}
    train, test = days[:-1], days[-1:]
    fitted, train_e = fit(train, cache)
    e_cur, e_fit = errors(test, cur, cache), errors(test, fitted, cache)
    ls = [abs(a['linestar_proj_own'] - a['actual_own']) for d in test for a in d['actual'].values() if a.get('linestar_proj_own') is not None]
    adopted = e_fit < e_cur - 0.2
    new = fitted
    if adopted:
        new, all_e = fit(days, cache)      # refit on every day once validated
    else:
        new = cur
    all_err = errors(days, new, cache)
    n_players = sum(len(d['actual']) for d in days)
    decision = (f"Adopted: on {test[0]['day']} (held out) ownership missed by {e_fit:.1f} points vs {e_cur:.1f} before."
                if adopted else f"Kept current settings: refit didn't beat them on {test[0]['day']} ({e_fit:.1f} vs {e_cur:.1f}).")
    out = {'schema': 1, 'updated_at': now, 'params': new, 'decision': decision, 'days_used': files,
           'evaluation': {'days': len(days), 'players': n_players, 'avg_miss_all_days': round(all_err, 2),
                          'heldout_day': test[0]['day'], 'heldout_current': round(e_cur, 2), 'heldout_fitted': round(e_fit, 2),
                          'linestar_heldout': round(statistics.mean(ls), 2) if ls else None},
           'log': (prev.get('log') or [])[-20:] + [{'date': now[:10], 'adopted': adopted, 'params': new}],
           'method': 'Simulated optimizer field on consensus projections + feel-based share; grid search graded against actual FanDuel ownership of named players.'}
    OUT.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    print(decision)
    print(json.dumps(out['evaluation'], indent=1), new)


if __name__ == '__main__':
    main()
