#!/usr/bin/env python3
"""NBA EDGE v27: final slate projections -> data/today.json (what the website reads).

Layers on top of the base model in automatic_projections.json (rotation-aware
minutes x sample-shrunk FP/min):
  1. Availability: OUT/DOUBTFUL removed (DFF tag + ESPN injury feed).
  2. Injury minutes: a NEW absence (player played his team's last game) frees his
     minutes. Teammates get them in proportion to how their minutes actually changed
     in past games without him (on/off from box scores); position-based fallback.
  3. Confirmed starters (RotoWire) with a small projected role get a modest bump.
  4. Team minutes capped near 240.
  5. Vegas: implied team total vs slate average (scoring environment); big spreads
     trim starters' minutes (blowout risk).
  6. Matchup: opponent fantasy points allowed by position (DvP), shrunk.
  7. Floor/ceiling and an ownership ESTIMATE (heuristic, not observed ownership).
Every adjustment is bounded and recorded per player so the site can explain it.
All weights are heuristics; data/accuracy.json grades the result nightly.
"""
import datetime as dt
import json
import math
import pathlib
import re
import statistics
import unicodedata
from collections import defaultdict
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
ALIASES = {'GS': 'GSW', 'NO': 'NOP', 'NY': 'NYK', 'PHO': 'PHX', 'SA': 'SAS', 'UTAH': 'UTA', 'WSH': 'WAS', 'BRK': 'BKN'}
POS_GROUP = {'PG': 'G', 'SG': 'G', 'SF': 'F', 'PF': 'F', 'C': 'C'}


def canon(t):
    t = str(t or '').strip().upper()
    return ALIASES.get(t, t)


def norm(s):
    return re.sub(r'[^a-z0-9]', '', unicodedata.normalize('NFKD', str(s or '')).lower())


def read(name, default=None):
    p = DATA / name
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {} if default is None else default


def clip(x, lo, hi):
    return max(lo, min(hi, x))


# ---------- history helpers ----------
def team_games(logs):
    """team -> list of (date, {player_key: minutes}) sorted by date, one entry per game."""
    out = defaultdict(dict)
    for r in logs:
        try:
            m = float(r.get('minutes') or 0)
        except (TypeError, ValueError):
            continue
        ev = str(r.get('event_id') or r.get('date'))
        g = out[canon(r.get('team'))].setdefault(ev, (str(r.get('date')), {}))
        g[1][norm(r.get('name'))] = m
    return {t: [v[1] for v in sorted(evs.values(), key=lambda x: x[0])] for t, evs in out.items()}


def on_off_shares(games, absent_key, teammates):
    """Average minute change for each teammate in games WITHOUT absent_key vs WITH him,
    restricted to his tenure on the team (first..last appearance) and to games the
    teammate actually played in both samples. None if the sample is too small."""
    idx = [i for i, g in enumerate(games) if g.get(absent_key, 0) > 0]
    if len(idx) < 3:
        return None
    span = games[idx[0]:]
    with_g = [g for g in span if g.get(absent_key, 0) > 0]
    without = [g for g in span if g.get(absent_key, 0) == 0]
    if len(without) < 2:
        return None
    deltas = {}
    for t in teammates:
        a = [g[t] for g in without if g.get(t, 0) > 0]
        b = [g[t] for g in with_g if g.get(t, 0) > 0]
        if len(a) >= 2 and len(b) >= 2:
            deltas[t] = statistics.mean(a) - statistics.mean(b)
    return deltas


def naive_fppg(prior_players, logs):
    """Simple-average baseline used by the accuracy report: mean FP of all observed games."""
    tot = defaultdict(lambda: [0.0, 0])
    for p in prior_players:
        g = int(p.get('games') or 0)
        if g and p.get('avg_fantasy_points') is not None:
            tot[norm(p['name'])][0] += float(p['avg_fantasy_points']) * g
            tot[norm(p['name'])][1] += g
    for r in logs:
        try:
            if float(r.get('minutes') or 0) > 0:
                k = norm(r.get('name'))
                tot[k][0] += float(r.get('fantasy_points') or 0)
                tot[k][1] += 1
        except (TypeError, ValueError):
            pass
    return {k: v[0] / v[1] for k, v in tot.items() if v[1]}


def starter_match(name, lineup_names):
    """RotoWire lists 'D. Mitchell' style names."""
    full = norm(name)
    parts = str(name).replace('.', '').split()
    for n in lineup_names:
        if norm(n) == full:
            return True
        bits = str(n).replace('.', '').split()
        if len(bits) >= 2 and len(parts) >= 2 and norm(bits[-1]) == norm(parts[-1]) and bits[0][:1].lower() == parts[0][:1].lower():
            return True
    return False


# ---------- main build ----------
def build(day=None):
    day = day or dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat()
    slate = read(f'slate_{day}.json')
    if not slate.get('players'):
        print('No slate JSON for', day)
        return None
    base = read('automatic_projections.json')
    base_rows = base.get('players', []) if base.get('slate_date') == day else []
    by_team = {(norm(r['name']), canon(r['team'])): r for r in base_rows}
    by_name = defaultdict(list)
    for r in base_rows:
        by_name[norm(r['name'])].append(r)

    inj = {}
    for r in read('injuries.json').get('injuries', []):
        inj[(norm(r.get('name')), canon(r.get('team')))] = str(r.get('status') or '').upper()
    lineups = {canon(l.get('team')): l for l in read('lineup_news.json').get('lineups', [])
               if l.get('date') == day and l.get('status') == 'confirmed'}
    mu = read('matchup_usage.json')
    dvp = {(canon(d['opponent']), d['position']): d for d in mu.get('dvp', []) if d.get('league_avg')}

    last = read('last_season_stats.json')
    cur = read('player_stats.json')
    logs_cur = cur.get('game_logs', [])
    logs_all = last.get('game_logs', []) + logs_cur
    tg_all = team_games(logs_all)
    naive = naive_fppg(last.get('players', []), logs_cur)

    games = slate.get('games', [])
    totals = {}
    for g in games:
        if g.get('total') and g.get('home_spread') is not None:
            t, s = g['total'], g['home_spread']
            totals[g['home']] = (t / 2 - s / 2, s)
            totals[g['away']] = (t / 2 + s / 2, -s)
    avg_tt = statistics.mean(v[0] for v in totals.values()) if totals else None

    players = []
    for sp in slate['players']:
        key, team = norm(sp['name']), canon(sp['team'])
        row = by_team.get((key, team))
        moved = False
        if row is None and len(by_name.get(key, [])) == 1:
            row, moved = by_name[key][0], True
        p = {'name': sp['name'], 'team': team, 'opp': canon(sp.get('opp')), 'home': sp.get('home'),
             'position': sp['position'], 'salary': sp['salary'], 'status': sp.get('status', 'ACTIVE'),
             'flags': [], 'adj': {}, 'minutes': 0.0, 'rate': 0.0, 'proj': 0.0,
             'naive': round(naive.get(key, 0.0), 2)}
        espn = inj.get((key, team))
        if espn in ('OUT', 'INACTIVE', 'DOUBTFUL'):
            p['status'] = espn if espn != 'INACTIVE' else 'OUT'
        elif espn in ('QUESTIONABLE', 'DAY-TO-DAY', 'GTD') and p['status'] == 'ACTIVE':
            p['status'] = 'QUESTIONABLE'
        if row is None or not row.get('expected_minutes'):
            p['flags'].append('No NBA game history yet; not projected')
            p['_recent'] = []
        else:
            m = float(row['team_change_minutes'] if moved else row['expected_minutes'])
            p['minutes'], p['rate'] = m, float(row['fp_per_minute'])
            p['method'] = 'team_change' if moved else row.get('minutes_method')
            p['_recent'] = row.get('team_recent_minutes') or []
            if moved:
                p['flags'].append(f"New team ({row['team']} → {team}); role unverified")
            for w in row.get('warnings', []):
                if w.startswith(('ROLE DROP', 'DNP in last', 'Small sample', 'One extremely short', 'Lower current')):
                    p['flags'].append(w)
        if p['status'] in ('OUT', 'DOUBTFUL'):
            p['out'] = True
        players.append(p)

    # 2. injury redistribution (new absences only)
    by_t = defaultdict(list)
    for p in players:
        by_t[p['team']].append(p)
    for team, roster in by_t.items():
        new_out = [p for p in roster if p.get('out') and p['minutes'] >= 10 and p['_recent'] and p['_recent'][-1] > 0]
        avail = [p for p in roster if not p.get('out') and p['minutes'] > 0]
        for o in new_out:
            freed = o['minutes'] * 0.9
            hist = on_off_shares(tg_all.get(team, {}), norm(o['name']), [norm(p['name']) for p in avail])
            gains = {}
            if hist:
                pos = {k: v for k, v in hist.items() if v > 0.3}
                tot = sum(pos.values())
                if tot > 0:
                    scale = min(1.0, freed / tot)
                    gains = {k: v * scale for k, v in pos.items()}
                    method = 'on/off history'
            if not gains:
                grp = {POS_GROUP.get(x) for x in o['position'].split('/')}
                cands = [p for p in avail if grp & {POS_GROUP.get(x) for x in p['position'].split('/')}] or avail
                w = sum(p['minutes'] for p in cands) or 1
                gains = {norm(p['name']): freed * p['minutes'] / w for p in cands}
                method = 'position fallback'
            for p in avail:
                room = 10.0 - p['adj'].get('injury_min', 0)  # total injury gain capped at 10 min
                g = min(gains.get(norm(p['name']), 0.0), 8.0, room, max(0.0, 40 - p['minutes']))
                if g >= 0.5:
                    p['minutes'] += g
                    p['adj']['injury_min'] = round(p['adj'].get('injury_min', 0) + g, 1)
                    p['flags'].append(f"+{g:.1f} min with {o['name']} out ({method})")
            # usage bump: missing high-volume players raise teammates' per-minute output
            o_fp = o['minutes'] * o['rate']
            if o_fp >= 20:
                for p in avail:
                    p['adj']['usage'] = round(min(1.06, p['adj'].get('usage', 1.0) * (1 + 0.0015 * o_fp)), 3)

        # 3. confirmed starters
        lu = lineups.get(team)
        if lu:
            for p in avail:
                if starter_match(p['name'], lu.get('players', [])):
                    p['starter'] = True
                    if p['minutes'] < 24:
                        bump = (28 - p['minutes']) * 0.5
                        p['minutes'] += bump
                        p['adj']['starter_min'] = round(bump, 1)
                        p['flags'].append(f'Confirmed starter (+{bump:.1f} min)')

        # 4. team minutes sanity
        tot = sum(p['minutes'] for p in avail)
        if tot > 245:
            f = 240 / tot
            for p in avail:
                p['minutes'] *= f
            for p in avail:
                p['adj']['team_cap'] = round(f, 3)

    # 5-6. Vegas + matchup, then final numbers
    for p in players:
        if p.get('out') or p['minutes'] <= 0:
            p['proj'] = 0.0
            continue
        vegas = 1.0
        if avg_tt and p['team'] in totals:
            tt, spread = totals[p['team']]
            p['team_total'] = round(tt, 1)
            p['spread'] = spread
            vegas = clip(1 + 0.6 * (tt / avg_tt - 1), 0.92, 1.08)
            if abs(spread) > 9 and p['minutes'] >= 28:
                trim = min(0.06, 0.01 * (abs(spread) - 9))
                p['minutes'] *= 1 - trim
                p['adj']['blowout'] = round(-trim, 3)
        p['adj']['vegas'] = round(vegas, 3)
        ratios = []
        for pos in p['position'].split('/'):
            d = dvp.get((p['opp'], pos))
            if d and d.get('games', 0) >= 10:
                ratios.append(d['fp_allowed'] / d['league_avg'])
        matchup = clip(1 + 0.5 * (statistics.mean(ratios) - 1), 0.94, 1.06) if ratios else 1.0
        p['adj']['matchup'] = round(matchup, 3)
        env = clip(vegas * matchup * p['adj'].get('usage', 1.0), 0.88, 1.14)
        p['proj'] = round(p['minutes'] * p['rate'] * env, 2)
        p['minutes'] = round(p['minutes'], 1)
        # 7. range: FP spread grows with projection; extra for shaky roles
        shaky = any(f.startswith(('ROLE DROP', 'New team', 'Small sample', 'No NBA')) for f in p['flags']) or p['status'] == 'QUESTIONABLE'
        sd = 0.28 * p['proj'] + 3 + (3 if shaky else 0)
        p['floor'] = round(max(0, p['proj'] - 1.28 * sd), 1)
        p['ceiling'] = round(p['proj'] + 1.28 * sd, 1)
        p['value'] = round(p['proj'] * 1000 / p['salary'], 2)

    # ownership estimate: value + raw points + game environment + news, normalized to
    # 900% (9 roster spots) with a 60% cap; excess is redistributed.
    elig = [p for p in players if p['proj'] > 0]
    if elig:
        ws = []
        for p in elig:
            tt_term = ((p.get('team_total') or avg_tt or 112) - (avg_tt or 112)) * 0.03
            news = 0.35 if p['adj'].get('injury_min', 0) >= 4 else 0
            ws.append(math.exp(0.75 * (p['value'] - 5) + 0.06 * (p['proj'] - 25) + tt_term + news))
        own = [900 * w / sum(ws) for w in ws]
        for _ in range(20):
            over = sum(max(0, o - 60) for o in own)
            if over < 0.01:
                break
            free = [i for i, o in enumerate(own) if o < 60]
            fw = sum(own[i] for i in free)
            own = [min(o, 60) for o in own]
            for i in free:
                own[i] += over * own[i] / fw
        for p, o in zip(elig, own):
            p['own'] = round(min(60.0, o), 1)

    for p in players:
        p.pop('_recent', None)
    out = {'schema': 1, 'model': 'v27', 'date': day, 'slate': slate.get('slate', 'Main'),
           'built_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
           'avg_team_total': round(avg_tt, 1) if avg_tt else None, 'games': games,
           'sources': {'slate': slate.get('source'), 'injuries_updated': read('injuries.json').get('updated_at'),
                       'stats_updated': cur.get('updated_at'), 'lineups_checked': read('lineup_news.json').get('checked_at')},
           'players': sorted(players, key=lambda p: -p['proj'])}
    return out


def main():
    out = build()
    if not out:
        return
    path = DATA / 'today.json'
    old = read('today.json')
    strip = lambda d: {k: v for k, v in d.items() if k not in ('built_at', 'sources')}
    if old and strip(old) == strip(out):
        print('today.json unchanged')
        return
    path.write_text(json.dumps(out, separators=(',', ':')) + '\n', encoding='utf-8')
    n = sum(p['proj'] > 0 for p in out['players'])
    print(f"today.json: {out['date']} {n} projected players, {len(out['games'])} games")


if __name__ == '__main__':
    main()
