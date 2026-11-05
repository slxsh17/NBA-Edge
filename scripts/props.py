#!/usr/bin/env python3
"""NBA EDGE player props, PAPER TRADING ONLY -> data/props/YYYY-MM-DD.json, data/props_results.json.

No bets are placed. Each night, about 30-150 minutes before the first main-slate tip, FanDuel's
points / rebounds / assists lines come from The Odds API (free plan: set the repository secret
ODDS_API_KEY). Credits decide the night (plan_night): on the free plan one pull per game about
40-150 minutes before its tip, usually points only; on a paid plan all three markets, re-pulled
every 15-120 minutes from 4 hours before each tip. Every pipeline run re-scores the lines with the
latest projections (the site's live props feed); a game's picks freeze at tip. For every line NBA EDGE estimates the chance of the over and the under:

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
BLEND_MIN_LINES = 300
MIN_MINUTES = 15
WINDOW = (40, 150)          # single pull (free plan): minutes before each game's tip
OPEN, CLOSE = 240, 10       # refreshing pulls (paid plan): from 4 hours to 10 minutes before tip
REFRESH = [15, 20, 30, 45, 60, 90, 120]
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
HONEST_MINUTES = str(read(DATA / 'learned_weights.json').get('method_version') or '').startswith('two-stage')


def prop_minutes(p):
    """Expected minutes for props. Since Nov 3 the learner fits minutes to actual minutes
    (two-stage), so today.json's `minutes` is used directly (miss 4.48, bias -0.04 on 1,152
    player-games of 15+ minutes). Before that `minutes` carried a learned shrink (3-5 minutes low
    for starters), and props used halfway between base and DARKO minutes plus injury minutes
    (miss 4.56), which remains the fallback."""
    if p.get('out'):
        return 0.0
    if HONEST_MINUTES and p.get('minutes'):
        return p['minutes']
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


def all_games(today):
    """Every game NBA EDGE projects tonight: Main slate, other classic slates, single games."""
    out, seen = [], set()
    for g in today.get('games', []) + [g for cs in today.get('classic_slates', []) for g in cs.get('games', [])] + today.get('single_games', []):
        k = (g.get('away'), g.get('home'))
        if g.get('time') and k not in seen:
            seen.add(k)
            out.append(g)
    return out


def game_key(away, home):
    return f'{away} @ {home}'


def plan_night(left, days_left, n_games):
    """How to spend tonight's credits (credits left / days left in the month): all three markets if
    possible, refreshed as often as the budget allows (every 15-120 minutes from 4 hours before each
    tip). Returns (markets, refresh minutes or None for a single pull about 2 hours before tip).
    Free plan (500 a month): one pull, usually points only. 20K plan: 3 markets every 15-20 min."""
    budget = float(left) / max(1, days_left) if left not in (None, '') else 3 * n_games
    n = max(1, n_games)
    for n_mk in (3, 2, 1):
        for iv in REFRESH:
            if n * n_mk * math.ceil((OPEN - CLOSE) / iv) <= budget:
                return MARKETS[:n_mk], iv
    return MARKETS[:max(0, min(3, int(budget // n)))], None


def due_games(today, fetched, now, refresh):
    """Games whose FanDuel lines should be pulled now. Single pull: tip 40-150 minutes away and not
    pulled yet. Refreshing: tip 10-240 minutes away and last pull at least `refresh` minutes ago."""
    out = []
    for g in all_games(today):
        tip = dt.datetime.fromisoformat(g['time'].replace('Z', '+00:00'))
        mins = (tip - now).total_seconds() / 60
        k = game_key(g.get('away'), g.get('home'))
        last = fetched.get(k)
        if refresh is None:
            if WINDOW[0] <= mins <= WINDOW[1] and not last:
                out.append(k)
        elif CLOSE <= mins <= OPEN and (not last or (now - dt.datetime.fromisoformat(last)).total_seconds() / 60 >= refresh - 3):
            out.append(k)
    return out


def tonight_events(key, day):
    """Today's events from The Odds API (this call is free) and credits left."""
    events, h = get(f'{API}/events?apiKey={urllib.parse.quote(key)}')
    out = []
    for e in events:
        t = dt.datetime.fromisoformat(e['commence_time'].replace('Z', '+00:00'))
        if t.astimezone(ET).date().isoformat() == day:
            out.append((e, t, game_key(TEAMS.get(e['away_team'], e['away_team']), TEAMS.get(e['home_team'], e['home_team']))))
    return out, h.get('x-requests-remaining')


def fetch_lines(key, events, want, markets, left=None):
    """FanDuel props for the games in `want` ("AWY @ HOM"); one credit per market per game.
    Returns (props, credits_remaining, pulled game keys)."""
    props, done = [], []
    for e, t, game in events:
        if game not in want or t <= dt.datetime.now(dt.timezone.utc):
            continue
        try:
            d, h = get(f"{API}/events/{e['id']}/odds?apiKey={urllib.parse.quote(key)}&regions=us&bookmakers=fanduel"
                       f"&oddsFormat=american&markets={','.join(markets)}&includeLinks=true")
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
                by, links = {}, {}
                for o in m.get('outcomes', []):
                    k = (o.get('description'), o.get('point'))
                    by.setdefault(k, {})[o.get('name')] = o.get('price')
                    if o.get('link'):        # FanDuel add-to-bet-slip link for this side
                        links.setdefault(k, {})[o.get('name')] = o['link']
                for (player, line), sides in by.items():
                    if player and line is not None and 'Over' in sides and 'Under' in sides:
                        lk = links.get((player, line), {})
                        props.append({'player': player, 'game': game, 'tip': t.isoformat(), 'market': m['key'],
                                      'line': line, 'over': sides['Over'], 'under': sides['Under'], 'line_at': at,
                                      **({'link_over': lk['Over']} if lk.get('Over') else {}),
                                      **({'link_under': lk['Under']} if lk.get('Under') else {})})
    return props, left, done


BASE = ('player', 'game', 'tip', 'market', 'line', 'over', 'under', 'line_at', 'line0', 'line0_at', 'link_over', 'link_under')


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


def blend_w():
    """Weight on NBA EDGE's probability when blending with FanDuel's no-vig one (log-odds). 0.5 until
    300+ graded lines, then the weight that scored best (Brier) on every graded line."""
    b = read(RESULTS).get('blend') or {}
    return b['w'] if b.get('lines', 0) >= BLEND_MIN_LINES and b.get('w') is not None else 0.5


def mix(p, q, w):
    lg = lambda x: math.log(min(max(x, 1e-4), 1 - 1e-4) / (1 - min(max(x, 1e-4), 1 - 1e-4)))
    return 1 / (1 + math.exp(-(w * lg(p) + (1 - w) * lg(q))))


def analyze(props, proj):
    rows = []
    w = blend_w()
    for pr in props:
        p = proj.get(norm(pr['player']))
        st = STAT[pr['market']]
        row = {**pr, 'stat': st}
        if p:
            mu = p[st]
            ov, push, un = probs(pr['line'], mu, PHI[st])
            mo, mu_ = implied(pr['over']), implied(pr['under'])
            mkt = mo / (mo + mu_)
            # Picks use our probability blended with FanDuel's: two forecasters that each know things
            # the other doesn't beat either alone (Nov 2: Brier 0.2516 blended vs 0.2549 ours, 0.2546
            # FanDuel), and the blend only clears the bar when we disagree with the line by a lot.
            bo = mix(ov / max(1e-9, ov + un), mkt, w) * (1 - push)
            bu = (1 - push) - bo
            row.update({'team': p['team'], 'minutes': round(p['minutes'], 1), 'proj': round(mu, 2),
                        'p_over': round(ov, 4), 'p_under': round(un, 4), 'p_push': round(push, 4),
                        'market_p_over': round(mkt, 4), 'blend_p_over': round(bo, 4), 'blend_w': w})
            if not p['out'] and not p.get('shaky') and p['minutes'] >= MIN_MINUTES:
                for side, pw, price in (('over', bo, pr['over']), ('under', bu, pr['under'])):
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
    picks, cal, brier, pairs = [], [], {'ours': [], 'market': []}, []
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
                pairs.append((r['p_over'] / max(1e-9, r['p_over'] + r.get('p_under', 1 - r['p_over'])), r['market_p_over'], y))
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
    blend = None
    if pairs:
        def bs(w):
            return sum((mix(p, q, w) - y) ** 2 for p, q, y in pairs) / len(pairs)
        bw = min((bs(w / 20), w / 20) for w in range(21))
        blend = {'w': round(bw[1], 2), 'brier': round(bw[0], 4), 'brier_at_0.5': round(bs(0.5), 4), 'lines': len(pairs),
                 'in_use': bw[1] if len(pairs) >= BLEND_MIN_LINES else 0.5}
    out = {'schema': 1, 'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'), 'status': status,
           'paper_only': True, 'nights': nights, 'lines_graded': len(cal),
           'picks': {'n': len(picks), 'wins': w, 'losses': lo, 'pushes': sum(p['result'] == 'push' for p in picks),
                     'units': round(sum(p['units'] for p in picks), 2),
                     'roi': round(sum(p['units'] for p in picks) / max(1, w + lo), 4) if picks else None,
                     'hit_rate': round(w / (w + lo), 4) if w + lo else None,
                     'avg_breakeven': mean([p['breakeven'] for p in picks])},
           'brier': {'nba_edge': mean(brier['ours']), 'fanduel_no_vig': mean(brier['market']),
                     'blend': blend['brier_at_0.5'] if blend else None},
           'blend': blend,
           'calibration': bins, 'recent_picks': picks[-40:],
           'method': 'Paper only. Mean = projected minutes x DARKO per-minute rate; negative binomial spread; '
                     f'blended 50/50 (log-odds) with FanDuel no-vig until {BLEND_MIN_LINES}+ graded lines, then the best-scoring weight; '
                     f'pick when the blend beats FanDuel break-even by {int(EDGE * 100)}+ points. '
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
    soon = [g for g in all_games(today) if 0 < (dt.datetime.fromisoformat(g['time'].replace('Z', '+00:00')) - now).total_seconds() / 60 <= OPEN]
    if key and soon:
        try:
            events, left = tonight_events(key, day)
            if 'refresh_min' not in doc:
                nxt = (dt.datetime.now(ET).replace(day=1) + dt.timedelta(days=32)).replace(day=1)
                doc['markets'], doc['refresh_min'] = plan_night(left, (nxt.date() - dt.datetime.now(ET).date()).days, len(all_games(today)))
                doc['credits_at_start'] = left
                print(f"props: tonight {', '.join(STAT[m] for m in doc['markets']) or 'no markets'}, "
                      f"{'refresh every ' + str(doc['refresh_min']) + ' min' if doc['refresh_min'] else 'one pull per game'} ({left} credits left)")
            want = due_games(today, doc['fetched'], now, doc['refresh_min']) if doc['markets'] else []
            if want:
                rows, left, done = fetch_lines(key, events, want, doc['markets'], left)
                doc['credits_remaining'] = left
                old = {(r['player'], r['market']): r for r in doc['props'] if r['game'] in done}
                for r in rows:
                    o = old.get((r['player'], r['market']))
                    if o:
                        # keep where the line opened, and a pick's start time when the line hasn't changed
                        r['line0'], r['line0_at'] = o.get('line0', o['line']), o.get('line0_at', o.get('line_at'))
                        if o.get('pick') and o['line'] == r['line']:
                            r['pick'] = o['pick']
                for g in done:
                    doc['fetched'][g] = now.isoformat(timespec='seconds')
                doc['props'] = [r for r in doc['props'] if r['game'] not in done] + rows
                print(f"props: pulled {', '.join(done) or 'nothing'} ({left} credits left)")
        except Exception as e:
            print(f'::warning::props fetch failed: {e}')
            status = f'Last fetch failed: {e}'[:200]
    if doc['props'] and not doc.get('graded') and proj:
        doc['props'] = rescore(doc['props'], proj, now)
        doc['updated_at'] = now.isoformat(timespec='seconds')
        doc['games'] = {game_key(g.get('away'), g.get('home')): g.get('time') for g in all_games(today)}
        path.write_text(json.dumps(doc, separators=(',', ':')) + '\n', encoding='utf-8')
        n_p = sum(1 for r in doc['props'] if r.get('pick') and dt.datetime.fromisoformat(r['tip'].replace('Z', '+00:00')) > now)
        print(f"props: {len(doc['props'])} lines, {n_p} live paper picks")
    s = summarize(status)
    print('props record:', s['picks'], 'brier', s['brier'])


if __name__ == '__main__':
    main()
