#!/usr/bin/env python3
"""Live grade of tonight: NBA EDGE's frozen lineups and projections against ESPN box scores,
finished and in-progress games alike -> data/live_grade.json.

The real grading (grade_projections / grade_lineups) waits until the next morning; this answers
"how are we doing tonight" while games are on. Run on demand (workflow "NBA EDGE Live Grade")
and late each night.
"""
import datetime as dt
import json
import pathlib
import statistics
import sys
import time
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
sys.path.insert(0, str(ROOT / 'scripts'))
from update_player_stats import BASE, fetch_json, parse_summary  # noqa: E402
from grade_projections import canon, norm  # noqa: E402


def read(p, default=None):
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {} if default is None else default


def games(day):
    out = {}
    for ev in fetch_json(BASE + 'scoreboard?dates=' + day.replace('-', '')).get('events', []):
        st = ev.get('status', {}).get('type', {})
        comp = (ev.get('competitions') or [{}])[0]
        teams = [canon(c.get('team', {}).get('abbreviation')) for c in comp.get('competitors', [])]
        state = 'final' if st.get('completed') else ('live' if st.get('state') == 'in' else 'pre')
        out[ev['id']] = {'teams': teams, 'state': state, 'detail': st.get('shortDetail') or st.get('detail'),
                         'score': {canon(c.get('team', {}).get('abbreviation')): c.get('score') for c in comp.get('competitors', [])}}
    return out


def main():
    today = read(DATA / 'today.json')
    day = today.get('date') or dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat()
    G = games(day)
    team_state, box = {}, {}
    for gid, g in G.items():
        for t in g['teams']:
            team_state[t] = g['state']
        if g['state'] == 'pre':
            continue
        try:
            for r in parse_summary(fetch_json(BASE + 'summary?event=' + gid), gid, day):
                box[norm(r['name']) + '|' + canon(r['team'])] = r
        except Exception as e:  # one bad game shouldn't sink the grade
            print(f'::warning::{gid}: {e}')
        time.sleep(0.15)

    def actual(name, team):
        r = box.get(norm(name) + '|' + canon(team))
        st = team_state.get(canon(team), 'pre')
        if r:
            return r['fantasy_points'], st, r
        return (0.0 if st == 'final' else None), st, None

    slates = {}
    for key, s in (read(DATA / 'lineups' / f'{day}.json').get('slates') or {}).items():
        rows = []
        for lu in s.get('lineups', []):
            pts, done, players = 0.0, 0, []
            for p in lu['players']:
                a, st, r = actual(p['name'], p['team'])
                m = p.get('mult') or 1
                if a is not None:
                    pts += a * m
                done += st == 'final'
                players.append({'name': p['name'], 'team': p['team'], 'slot': p['slot'], 'proj': round(p['proj'] * m, 1),
                                'actual': None if a is None else round(a * m, 1), 'state': st,
                                'min': r and r['minutes'], 'own': p.get('own')})
            rows.append({'strategy': lu.get('strategy'), 'proj': lu.get('proj'), 'actual': round(pts, 1),
                         'final_players': done, 'players': players})
        slates[key] = {'label': s.get('label'), 'format': s.get('format'), 'lineups': rows}

    # Projections vs actual for finished and live players (live ones only count once their game is final).
    snap = read(DATA / 'history' / f'{day}.json').get('players') or {}
    graded = []
    for p in snap.values():
        if p.get('out') or not p.get('proj'):
            continue
        a, st, r = actual(p['name'], p['team'])
        if st != 'final' or a is None:
            continue
        graded.append({'name': p['name'], 'team': p['team'], 'salary': p.get('salary'), 'proj': p['proj'],
                       'actual': round(a, 1), 'miss': round(a - p['proj'], 1), 'own': p.get('own'),
                       'min_proj': p.get('minutes'), 'min': r and r['minutes']})
    misses = sorted(graded, key=lambda x: x['miss'])
    proj = None
    if graded:
        proj = {'players': len(graded), 'mae': round(statistics.mean(abs(x['miss']) for x in graded), 2),
                'bias': round(statistics.mean(x['miss'] for x in graded), 2),
                'minutes_bias': round(statistics.mean((x['min'] or 0) - (x['min_proj'] or 0) for x in graded if x['min_proj']), 2),
                'worst_busts': misses[:8], 'best_smashes': misses[::-1][:8]}
    # Best possible scores tonight among the top projected players (for context on what won).
    top = sorted((x for x in graded if (x['salary'] or 0) > 0), key=lambda x: -x['actual'] / x['salary'])[:10]
    out = {'date': day, 'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
           'games': list(G.values()), 'slates': slates, 'projections': proj, 'best_values': top,
           'reference_lines': (read(DATA / 'contest_sims.json').get('lines_median') or {})}
    (DATA / 'live_grade.json').write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    finals = sum(g['state'] == 'final' for g in G.values())
    print(f'live_grade.json: {finals}/{len(G)} games final, {len(graded)} players graded')


if __name__ == '__main__':
    main()
