#!/usr/bin/env python3
"""NBA EDGE · DraftKings (paper mode): DK projections and lineups for today's DK main slate.

Projections reuse the FanDuel model's final player projection (minutes, rate, Vegas, injuries,
matchup — everything already learned) and convert it to DraftKings scoring with each player's
own DK/FD points ratio from this season's box scores, shrunk toward the league ratio. A player
who shoots lots of threes or piles up double-doubles gains on DK; a steals/blocks specialist loses.

Builds 1 cash lineup (max projection) and 3 tournament lineups (projection + ceiling, each one
3+ players different). Paper only: written to data/dk/lineups_YYYY-MM-DD.json and graded later by
dk_grade.py. Stops rebuilding once the slate locks so grades only use pre-lock information.
"""
import datetime as dt
import json
import sys
from zoneinfo import ZoneInfo

from build_slate import canon, norm
from dk_common import DATA, DK, SLOTS, load, load_logs, optimize, ratio_model

GPP_CEIL_W = 0.5
GPP_COUNT = 3


def lock_time(day, teams):
    """Earliest tip among today's games involving DK-slate teams (UTC), from the FanDuel slate file."""
    fd = load(DATA / f'slate_{day}.json', {}) or {}
    times = [g['time'] for g in fd.get('games', []) if g.get('time') and (g.get('home') in teams or g.get('away') in teams)]
    if not times:
        return None
    return min(dt.datetime.fromisoformat(t.replace('Z', '+00:00')) for t in times)


def fd_projections(day):
    """{name_key: (fd_proj, minutes, out, shaky, source)} — final model projection where it exists
    (FanDuel main-slate players), otherwise the all-players base projection."""
    out = {}
    auto = load(DATA / 'automatic_projections.json', {}) or {}
    if auto.get('slate_date') == day:
        for p in auto.get('players', []):
            st = (p.get('injury_status') or '').upper()
            out[norm(p['name'])] = {'fd': p.get('base_projected_fp') or 0, 'min': p.get('expected_minutes') or 0,
                                    'out': st == 'OUT', 'shaky': st in ('QUESTIONABLE', 'DOUBTFUL', 'GTD'),
                                    'src': 'base'}
    hist = load(DATA / 'history' / f'{day}.json', {}) or {}
    for s in (hist.get('players') or {}).values():
        out[norm(s['name'])] = {'fd': s.get('proj') or 0, 'min': s.get('minutes') or 0, 'out': bool(s.get('out')),
                                'shaky': bool(s.get('shaky')), 'src': 'model'}
    return out


def main():
    et = dt.datetime.now(ZoneInfo('America/New_York'))
    day = sys.argv[1] if len(sys.argv) > 1 else et.date().isoformat()
    slate = load(DK / f'slate_{day}.json')
    if not slate:
        print(f'No DK slate for {day}; nothing to build')
        return
    out_path = DK / f'lineups_{day}.json'
    prev = load(out_path, {}) or {}
    lock = lock_time(day, set(slate['teams']))
    now = dt.datetime.now(dt.timezone.utc)
    if prev.get('locked') or (lock and now >= lock and prev):
        print(f'DK slate {day} has locked; keeping the pre-lock lineups')
        if not prev.get('locked'):
            prev['locked'] = True
            out_path.write_text(json.dumps(prev, indent=1) + '\n', encoding='utf-8')
        return

    logs = load_logs()
    league, ratios, used = ratio_model(logs, lambda r: norm(r.get('name')))
    fd = fd_projections(day)

    pool, missing = [], []
    for p in slate['players']:
        if p['status'] == 'OUT':
            continue
        f = fd.get(norm(p['name']))
        if not f or f['out'] or f['fd'] <= 0:
            if not f:
                missing.append(p['name'])
            continue
        r = ratios.get(norm(p['name']), league)
        proj = round(f['fd'] * r, 2)
        shaky = f['shaky'] or p['status'] in ('QUESTIONABLE', 'DOUBTFUL')
        sd = round(0.28 * proj + 3 + (3 if shaky else 0), 2)
        pool.append({'name': p['name'], 'team': canon(p['team']), 'opp': canon(p.get('opp')),
                     'game': '@'.join(sorted((canon(p['team']), canon(p.get('opp'))))),
                     'position': p['position'], 'salary': p['salary'], 'status': p['status'],
                     'fd_proj': round(f['fd'], 2), 'ratio': round(r, 3), 'proj': proj, 'sd': sd,
                     'minutes': f['min'], 'source': f['src'], 'dff_proj': p.get('dff_proj')})
    if len(pool) < 20:
        print(f'::warning::Only {len(pool)} DK players with projections; not building lineups')
        return

    def lineup(picks, strategy):
        rows = sorted(((pool[i], s) for i, s in picks), key=lambda x: SLOTS.index(x[1]))
        return {'strategy': strategy, 'proj': round(sum(p['proj'] for p, _ in rows), 2),
                'salary': sum(p['salary'] for p, _ in rows),
                'players': [{'slot': s, 'name': p['name'], 'team': p['team'], 'salary': p['salary'], 'proj': p['proj']} for p, s in rows]}

    lineups = []
    cash = optimize(pool, lambda p: p['proj'])
    taken = []
    if cash:
        lineups.append(lineup(cash, 'cash'))
        taken.append({i for i, _ in cash})   # tournament lineups must differ from the cash one too
    for _ in range(GPP_COUNT):
        picks = optimize(pool, lambda p: p['proj'] + GPP_CEIL_W * p['sd'], exclude_sets=taken)
        if not picks:
            break
        taken.append({i for i, _ in picks})
        lineups.append(lineup(picks, 'tournament'))

    late = bool(lock and now >= lock)   # first build came after tip-off: graded but kept out of the summary
    doc = {'schema': 1, 'site': 'draftkings', 'mode': 'paper', 'date': day,
           'built_at': now.isoformat(timespec='seconds'), 'lock': lock.isoformat() if lock else None,
           'locked': late, 'late_build': late,
           'ratio_model': {'league_dk_per_fd': round(league, 4), 'box_scores_used': used,
                           'note': 'DK/FD ratio defaults to 1.0 until 200+ box scores include threes made' if used < 200 else ''},
           'players_unmatched': missing[:60], 'lineups': lineups,
           'pool': sorted(pool, key=lambda p: -p['proj'])}
    out_path.write_text(json.dumps(doc, indent=1) + '\n', encoding='utf-8')
    print(f'DK {day}: {len(pool)} players (league DK/FD {league:.3f} from {used} box scores), '
          f'{len(lineups)} lineups' + (f'; cash proj {lineups[0]["proj"]}' if lineups else ''))


if __name__ == '__main__':
    main()
