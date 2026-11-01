#!/usr/bin/env python3
"""NBA EDGE player props, PAPER TRADING ONLY -> data/props/YYYY-MM-DD.json, data/props_results.json.

No bets are placed. Each night, about 30-150 minutes before the first main-slate tip, FanDuel's
points / rebounds / assists lines come from The Odds API (free plan: set the repository secret
ODDS_API_KEY). For every line NBA EDGE estimates the chance of the over and the under:

  mean   = NBA EDGE projected minutes x DARKO per-minute rate (pts, reb, ast; points also get
           the Vegas team-total adjustment)
  spread = negative binomial with variance = phi x mean, phi fitted on 1,100 graded player-games
           (Oct 22-31) so predicted over rates matched actual ones: pts 6, reb 4, ast 4

A prop is a paper pick when our probability beats FanDuel's break-even (its price, vig included)
by 5+ points, the player isn't out and is projected for 15+ minutes. After the games every
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
PHI = {'pts': 6.0, 'reb': 4.0, 'ast': 4.0}
EDGE = 0.05
MIN_MINUTES = 15
WINDOW = (30, 150)          # minutes before the first main-slate tip
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
def projections(today):
    """norm(name) -> {team, minutes, out, pts, reb, ast} from tonight's minutes and DARKO rates."""
    darko = {}
    for r in read(DATA / 'darko.json').get('players', []):
        darko.setdefault(norm(r['name']), []).append(r)
    out = {}
    for p in today.get('players', []):
        cands = darko.get(norm(p['name'])) or []
        d = next((r for r in cands if r['team'] == p['team']), cands[0] if len(cands) == 1 else None)
        if not d or not p.get('minutes'):
            continue
        poss = d['pace'] / 4800.0 * p['minutes']
        vg = (p.get('adj') or {}).get('vegas', 1.0)
        out[norm(p['name'])] = {'name': p['name'], 'team': p['team'], 'minutes': p['minutes'], 'out': bool(p.get('out')),
                                'status': p.get('status'), 'pts': d['pts_100'] * poss * vg, 'reb': d['reb_100'] * poss,
                                'ast': d['ast_100'] * poss}
    return out


def first_tip(today):
    tips = [dt.datetime.fromisoformat(g['time'].replace('Z', '+00:00')) for g in today.get('games', []) if g.get('time')]
    return min(tips) if tips else None


def fetch_lines(key, day):
    """FanDuel props for today's games. Returns (props, credits_remaining, note)."""
    events, h = get(f'{API}/events?apiKey={urllib.parse.quote(key)}')
    todays = []
    for e in events:
        t = dt.datetime.fromisoformat(e['commence_time'].replace('Z', '+00:00'))
        if t.astimezone(ET).date().isoformat() == day and t > dt.datetime.now(dt.timezone.utc):
            todays.append((e, t))
    if not todays:
        return [], h.get('x-requests-remaining'), 'no games left today'
    left = h.get('x-requests-remaining')
    now = dt.datetime.now(ET)
    nxt = (now.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    days_left = max(1, (nxt.date() - now.date()).days)
    budget = float(left) / days_left if left not in (None, '') else 3 * len(todays)
    n_mk = max(0, min(3, int(budget // max(1, len(todays)))))
    if n_mk == 0:
        return [], left, f'not enough credits tonight ({left} left, {days_left} days to go)'
    markets = MARKETS[:n_mk]
    props = []
    for e, t in todays:
        try:
            d, h = get(f"{API}/events/{e['id']}/odds?apiKey={urllib.parse.quote(key)}&regions=us&bookmakers=fanduel"
                       f"&oddsFormat=american&markets={','.join(markets)}")
            left = h.get('x-requests-remaining', left)
        except Exception as ex:
            print(f"::warning::props {e.get('away_team')} @ {e.get('home_team')}: {ex}")
            continue
        game = f"{TEAMS.get(e['away_team'], e['away_team'])} @ {TEAMS.get(e['home_team'], e['home_team'])}"
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
                                      'line': line, 'over': sides['Over'], 'under': sides['Under']})
    return props, left, f"markets: {', '.join(STAT[m] for m in markets)}"


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
            if not p['out'] and p['minutes'] >= MIN_MINUTES:
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
    day = dt.datetime.now(ET).date().isoformat()
    OUT.mkdir(exist_ok=True)
    grade(day)
    key = os.environ.get('ODDS_API_KEY', '').strip()
    path = OUT / f'{day}.json'
    status = 'Waiting for ODDS_API_KEY (free key from the-odds-api.com, added as a GitHub repository secret).' if not key else 'ok'
    tip = first_tip(today) if today.get('date') == day else None
    if key and tip and not read(path).get('props'):
        mins = (tip - dt.datetime.now(dt.timezone.utc)).total_seconds() / 60
        if WINDOW[0] <= mins <= WINDOW[1] or '--force' in sys.argv:
            try:
                lines, left, note = fetch_lines(key, day)
                rows = analyze(lines, projections(today))
                path.write_text(json.dumps({'date': day, 'fetched_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
                                            'credits_remaining': left, 'note': note, 'props': rows}, separators=(',', ':')) + '\n',
                                encoding='utf-8')
                n_p = sum(1 for r in rows if r.get('pick'))
                print(f'props: {len(rows)} lines, {n_p} paper picks ({note}; {left} credits left)')
            except Exception as e:
                print(f'::warning::props fetch failed: {e}')
                status = f'Last fetch failed: {e}'[:200]
        else:
            print(f'props: {mins:.0f} min to first tip; fetching {WINDOW[0]}-{WINDOW[1]} min before')
    s = summarize(status)
    print('props record:', s['picks'], 'brier', s['brier'])


if __name__ == '__main__':
    main()
