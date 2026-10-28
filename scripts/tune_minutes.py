#!/usr/bin/env python3
"""NBA EDGE: learn the minutes/rate rule settings -> data/model_params.json.

Replays the 2025-26 season: Oct 21-Dec 31 plays the role of "last season", and each
target day from January to April is predicted using only games before it (the same
rules the live model uses). A coordinate search finds settings that minimize the
average fantasy-point miss.

Guardrails:
  * The last quarter of target days is held out; settings must beat the current ones there.
  * A second check on this season's real days (prior = full 2025-26 season) must not get worse.
  * Rows where a player had just missed 2+ games are skipped (no historical injury
    reports, so those rows can't fairly judge the DNP rules, which stay fixed).
Runs weekly (Mondays) in the pipeline, or with --force.
"""
import datetime as dt
import json
import pathlib
import statistics
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import build_automatic_projections as B  # noqa: E402

DATA = ROOT / 'data'
OUT = DATA / 'model_params.json'
BOUNDS = {'role_ratio': (0.35, 0.75), 'role_bump': (0, 6), 'short_ratio': (0.3, 0.7), 'decline_ratio': (0.5, 0.9),
          'decline_w': (0.3, 1.0), 'prior_k': (0.5, 10), 'blend_cap': (0.6, 0.98), 'recent_w': (0, 0.8),
          'rate_k': (3, 40), 'rate_cap': (0.3, 0.95), 'shrink_games': (0, 15), 'blowout_game_w': (0, 1),
          'b2b_mult': (0.85, 1.05), 'three_in_four_mult': (0.85, 1.05)}
STEP = {k: (hi - lo) / 8 for k, (lo, hi) in BOUNDS.items()}


def d(s):
    return dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def rows_for(raw, prior, floor, days, injured_by_day=None):
    """One row per player who played on each target day: (day, old, cur, series, fp, min)."""
    rows = []
    actual = {}
    for r in raw:
        actual.setdefault(str(r['date']), []).append(r)
    for day in days:
        target = d(day)
        cur_logs = B.log_index(raw, floor, target)
        current = {k: B.summary(v) for k, v in cur_logs.items()}
        sched = B.team_schedule(raw, floor, target)
        inj = (injured_by_day or {}).get(day, {})
        for r in actual.get(day, []):
            try:
                mins, fp = float(r.get('minutes') or 0), float(r.get('fantasy_points') or 0)
            except (TypeError, ValueError):
                continue
            if mins <= 0:
                continue
            key, team = B.norm(r.get('name')), B.canon_team(r.get('team'))
            old, cur = prior.get(key), current.get(key)
            if not old and not cur:
                continue
            series = B.minute_series(cur_logs.get(key), sched.get(team, []))
            if len(series) - len(B.dropwhile_zero(series)) >= 2:
                continue
            b2b, t34, _ = B.rest_flags(sched.get(team, []), target)
            rows.append((day, old, cur, series, inj.get(key), fp, mins, b2b, t34))
    return rows


def evaluate(rows, params):
    B.P.update(params)
    e_fp, e_min = [], []
    for _, old, cur, series, inj, fp, mins, b2b, t34 in rows:
        m = B.expected_minutes(old, cur, series, inj)[0]
        if m is None:
            continue
        m *= B.rest_mult(b2b, t34)
        rate = B.rate_for(old if old and old['games'] >= 3 else None, cur)[0]
        e_fp.append(abs(fp - m * rate))
        e_min.append(abs(mins - m))
    return statistics.mean(e_fp), statistics.mean(e_min)


def search(rows, start):
    best = dict(start)
    score = evaluate(rows, best)[0]
    for scale in (1.0, 0.5, 0.25):
        improved = True
        while improved:
            improved = False
            for k, (lo, hi) in BOUNDS.items():
                for sign in (1, -1):
                    trial = dict(best)
                    trial[k] = round(min(hi, max(lo, best[k] + sign * STEP[k] * scale)), 4)
                    if trial[k] == best[k]:
                        continue
                    val = evaluate(rows, trial)[0]
                    if val < score - 1e-4:
                        best, score, improved = trial, val, True
    return best


def main():
    prev = json.loads(OUT.read_text(encoding='utf-8')) if OUT.exists() else {}
    today = dt.date.today()
    if prev.get('updated_at') and '--force' not in sys.argv:
        last = dt.date.fromisoformat(prev['updated_at'][:10])
        if (today - last).days < 7:
            print('Rule settings tuned', (today - last).days, 'days ago; weekly run keeps them')
            return
    t0 = time.time()
    last_season = json.loads((DATA / 'last_season_stats.json').read_text(encoding='utf-8'))
    raw = last_season.get('game_logs', [])
    B.attach_margins(raw)
    prior = {k: B.summary(v) for k, v in B.log_index(raw, dt.date(2025, 10, 21), dt.date(2026, 1, 1)).items()}
    prior = {k: v for k, v in prior.items() if v and v['games'] >= 3}
    all_days = sorted({str(r['date']) for r in raw if str(r['date']) >= '20260106'})
    days = all_days[::2]
    rows = rows_for(raw, prior, dt.date(2026, 1, 1), days)
    cut = days[int(len(days) * 0.75)]
    train = [r for r in rows if r[0] < cut]
    test = [r for r in rows if r[0] >= cut]

    # Real check: this season, prior = full 2025-26, injury tags from archived slates.
    season = B.season_logs()
    B.attach_margins(season)
    prior_full = B.prior_index(last_season)
    inj_by_day = {}
    for f in sorted((DATA / 'archive').glob('slate_*.json')):
        doc = json.loads(f.read_text(encoding='utf-8'))
        inj_by_day[doc['date'].replace('-', '')] = {B.norm(p['name']): p['status'] for p in doc['players'] if p.get('status') in ('OUT', 'DOUBTFUL', 'QUESTIONABLE')}
    real_days = sorted({str(r['date']) for r in season if str(r['date']) >= '20261021'})
    real = rows_for(season, prior_full, dt.date(2026, 10, 20), real_days, inj_by_day)

    current = {**B.DEFAULT_PARAMS, **(prev.get('params') or {})}
    defaults = dict(B.DEFAULT_PARAMS)
    tuned = search(train, current)
    res = {name: {'test_fp': round(evaluate(test, p)[0], 3), 'test_min': round(evaluate(test, p)[1], 3),
                  'real_fp': round(evaluate(real, p)[0], 3) if real else None,
                  'real_min': round(evaluate(real, p)[1], 3) if real else None}
           for name, p in (('default', defaults), ('current', current), ('tuned', tuned))}
    ok_test = res['tuned']['test_fp'] < res['current']['test_fp'] - 0.03
    ok_real = not real or res['tuned']['real_fp'] <= res['current']['real_fp'] + 0.05
    adopted = ok_test and ok_real
    new = tuned if adopted else current
    changes = [f'{k} {current[k]:g} → {new[k]:g}' for k in BOUNDS if abs(new[k] - current[k]) > 1e-9]
    if adopted:
        decision = (f"Adopted: held-out days {res['current']['test_fp']:.2f} → {res['tuned']['test_fp']:.2f} FP miss"
                    + (f"; this season {res['current']['real_fp']:.2f} → {res['tuned']['real_fp']:.2f}" if real else '') + '.')
    else:
        decision = ('Kept current settings: ' + ('no clear gain on held-out days.' if not ok_test else 'worse on this season’s real days.'))
    log = (prev.get('log') or [])[-20:] + [{'date': today.isoformat(), 'adopted': adopted, 'changes': changes}]
    out = {'schema': 1, 'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
           'params': new, 'decision': decision, 'results': res,
           'rows': {'train': len(train), 'test': len(test), 'real': len(real)}, 'log': log,
           'method': 'Replay of 2025-26 (Oct-Dec as prior, Jan-Apr predicted from earlier games only); coordinate search; adopted only if better on the held-out last quarter and not worse on this season.'}
    OUT.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    print(decision, f'({len(train)} train / {len(test)} test / {len(real)} real rows, {time.time() - t0:.0f}s)')
    for c in changes:
        print(' ', c)
    print(json.dumps(res, indent=1))


if __name__ == '__main__':
    main()
