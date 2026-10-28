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


NEWS_KINDS = [
    ('restriction', re.compile(r'minutes? (restriction|limit|cap)|limited minutes|limited to', re.I)),
    ('out', re.compile(r'ruled out|will(?: not|n.t) play|out (tonight|for|vs)|sidelined|late scratch', re.I)),
    ('doubt', re.compile(r'questionable|doubtful|game-time decision|downgraded', re.I)),
    ('start', re.compile(r'will start|to start|gets (the )?start|starting lineup|named (a )?starter|moves? into (the )?starting', re.I)),
    ('bench', re.compile(r'moved to (the )?bench|benched|won.t start|come off the bench', re.I)),
    ('return', re.compile(r'expected to (play|return)|(to|will) return|cleared to|upgraded|available', re.I)),
]


def attach_news(players, headlines):
    """Match recent headlines to slate players (full name, or a unique last name).
    Headlines are unverified: they become flags to check, never automatic minute changes."""
    last_count = defaultdict(int)
    for p in players:
        last_count[norm(str(p['name']).split()[-1])] += 1
    for h in headlines:
        text = h.get('headline', '')
        t = norm(text)
        kinds = [k for k, rx in NEWS_KINDS if rx.search(text)]
        for p in players:
            full = norm(p['name'])
            last = norm(str(p['name']).split()[-1])
            if full in t:   # full names only: 'Seth Curry' must not match Stephen Curry
                p.setdefault('news', []).append({'headline': text[:200], 'url': h.get('source_url'), 'at': h.get('published_at'), 'kinds': kinds})
    for p in players:
        news = sorted(p.get('news', []), key=lambda n: n.get('at') or '', reverse=True)[:4]
        if not news:
            p.pop('news', None)
            continue
        p['news'] = news
        kinds = {k for n in news for k in n['kinds']}
        if 'restriction' in kinds:
            p['flags'].append('News: possible minutes restriction (unverified headline)')
        if 'bench' in kinds:
            p['flags'].append('News: possible move to the bench (unverified headline)')
        if ('out' in kinds or 'doubt' in kinds) and not p.get('out'):
            p['flags'].append('News: injury or availability headline; check status')


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


# ---------- shared final formula (mirrored in index.html and learn_weights.py) ----------
DEFAULT_WEIGHTS = {'vegas': 1.0, 'matchup': 1.0, 'injury': 1.0, 'usage': 1.0, 'starter': 1.0,
                   'blowout': 1.0, 'scale': 1.0, 'outside': 0.0, 'travel': 0.0, 'minutes_mult': {}}
ARENAS = {'ATL': (33.757, -84.396), 'BOS': (42.366, -71.062), 'BKN': (40.683, -73.975), 'CHA': (35.225, -80.839),
          'CHI': (41.881, -87.674), 'CLE': (41.497, -81.688), 'DAL': (32.790, -96.810), 'DEN': (39.749, -105.008),
          'DET': (42.341, -83.055), 'GSW': (37.768, -122.388), 'HOU': (29.751, -95.362), 'IND': (39.764, -86.155),
          'LAC': (33.945, -118.341), 'LAL': (34.043, -118.267), 'MEM': (35.138, -90.051), 'MIA': (25.781, -80.188),
          'MIL': (43.045, -87.917), 'MIN': (44.979, -93.276), 'NOP': (29.949, -90.082), 'NYK': (40.751, -73.993),
          'OKC': (35.463, -97.515), 'ORL': (28.539, -81.384), 'PHI': (39.901, -75.172), 'PHX': (33.446, -112.071),
          'POR': (45.532, -122.667), 'SAC': (38.580, -121.500), 'SAS': (29.427, -98.437), 'TOR': (43.643, -79.379),
          'UTA': (40.768, -111.901), 'WAS': (38.898, -77.021)}


def km(a, b):
    (la1, lo1), (la2, lo2) = ARENAS[a], ARENAS[b]
    p1, p2 = math.radians(la1), math.radians(la2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def travel_km(day, games):
    """Distance each team travels into tonight: last venue (from saved slates, up to 5 days
    back) to tonight's arena. Unknown last venue -> no entry."""
    tonight = {}
    for g in games:
        tonight[g['home']] = g['home']
        tonight[g['away']] = g['home']
    last = {}
    d0 = dt.date.fromisoformat(day)
    for back in range(1, 6):
        d = (d0 - dt.timedelta(days=back)).isoformat()
        for f in (f'archive/slate_{d}.json', f'archive/single_{d}.json'):
            doc = read(f)
            for g in doc.get('games', []) + doc.get('slates', []):
                for t in (g.get('home'), g.get('away')):
                    if t and t not in last and g.get('home') in ARENAS:
                        last[t] = g['home']
    return {t: round(km(last[t], v)) for t, v in tonight.items() if t in last and t in ARENAS and v in ARENAS}


def learned_weights():
    """Weights fitted nightly by learn_weights.py (falls back to defaults)."""
    w = read('learned_weights.json').get('weights')
    return w if isinstance(w, dict) else {}


def final_projection(p, W):
    """minutes, projection from base minutes + recorded adjustments + weights."""
    a = p.get('adj', {})
    mm = (W.get('minutes_mult') or {}).get(p.get('method') or 'other', 1.0)
    m = (p['base_minutes'] * mm + a.get('injury_min', 0) * W['injury'] + a.get('starter_min', 0) * W['starter'])
    m *= a.get('team_cap', 1.0) * (1 + a.get('blowout', 0) * W['blowout'])
    f = lambda x, w: 1 + (x - 1) * w
    wmax = max(1.0, W['vegas'], W['matchup'], W['usage'])
    env = clip(f(a.get('vegas', 1.0), W['vegas']) * f(a.get('matchup', 1.0), W['matchup']) * f(a.get('usage', 1.0), W['usage']),
               1 - 0.12 * wmax, 1 + 0.14 * wmax)
    proj = m * p['rate'] * env * W['scale']
    proj *= 1 + W.get('travel', 0.0) * a.get('travel_km', 0) / 1000.0   # learned; 0 until it proves useful
    # Blend with the outside (DailyFantasyFuel) projection by a learned weight (0 = ours only).
    b = W.get('outside', 0.0)
    if b and p.get('ext') is not None:
        proj = (1 - b) * proj + b * p['ext']
    return m, proj


# ---------- main build ----------
def build(day=None, backtest=False, weights=None):
    """backtest=True rebuilds a past day using only information available before it:
    archived slate (its own injury tags), box scores before that date, no live feeds."""
    day = day or dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat()
    slate = read(f'archive/slate_{day}.json') if backtest else read(f'slate_{day}.json')
    if not slate.get('players'):
        print('No slate JSON for', day)
        return None
    W = {**DEFAULT_WEIGHTS, **(weights if weights is not None else learned_weights())}
    if backtest:
        import sys
        sys.path.insert(0, str(ROOT / 'scripts'))
        import build_automatic_projections as B
        tags = {norm(p['name']): p['status'] for p in slate['players'] if p.get('status') in ('OUT', 'DOUBTFUL', 'QUESTIONABLE')}
        base = B.build(dt.date.fromisoformat(day), injury_map=tags)
    else:
        base = read('automatic_projections.json')
    base_rows = base.get('players', []) if base.get('slate_date') == day else []
    by_team = {(norm(r['name']), canon(r['team'])): r for r in base_rows}
    by_name = defaultdict(list)
    for r in base_rows:
        by_name[norm(r['name'])].append(r)

    inj = {}
    for r in ([] if backtest else read('injuries.json').get('injuries', [])):
        inj[(norm(r.get('name')), canon(r.get('team')))] = str(r.get('status') or '').upper()
    lineups = {} if backtest else {canon(l.get('team')): l for l in read('lineup_news.json').get('lineups', [])
               if l.get('date') == day and l.get('status') == 'confirmed'}
    mu = read('matchup_usage.json')
    dvp = {(canon(d['opponent']), d['position']): d for d in mu.get('dvp', []) if d.get('league_avg')}

    last = read('last_season_stats.json')
    cur = read('player_stats.json')
    cutoff = day.replace('-', '')
    season = {}
    for r in read('season_logs.json').get('game_logs', []) + cur.get('game_logs', []):
        season[(str(r.get('id') or r.get('name')), str(r.get('event_id') or r.get('date')))] = r
    logs_cur = [r for r in season.values() if str(r.get('date')) < cutoff]  # never peek at the day itself
    logs_all = last.get('game_logs', []) + logs_cur
    tg_all = team_games(logs_all)
    naive = naive_fppg(last.get('players', []), logs_cur)

    games = slate.get('games', [])
    # Single-game slates: project their players too (games not on the main slate get added).
    singles = (read(f'archive/single_{day}.json') if backtest else read(f'single_{day}.json')).get('slates', []) if not backtest else []
    on_main = {(norm(p['name']), canon(p['team'])) for p in slate['players']}
    slate_players = [dict(p, main=True) for p in slate['players']]
    for sg in singles:
        for sp in sg['players']:
            k = (norm(sp['name']), canon(sp['team']))
            if k not in on_main:
                on_main.add(k)
                slate_players.append(dict(sp, main=False))
    main_teams = {g['home'] for g in games} | {g['away'] for g in games}
    totals = {}
    for g in games + [sg for sg in singles if sg['home'] not in main_teams]:
        if g.get('total') and g.get('home_spread') is not None:
            t, s = g['total'], g['home_spread']
            totals[g['home']] = (t / 2 - s / 2, s)
            totals[g['away']] = (t / 2 + s / 2, -s)
    avg_tt = statistics.mean(v[0] for v in totals.values()) if totals else None
    travel = travel_km(day, games + [sg for sg in singles if sg['home'] not in main_teams])

    players = []
    for sp in slate_players:
        key, team = norm(sp['name']), canon(sp['team'])
        row = by_team.get((key, team))
        moved = False
        if row is None and len(by_name.get(key, [])) == 1:
            row, moved = by_name[key][0], True
        p = {'main': sp.get('main', True), 'ext': sp.get('ext_proj'), 'name': sp['name'], 'team': team, 'opp': canon(sp.get('opp')), 'home': sp.get('home'),
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
            p['base_minutes'] = round(m, 2)  # before injury/starter/team/blowout adjustments (site re-weights these)
            p['method'] = 'team_change' if moved else row.get('minutes_method')
            p['_recent'] = row.get('team_recent_minutes') or []
            if moved:
                p['flags'].append(f"New team ({row['team']} → {team}); role unverified")
            for w in row.get('warnings', []):
                if w.startswith(('ROLE DROP', 'DNP in last', 'Small sample', 'One extremely short', 'Lower current', 'Blowout', 'Back-to-back', 'Third game')):
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
        if p.get('out') or p['minutes'] <= 0 or 'base_minutes' not in p:
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
                p['adj']['blowout'] = round(-trim, 3)
        p['adj']['vegas'] = round(vegas, 3)
        if travel.get(p['team']):
            p['adj']['travel_km'] = travel[p['team']]
            if travel[p['team']] >= 2500:
                p['flags'].append(f"Long trip in: about {travel[p['team']]:,} km since last game")
        ratios = []
        for pos in p['position'].split('/'):
            d = dvp.get((p['opp'], pos))
            if d and d.get('games', 0) >= 10:
                ratios.append(d['fp_allowed'] / d['league_avg'])
        matchup = clip(1 + 0.5 * (statistics.mean(ratios) - 1), 0.94, 1.06) if ratios else 1.0
        p['adj']['matchup'] = round(matchup, 3)
        m, proj = final_projection(p, W)
        p['proj'] = round(proj, 2)
        p['minutes'] = round(m, 1)
        own_proj = final_projection(p, {**W, 'outside': 0.0})[1]
        p['model_proj'] = round(own_proj, 2)
        if p.get('ext') is not None and abs(own_proj - p['ext']) >= max(8, 0.3 * max(p['ext'], 1)):
            p['flags'].append(f"Outside projection disagrees: DailyFantasyFuel {p['ext']:.1f} vs NBA EDGE model {own_proj:.1f}")
        # 7. range: FP spread grows with projection; extra for shaky roles
        shaky = any(f.startswith(('ROLE DROP', 'New team', 'Small sample', 'No NBA')) for f in p['flags']) or p['status'] == 'QUESTIONABLE'
        sd = 0.28 * p['proj'] + 3 + (3 if shaky else 0)
        p['shaky'] = shaky
        p['floor'] = round(max(0, p['proj'] - 1.28 * sd), 1)
        p['ceiling'] = round(p['proj'] + 1.28 * sd, 1)
        p['value'] = round(p['proj'] * 1000 / p['salary'], 2)

    # ownership estimate: value + raw points + game environment + news, normalized to
    # 900% (9 roster spots) with a 60% cap; excess is redistributed.
    elig = [p for p in players if p['proj'] > 0 and p['main']]
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
            p['own_heur'] = round(min(60.0, o), 1)
            p['own'] = p['own_heur']

    if not backtest:
        heads = list(read('lineup_news.json').get('headlines', []))
        heads += [{'headline': f"@{x['account']}: {x['text']}", 'source_url': x['url'], 'published_at': x['at']}
                  for x in read('x_news.json').get('posts', [])]
        attach_news(players, heads)
    for p in players:
        p.pop('_recent', None)
    out_singles = single_game_slates(singles, players)
    if not backtest:
        SIM_DAY[0] = day
        simulate(players, out_singles, read('today.json'))
    out = {'schema': 1, 'model': 'v28', 'date': day, 'weights': W, 'slate': slate.get('slate', 'Main'),
           'built_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
           'avg_team_total': round(avg_tt, 1) if avg_tt else None, 'games': games,
           'sources': {'slate': slate.get('source'), 'injuries_updated': read('injuries.json').get('updated_at'),
                       'stats_updated': cur.get('updated_at'), 'lineups_checked': read('lineup_news.json').get('checked_at')},
           'single_games': out_singles, 'sim_sig': SIM_SIG[0], 'own_model': own_params(),
           'players': sorted(players, key=lambda p: -p['proj'])}
    return out


def own_params():
    """Ownership model settings learned by learn_ownership.py (defaults otherwise)."""
    import field_sim as FS
    return {**FS.DEFAULT_OWN, **(read('ownership_params.json').get('params') or {})}


def chalk(own, power, total):
    """Sharpen (power > 1) or flatten ownership, keeping the slate total."""
    if power == 1 or not own:
        return own
    raw = {k: max(v, 0) ** power for k, v in own.items()}
    sc = total / (sum(raw.values()) or 1)
    return {k: min(95.0, v * sc) for k, v in raw.items()}


def apply_consensus(own, players, cons, P, total):
    """Blend public projected ownership (where available) and pick-article mentions into
    our ownership, then rescale to the slate total. Weights are learned."""
    ew, mw = P.get('external_w', 0.0), P.get('mention_w', 0.0)
    if not cons or (not ew and not mw):
        return own
    out = dict(own)
    for p in players:
        c = cons.get(norm(p['name']) + '|' + p['team'])
        if not c or id(p) not in out:
            continue
        v = out[id(p)]
        if ew and c.get('consensus_own') is not None:
            v = (1 - ew) * v + ew * c['consensus_own']
        if mw and c.get('mentions'):
            v *= 1 + mw * min(c['mentions'], 3)
        out[id(p)] = v
    sc = total / (sum(out.values()) or 1)
    return {k: min(95.0, v * sc) for k, v in out.items()}


def simulate(players, singles, old):
    """Field ownership (simulated optimizer field + feel), optimal rate, leverage, pivots.
    Reuses the previous run's numbers when nothing that matters changed."""
    import sys
    sys.path.insert(0, str(ROOT / 'scripts'))
    import field_sim as FS
    P = own_params()
    main = [p for p in players if p['main'] and p['proj'] > 0 and not p.get('out')]
    sig = json.dumps([P, read('public_consensus.json').get('updated_at'), len(read('alerts.json').get('late_news', []))] + [[p['name'], p['team'], p['salary'], round(p['proj'], 1), p.get('ext')] for p in main]
                     + [[sg['id'], [[r['k'], r['salary'], r['status']] for r in sg['players']]] for sg in singles])
    import hashlib
    sig = hashlib.sha1(sig.encode()).hexdigest()
    if old.get('sim_sig') == sig:
        prev = {(q['name'], q['team']): q for q in old.get('players', [])}
        for p in players:
            q = prev.get((p['name'], p['team']), {})
            for k in ('own', 'opt', 'lev', 'pivots', 'consensus', 'late_news'):
                if k in q:
                    p[k] = q[k]
        prev_sg = {x['id']: {r['k']: r for r in x['players']} for x in old.get('single_games', [])}
        for sg in singles:
            for r in sg['players']:
                q = prev_sg.get(sg['id'], {}).get(r['k'], {})
                for k in ('own', 'opt', 'lev'):
                    if k in q:
                        r[k] = q[k]
        SIM_SIG[0] = sig
        return
    sim = FS.field_ownership(main, 'classic', 60000, P, sims=200)
    own = chalk(FS.blend_ownership(main, sim, P, 900), P.get('chalk_power', 1.0), 900)
    pc = read('public_consensus.json')
    cons = {norm(c['name']) + '|' + c['team']: c for c in pc.get('players', [])} if pc.get('date') == SIM_DAY[0] else {}
    own = apply_consensus(own, main, cons, P, 900)
    # Late-news leverage: players who gained from news close to tip-off. Most of the field
    # has built by then, so they end up less owned than a fresh projection suggests.
    al = read('alerts.json')
    late = {x['key']: x for x in al.get('late_news', [])} if al.get('date') == SIM_DAY[0] else {}
    if late:
        lw = P.get('late_news_own', 0.8)
        for p in main:
            x = late.get(p['name'] + '|' + p['team'])
            if x and id(p) in own:
                own[id(p)] *= lw
                p['flags'].append(f"Late news: gained after {x['because']} (the field may not react in time)")
                p['late_news'] = True
        sc = 900 / (sum(own.values()) or 1)
        own = {k: v * sc for k, v in own.items()}
    for p in main:
        c = cons.get(norm(p['name']) + '|' + p['team'])
        if c:
            p['consensus'] = {k: c.get(k) for k in ('fta_own', 'linestar_own', 'mentions')}
            p['consensus']['articles'] = c.get('articles', [])[:3]
    opt = FS.optimal_rate(main, 'classic', 60000, sims=200)
    for p in main:
        p['own'] = round(own.get(id(p), 0.0), 1)
        p['opt'] = round(opt.get(id(p), 0.0), 1)
        p['lev'] = round(p['opt'] - p['own'], 1)
    for a, alts in FS.pivots(main).items():
        pa = next(p for p in main if id(p) == a)
        pa['pivots'] = [{'name': b['name'], 'team': b['team'], 'salary': b['salary'], 'proj': b['proj'], 'own': b['own']} for b in alts]
    by_key = {norm(p['name']) + '|' + p['team']: p for p in players}
    for sg in singles:
        clones = []
        for r in sg['players']:
            b = by_key.get(r['k'])
            if b and b['proj'] > 0 and not b.get('out') and r['status'] not in ('OUT', 'DOUBTFUL'):
                clones.append({'k': r['k'], 'name': b['name'], 'team': b['team'], 'position': b['position'], 'salary': r['salary'],
                               'proj': b['proj'], 'ext': b.get('ext'), 'ceiling': b.get('ceiling'), 'own_heur': r.get('own', 0)})
        if len(clones) < 6:
            continue
        s_sim = FS.field_ownership(clones, 'single', 60000, P, sims=150)
        s_own = chalk(FS.blend_ownership(clones, s_sim, P, 600), P.get('chalk_power', 1.0), 600)
        s_opt = FS.optimal_rate(clones, 'single', 60000, sims=150)
        rows = {r['k']: r for r in sg['players']}
        for c in clones:
            r = rows[c['k']]
            r['own'] = round(s_own.get(id(c), 0.0), 1)
            r['opt'] = round(s_opt.get(id(c), 0.0), 1)
            r['lev'] = round(r['opt'] - r['own'], 1)
    SIM_SIG[0] = sig


SIM_SIG = [None]
SIM_DAY = [None]


def single_game_slates(singles, players):
    """Per single-game slate: its own salaries and an ownership estimate normalized to 600%
    (6 roster spots: 1 MVP + 5 UTIL). Projections are shared with the main model by player key."""
    by_key = {norm(p['name']) + '|' + p['team']: p for p in players}
    out = []
    for sg in singles:
        rows = []
        for sp in sg['players']:
            k = norm(sp['name']) + '|' + canon(sp['team'])
            p = by_key.get(k)
            if not p:
                continue
            rows.append({'k': k, 'salary': sp['salary'], 'status': sp.get('status', 'ACTIVE')})
        elig = [(r, by_key[r['k']]) for r in rows if by_key[r['k']]['proj'] > 0]
        if elig:
            ws = [math.exp(0.06 * (p['proj'] - 25) + 0.15 * (min(p['proj'] * 1000 / max(r['salary'], 1000), 8) - 3)) for r, p in elig]
            tot = sum(ws)
            own = [600 * w / tot for w in ws]
            for _ in range(20):
                over = sum(max(0, o - 75) for o in own)
                if over < 0.01:
                    break
                free = [i for i, o in enumerate(own) if o < 75]
                fw = sum(own[i] for i in free) or 1
                own = [min(o, 75) for o in own]
                for i in free:
                    own[i] += over * own[i] / fw
            for (r, _), o in zip(elig, own):
                r['own'] = round(o, 1)
        out.append({'id': sg['id'], 'label': sg['label'], 'away': sg['away'], 'home': sg['home'], 'time': sg.get('time'),
                    'total': sg.get('total'), 'home_spread': sg.get('home_spread'), 'players': rows})
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
