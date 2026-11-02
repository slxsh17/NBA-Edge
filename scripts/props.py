#!/usr/bin/env python3
"""NBA EDGE player props, PAPER TRADING ONLY -> data/props/YYYY-MM-DD.json, data/props_results.json.

No bets are placed. Each night, about 30-150 minutes before the first main-slate tip, FanDuel's
points / rebounds / assists lines come from The Odds API (free plan: set the repository secret
ODDS_API_KEY), each game's lines about 40-150 minutes before its tip. Every pipeline run re-scores
the lines with the latest projections (the site's live props feed); a game's picks freeze at tip. For every line NBA EDGE estimates the chance of the over and the under:

  mean   = expected minutes (NBA EDGE base / DARKO average, plus injury minutes) x DARKO
           per-minute rate (pts, reb, ast; points also get the Vegas team-total adjustment)
  spread = negative binomial with variance = phi x mean, phi fitted by log loss on 976 graded
           player-games (Oct 21-Nov 1, lines around each mean): pts 4, reb 1.2, ast 1.4

A prop is a paper pick when our probability beats FanDuel's break-even (its price, vig included)
by 5+ points, the player is active (not questionable), has no role-change or outside-projection
flag, and is projected for 15+ minutes. After the games every
line is graded (DNPs void, as FanDuel does): picks get a W/L and units at the posted odds, and
all lines feed a calibration check of our probabilities against the market's no-vig ones.
That last check is the honest test: an edge exists only if our probabilities beat the market's.

Credits: the events list is free; each game's props cost one credit per market returned. The
script spends at most (credits left / days left in the month) per night, dropping markets
(rebounds, then assists) when that's short.
"""
import datetime as dt
import json
import math
import os
import pathlib
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
OUT = DATA / 'props'
RESULTS = DATA / 'props_results.json'
API = 'https://api.the-odds-api.com/v4/sports/basketball_nba'
ET = ZoneInfo('America/New_York')
MARKETS = ['player_points', 'player_assists', 'player_rebounds']      # priority order when credits are short
STAT = {'player_points': 'pts', 'player_rebounds': 'reb', 'player_assists': 'ast'}
PHI = {'pts': 4.0, 'reb': 1.2, 'ast': 1.4}
EDGE = 0.05
MIN_MINUTES = 15
WINDOW = (40, 150)          # fetch each game's lines this many minutes before its tip
TEAMS = {'Atlanta Hawks': 'ATL', 'Boston Celtics': 'BOS', 'Brooklyn Nets': 'BKN', 'Charlotte Hornets': 'CHA',
         'Chicago Bulls': 'CHI', 'Cleveland Cavaliers': 'CLE', 'Dallas Mavericks': 'DAL', 'Denver Nuggets': 'DEN',
         'Detroit Pistons': 'DET', 'Golden State Warriors': 'GSW', 'Houston Rockets': 'HOU', 'Indiana Pacers': 'IND',
         'LA Clippers': 'LAC', 'Los Angeles Clippers': 'LAC', 'Los Angeles Lakers': 'LAL', 'Memphis Grizzlies': 'MEM',
         'Miami Heat': 'MIA', 'Milwaukee Bucks': 'MIL', 'Minnesota Timberwolves': 'MIN', 'New Orleans Pelicans': 'NOP',
         'New York Knicks': 'NYK', 'Oklahoma City Thunder': 'OKC', 'Orlando Magic': 'ORL', 'Philadelphia 76ers': 'PHI',
         'Phoenix Suns': 'PHX', 'Portland Trail Blazers': 'POR', 'Sacramento Kings': 'SAC', 'San Antonio Spurs': 'SAS',
         'Toronto Raptors': 'TOR', 'Utah Jazz': 'UTA', 'Washington Wizards': 'WAS'}


def read(p, default=None):
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {} if default is None else default


def norm(s):
    s = unicodedata.normalize('NFKD', str(s or '')).lower().strip()
    s = re.sub(r'[\s,]+(jr|sr|ii|iii|iv|v)\.?$', '', s)
    return re.sub(r'[^a-z0-9]', '', s)


def p_at_least(k, mu, phi):
    """P(X >= k), X negative binomial with mean mu and variance phi*mu (Poisson if phi <= 1)."""
    if k <= 0:
        return 1.0
    mu = max(mu, 0.05)
    if phi <= 1.0001:
        p = c = math.exp(-mu)
        for i in range(1, k):
            p *= mu / i
            c += p
        return max(0.0, 1 - c)
    r = mu / (phi - 1)
    p = c = (r / (r + mu)) ** r
    for i in range(k - 1):
        p *= (i + r) / (i + 1) * (mu / (r + mu))
        c += p
    return max(0.0, 1 - c)


def probs(line, mu, phi):
    """(over, push, under) for a line; push only on whole-number lines."""
    k = math.floor(line) + 1
    over = p_at_least(k, mu, phi)
    push = (p_at_least(int(line), mu, phi) - over) if float(line).is_integer() else 0.0
    return over, push, max(0.0, 1 - over - push)


def implied(price):
    return 100 / (price + 100) if price > 0 else -price / (-price + 100)


def profit(price):
    """Profit per 1 unit risked."""
    return price / 100 if price > 0 else 100 / -price


def get(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'NBA-EDGE-personal'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode('utf-8')), r.headers


# ---------- tonight ----------
def prop_minutes(p):
    """Expected minutes for props: halfway between NBA EDGE's base minutes and DARKO's, plus
    injury-freed minutes. Not today.json's `minutes`: that one carries a learned shrink that the
    FanDuel projection undoes with its scale and outside blend, so it runs 3-5 minutes low for
    starters (Oct 27-Nov 1). This version: bias -0.1 min, miss 4.4 min on 817 player-games."""
    if p.get('out'):
        return 0.0
    b = p.get('base_minutes') or 0.0
    m = (b + p['darko_min']) / 2 if p.get('darko_min') and b > 0 else b
    return m + (p.get('adj') or {}).get('injury_min', 0.0) if m > 0 else 0.0


def projections(today):
    """norm(name) -> {team, minutes, out, pts, reb, ast} from tonight's minutes and DARKO rates."""
    darko = {}
    for r in read(DATA / 'darko.json').get('players', []):
        darko.setdefault(norm(r['name']), []).append(r)
    out = {}
    for p in today.get('players', []):
        cands = darko.get(norm(p['name'])) or []
        d = next((r for r in cands if r['team'] == p['team']), cands[0] if len(cands) == 1 else None)
        m = prop_minutes(p)
        if not d or not m:
            continue
        poss = d['pace'] / 4800.0 * m
        vg = (p.get('adj') or {}).get('vegas', 1.0)
        out[norm(p['name'])] = {'name': p['name'], 'team': p['team'], 'minutes': m, 'out': bool(p.get('out')),
                                'status': p.get('status'),
                                # no paper pick when the inputs are shaky: game-time decisions, a role
                                # change our minutes model flags, or a big gap to the outside projection
                                'shaky': (p.get('status') or 'ACTIVE').upper() not in ('ACTIVE', 'PROBABLE')
                                or any(f.startswith(('ROLE', 'Outside projection disagrees')) for f in p.get('flags') or []),
                                'pts': d['pts_100'] * poss * vg, 'reb': d['reb_100'] * poss,
                                'ast': d['ast_100'] * poss}
    return out


def first_tip(today):
    tips = [dt.datetime.fromisoformat(g['time'].replace('Z', '+00:00')) for g in today.get('games', []) if g.get('time')]
    return min(tips) if tips else None


def game_key(away, home):
    return f'{away} @ {home}'


def due_games(today, fetched, now):
    """Today's games whose FanDuel lines should be fetched now: tip 40-150 minutes away, not fetched yet."""
    out = []
    for g in today.get('games', []):
        if not g.get('time'):
            continue
        tip = dt.datetime.fromisoformat(g['time'].replace('Z', '+00:00'))
        mins = (tip - now).total_seconds() / 60
        k = game_key(g.get('away'), g.get('home'))
        if WINDOW[0] <= mins <= WINDOW[1] and k not in fetched:
            out.append(k)
    return out


def fetch_lines(key, day, want, markets=None, n_games=None):
    """FanDuel props for the games in `want` ("AWY @ HOM"). Returns (props, credits_remaining, markets, fetched keys).
    Markets are chosen once per night from the credit budget (credits left / days left in the month,
    spread over all of tonight's games) and reused for the night's later games."""
    events, h = get(f'{API}/events?apiKey={urllib.parse.quote(key)}')     # free
    left = h.get('x-requests-remaining')
    todays = []
    for e in events:
        t = dt.datetime.fromisoformat(e['commence_time'].replace('Z', '+00:00'))
        if t.astimezone(ET).date().isoformat() == day:
            todays.append((e, t, game_key(TEAMS.get(e['away_team'], e['away_team']), TEAMS.get(e['home_team'], e['home_team']))))
    if markets is None:
        now = dt.datetime.now(ET)
        nxt = (now.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
        days_left = max(1, (nxt.date() - now.date()).days)
        budget = float(left) / days_left if left not in (None, '') else 3 * len(todays)
        markets = MARKETS[:max(0, min(3, int(budget // max(1, n_games or len(todays)))))]
    if not markets:
        return [], left, markets, []
    props, done = [], []
    for e, t, game in todays:
        if game not in want or t <= dt.datetime.now(dt.timezone.utc):
            continue
        try:
            d, h = get(f"{API}/events/{e['id']}/odds?apiKey={urllib.parse.quote(key)}&regions=us&bookmakers=fanduel"
                       f"&oddsFormat=american&markets={','.join(markets)}")
            left = h.get('x-requests-remaining', left)
        except Exception as ex:
            print(f"::warning::props {game}: {ex}")
            continue
        done.append(game)
        at = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
        for b in d.get('bookmakers', []):
            if b.get('key') != 'fanduel':
                continue
            for m in b.get('markets', []):
                if m.get('key') not in STAT:
                    continue
                by = {}
                for o in m.get('outcomes', []):
                    by.setdefault((o.get('description'), o.get('point')), {})[o.get('name')] = o.get('price')
                for (player, line), sides in by.items():
                    if player and line is not None and 'Over' in sides and 'Under' in sides:
                        props.append({'player': player, 'game': game, 'tip': t.isoformat(), 'market': m['key'],
                                      'line': line, 'over': sides['Over'], 'under': sides['Under'], 'line_at': at})
    return props, left, markets, done


BASE = ('player', 'game', 'tip', 'market', 'line', 'over', 'under', 'line_at')


def rescore(rows, proj, now):
    """Live re-scoring with the latest projections for games that haven't tipped. Rows for games that
    have started are left as they were at tip: that last pre-tip pick is what gets graded. When our
    projection has moved a lot since the line was posted (news), FanDuel has probably moved the line
    too, so no pick until fresh lines come in."""
    out = []
    for r in rows:
        if dt.datetime.fromisoformat(r['tip'].replace('Z', '+00:00')) <= now:
            out.append(r)
            continue
        new = analyze([{k: r[k] for k in BASE if k in r}], proj)[0]
        p0 = r['proj0'] if 'proj0' in r else new.get('proj')
        new['proj0'] = p0
        if p0 is not None and new.get('proj') is not None and abs(new['proj'] - p0) > max(1.0, 0.12 * p0):
            new['moved'] = True
            new.pop('pick', None)
        if new.get('pick') and r.get('pick') and r['pick']['side'] == new['pick']['side']:
            new['pick']['since'] = r['pick'].get('since') or r.get('line_at')
        elif new.get('pick'):
            new['pick']['since'] = now.isoformat(timespec='seconds')
        out.append(new)
    return out


def analyze(props, proj):
    rows = []
    for pr in props:
        p = proj.get(norm(pr['player']))
        st = STAT[pr['market']]
        row = {**pr, 'stat': st}
        if p:
            mu = p[st]
            ov, push, un = probs(pr['line'], mu, PHI[st])
            mo, mu_ = implied(pr['over']), implied(pr['under'])
            row.update({'team': p['team'], 'minutes': round(p['minutes'], 1), 'proj': round(mu, 2),
                        'p_over': round(ov, 4), 'p_under': round(un, 4), 'p_push': round(push, 4),
                        'market_p_over': round(mo / (mo + mu_), 4)})
            if not p['out'] and not p.get('shaky') and p['minutes'] >= MIN_MINUTES:
                for side, pw, price in (('over', ov, pr['over']), ('under', un, pr['under'])):
                    be = implied(price)
                    if pw - be >= EDGE:
                        row['pick'] = {'side': side, 'prob': round(pw, 4), 'breakeven': round(be, 4), 'price': price,
                                       'ev': round(pw * profit(price) - (1 - pw - push), 4)}
        rows.append(row)
    return rows


# ---------- grading ----------
def actuals():
    out = {}
    for r in read(DATA / 'season_logs.json').get('game_logs', []):
        d = str(r.get('date'))
        out[(norm(r['name']), f'{d[:4]}-{d[4:6]}-{d[6:]}')] = r
    return out


def grade(today_iso):
    acts = actuals()
    for f in sorted(OUT.glob('*.json')) if OUT.exists() else []:
        day = f.stem
        doc = read(f)
        if day >= today_iso or doc.get('graded') or not doc.get('props'):
            continue
        done = 0
        for r in doc['props']:
            a = acts.get((norm(r['player']), day))
            if not a:
                continue                              # box score not in yet (or DNP: no log row)
            if (a.get('minutes') or 0) <= 0:
                r['result'] = 'void'
            else:
                v = a.get(r['stat'])
                r['actual'] = v
                r['result'] = 'push' if v == r['line'] else ('over' if v > r['line'] else 'under')
            done += 1
        if done and done >= 0.8 * len(doc['props']):
            doc['graded'] = True
            f.write_text(json.dumps(doc, separators=(',', ':')) + '\n', encoding='utf-8')


def summarize(status):
    picks, cal, brier = [], [], {'ours': [], 'market': []}
    nights = 0
    for f in sorted(OUT.glob('*.json')) if OUT.exists() else []:
        doc = read(f)
        if not doc.get('graded'):
            continue
        nights += 1
        for r in doc['props']:
            if r.get('result') in (None, 'void') or r.get('p_over') is None:
                continue
            if r['result'] != 'push':
                y = 1 if r['result'] == 'over' else 0
                cal.append((r['p_over'], y))
                brier['ours'].append((r['p_over'] - y) ** 2)
                brier['market'].append((r['market_p_over'] - y) ** 2)
            pk = r.get('pick')
            if pk:
                won = r['result'] == pk['side']
                units = 0.0 if r['result'] == 'push' else (profit(pk['price']) if won else -1.0)
                picks.append({'date': f.stem, 'player': r['player'], 'stat': r['stat'], 'line': r['line'], 'side': pk['side'],
                              'price': pk['price'], 'prob': pk['prob'], 'breakeven': pk['breakeven'], 'actual': r.get('actual'),
                              'result': 'push' if r['result'] == 'push' else ('win' if won else 'loss'), 'units': round(units, 3)})
    w = sum(p['result'] == 'win' for p in picks)
    lo = sum(p['result'] == 'loss' for p in picks)
    bins = []
    for lo_, hi_ in ((0, .3), (.3, .4), (.4, .5), (.5, .6), (.6, .7), (.7, 1.01)):
        xs = [(p, y) for p, y in cal if lo_ <= p < hi_]
        if xs:
            bins.append({'range': f'{lo_:.1f}-{min(hi_, 1):.1f}', 'n': len(xs), 'predicted': round(sum(p for p, _ in xs) / len(xs), 3),
                         'actual': round(sum(y for _, y in xs) / len(xs), 3)})
    mean = lambda xs: round(sum(xs) / len(xs), 4) if xs else None
    out = {'schema': 1, 'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'), 'status': status,
           'paper_only': True, 'nights': nights, 'lines_graded': len(cal),
           'picks': {'n': len(picks), 'wins': w, 'losses': lo, 'pushes': sum(p['result'] == 'push' for p in picks),
                     'units': round(sum(p['units'] for p in picks), 2),
                     'roi': round(sum(p['units'] for p in picks) / max(1, w + lo), 4) if picks else None,
                     'hit_rate': round(w / (w + lo), 4) if w + lo else None,
                     'avg_breakeven': mean([p['breakeven'] for p in picks])},
           'brier': {'nba_edge': mean(brier['ours']), 'fanduel_no_vig': mean(brier['market'])},
           'calibration': bins, 'recent_picks': picks[-40:],
           'method': 'Paper only. Mean = projected minutes x DARKO per-minute rate; negative binomial spread; '
                     f'pick when our probability beats FanDuel break-even by {int(EDGE * 100)}+ points. '
                     'Lower Brier = better probabilities; an edge needs NBA EDGE below FanDuel no-vig.'}
    RESULTS.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    return out


def main():
    today = read(DATA / 'today.json')
    now = dt.datetime.now(dt.timezone.utc)
    day = dt.datetime.now(ET).date().isoformat()
    OUT.mkdir(exist_ok=True)
    grade(day)
    key = os.environ.get('ODDS_API_KEY', '').strip()
    path = OUT / f'{day}.json'
    status = 'Waiting for ODDS_API_KEY (free key from the-odds-api.com, added as a GitHub repository secret).' if not key else 'ok'
    doc = read(path) or {'date': day, 'props': []}
    doc.setdefault('fetched', {})
    if today.get('date') != day:
        today = {}
    proj = projections(today)
    want = due_games(today, doc['fetched'], now) if key and today else []
    if want:
        try:
            rows, left, markets, done = fetch_lines(key, day, want, doc.get('markets'), len(today.get('games', [])))
            doc['markets'] = markets
            doc['credits_remaining'] = left
            for g in done:
                doc['fetched'][g] = now.isoformat(timespec='seconds')
            doc['props'] = [r for r in doc['props'] if r['game'] not in done] + rows
            print(f"props: fetched {', '.join(done) or 'nothing'} ({', '.join(STAT[m] for m in markets) or 'no credits'}; {left} credits left)")
        except Exception as e:
            print(f'::warning::props fetch failed: {e}')
            status = f'Last fetch failed: {e}'[:200]
    elif today:
        nxt = [g for g in today.get('games', []) if g.get('time') and game_key(g.get('away'), g.get('home')) not in doc['fetched']]
        if nxt:
            print(f"props: next lines about {WINDOW[1]} min before {min(g['time'] for g in nxt)}")
    if doc['props'] and not doc.get('graded') and proj:
        doc['props'] = rescore(doc['props'], proj, now)
        doc['updated_at'] = now.isoformat(timespec='seconds')
        doc['games'] = {game_key(g.get('away'), g.get('home')): g.get('time') for g in today.get('games', []) if g.get('time')}
        path.write_text(json.dumps(doc, separators=(',', ':')) + '\n', encoding='utf-8')
        n_p = sum(1 for r in doc['props'] if r.get('pick') and dt.datetime.fromisoformat(r['tip'].replace('Z', '+00:00')) > now)
        print(f"props: {len(doc['props'])} lines, {n_p} live paper picks")
    s = summarize(status)
    print('props record:', s['picks'], 'brier', s['brier'])


if __name__ == '__main__':
    main()
