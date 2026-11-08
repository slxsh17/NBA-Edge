#!/usr/bin/env python3
"""Kalshi vs FanDuel, paper only: are Kalshi's NBA player-points contracts mispriced against
FanDuel's lines?

FanDuel's no-vig odds have been a better forecast than NBA EDGE's own model, so this uses them as
the fair price. For every Kalshi "Player: N+ points" contract (series KXNBAPTS, public market data,
no key) in a game FanDuel has a points line for, the fair chance of N+ comes from FanDuel's line:
the same line when Kalshi's strike matches it, otherwise a negative-binomial points distribution
whose mean is fitted so it reproduces FanDuel's no-vig chance at FanDuel's line. A paper pick is
buying YES or NO at Kalshi's ask when the fair chance beats the ask plus Kalshi's taker fee
(0.07 x price x (1 - price) per contract) by EDGE or more. Picks freeze at tip and are graded from
box scores; data/kalshi_results.json keeps the record plus the real test: whether FanDuel's fair
price or Kalshi's own mid predicted the outcomes better (Brier).

Nothing is ever traded.
"""
import datetime as dt
import json
import math
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
OUT = DATA / 'kalshi'
RESULTS = DATA / 'kalshi_results.json'
sys.path.insert(0, str(ROOT / 'scripts'))
from props import ET, PHI, norm, probs, read  # noqa: E402

API = 'https://api.elections.kalshi.com/trade-api/v2'
SERIES = {'KXNBAPTS': 'pts'}
EDGE = 0.03          # fair chance must beat ask + fee by 3 points
MAX_GAP = 3.0        # picks only within 3 points of FanDuel's line, where the line pins the fair price down
MIN_PRICE, MAX_PRICE = 0.03, 0.97
MON = {m: i for i, m in enumerate(['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'], 1)}


def get(path, **q):
    url = API + path + ('?' + urllib.parse.urlencode(q) if q else '')
    req = urllib.request.Request(url, headers={'User-Agent': 'NBA-EDGE-personal', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode('utf-8'))


def fee(p):
    return 0.07 * p * (1 - p)


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def kalshi_day(event_ticker):
    """KXNBAPTS-26NOV08PHICHI -> 2026-11-08."""
    m = re.search(r'-(\d\d)([A-Z]{3})(\d\d)', event_ticker or '')
    if not m or m.group(2) not in MON:
        return None
    return f'20{m.group(1)}-{MON[m.group(2)]:02d}-{m.group(3)}'


def markets(day):
    """Open Kalshi player-points markets for `day`."""
    out = []
    for series, stat in SERIES.items():
        cursor = ''
        for _ in range(30):
            q = {'series_ticker': series, 'status': 'open', 'limit': 1000}
            if cursor:
                q['cursor'] = cursor
            doc = get('/markets', **q)
            for m in doc.get('markets', []):
                if kalshi_day(m.get('event_ticker')) != day:
                    continue
                name = (m.get('title') or '').split(':')[0].strip()
                floor = num(m.get('floor_strike'))
                if not name or floor is None or m.get('strike_type') != 'greater':
                    continue
                out.append({'ticker': m['ticker'], 'event': m['event_ticker'], 'player': name, 'stat': stat,
                            'floor': floor, 'title': m.get('title'),
                            'yes_bid': num(m.get('yes_bid_dollars')), 'yes_ask': num(m.get('yes_ask_dollars')),
                            'no_ask': num(m.get('no_ask_dollars')), 'close': m.get('expected_expiration_time')})
            cursor = doc.get('cursor') or ''
            if not cursor:
                break
            time.sleep(0.25)
    return out


def fair_over(line, p_over_line, floor, phi):
    """P(stat > floor) given FanDuel's no-vig chance of going over `line`."""
    if abs(floor - line) < 1e-9:
        return p_over_line
    lo, hi = 0.2, 80.0
    for _ in range(60):                       # fit the mean so P(X > line) matches FanDuel
        mid = (lo + hi) / 2
        ov, push, un = probs(line, mid, phi)
        if ov / max(1e-9, ov + un) < p_over_line:
            lo = mid
        else:
            hi = mid
    ov, push, un = probs(floor, (lo + hi) / 2, phi)
    return ov / max(1e-9, ov + un)


def analyze(day, now):
    fd = read(DATA / 'props' / f'{day}.json').get('props') or []
    lines = {}
    for r in fd:
        if r.get('market_p_over') is None or r.get('moved') or r.get('eligible') is False:
            continue
        lines[(norm(r['player']), r['stat'])] = r
    rows = []
    for k in markets(day):
        r = lines.get((norm(k['player']), k['stat']))
        if not r:
            continue
        if dt.datetime.fromisoformat(r['tip'].replace('Z', '+00:00')) <= now:
            continue
        fair = fair_over(r['line'], r['market_p_over'], k['floor'], PHI[k['stat']])
        ours = None
        if r.get('proj') is not None:
            ov, push, un = probs(k['floor'], max(0.3, r['proj']), PHI[k['stat']])
            ours = ov / max(1e-9, ov + un)
        row = {**k, 'game': r['game'], 'tip': r['tip'], 'fd_line': r['line'], 'fd_over': r['over'], 'fd_under': r['under'],
               'fd_p_over': r['market_p_over'], 'fair': round(fair, 4), 'ours': ours and round(ours, 4),
               'mid': round((k['yes_bid'] + k['yes_ask']) / 2, 4) if k['yes_bid'] is not None and k['yes_ask'] is not None else None}
        best = None
        row['gap'] = round(k['floor'] - r['line'], 1)
        for side, price, p in (('yes', k['yes_ask'], fair), ('no', k['no_ask'], 1 - fair)):
            if price is None or not MIN_PRICE <= price <= MAX_PRICE or abs(row['gap']) > MAX_GAP:
                continue
            e = p - price - fee(price)
            if e >= EDGE and (best is None or e > best['edge']):
                best = {'side': side, 'price': price, 'fee': round(fee(price), 4), 'prob': round(p, 4), 'edge': round(e, 4)}
        if best:
            row['pick'] = best
        rows.append(row)
    return rows


def merge(old, new, now):
    """Rows for games that have tipped stay as they were at tip (that's what gets graded)."""
    tipped = lambda r: dt.datetime.fromisoformat(r['tip'].replace('Z', '+00:00')) <= now
    keep = [r for r in old if tipped(r)]
    seen = {r['ticker'] for r in keep}
    return keep + [r for r in new if r['ticker'] not in seen]


def actuals():
    logs = read(DATA / 'season_logs.json').get('game_logs') or []
    return {(str(r.get('date')), norm(r.get('name'))): r for r in logs}


def grade(today):
    acts = actuals()
    for f in sorted(OUT.glob('*.json')) if OUT.exists() else []:
        doc = read(f)
        if f.stem >= today or doc.get('graded') or not doc.get('rows'):
            continue
        done = 0
        for r in doc['rows']:
            a = acts.get((f.stem.replace('-', ''), norm(r['player'])))
            if not a:
                continue
            if (a.get('minutes') or 0) <= 0:
                r['result'] = 'void'          # didn't play: left out of the record
            else:
                r['actual'] = a.get(r['stat'])
                r['result'] = 'yes' if (r['actual'] or 0) > r['floor'] else 'no'
            done += 1
        if done and done >= 0.8 * len(doc['rows']):
            doc['graded'] = True
            f.write_text(json.dumps(doc, separators=(',', ':')) + '\n', encoding='utf-8')


def summarize(status):
    picks, b_fair, b_mid, b_ours, nights = [], [], [], [], 0
    for f in sorted(OUT.glob('*.json')) if OUT.exists() else []:
        doc = read(f)
        if not doc.get('graded'):
            continue
        nights += 1
        for r in doc['rows']:
            if r.get('result') not in ('yes', 'no'):
                continue
            y = 1 if r['result'] == 'yes' else 0
            b_fair.append((r['fair'] - y) ** 2)
            if r.get('mid') is not None:
                b_mid.append((r['mid'] - y) ** 2)
            if r.get('ours') is not None:
                b_ours.append((r['ours'] - y) ** 2)
            pk = r.get('pick')
            if pk:
                won = r['result'] == pk['side']
                cost = pk['price'] + pk['fee']
                picks.append({'date': f.stem, 'player': r['player'], 'title': r['title'], 'side': pk['side'], 'price': pk['price'],
                              'prob': pk['prob'], 'edge': pk['edge'], 'actual': r.get('actual'), 'result': 'win' if won else 'loss',
                              'units': round(((1 - cost) if won else -cost) / cost, 3)})
    w = sum(p['result'] == 'win' for p in picks)
    mean = lambda xs: round(sum(xs) / len(xs), 4) if xs else None
    out = {'schema': 1, 'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'), 'status': status,
           'paper_only': True, 'nights': nights,
           'picks': {'n': len(picks), 'wins': w, 'losses': len(picks) - w, 'units': round(sum(p['units'] for p in picks), 2),
                     'roi': round(sum(p['units'] for p in picks) / len(picks), 4) if picks else None,
                     'avg_edge': mean([p['edge'] for p in picks]), 'avg_price': mean([p['price'] for p in picks])},
           'brier': {'fanduel_fair': mean(b_fair), 'kalshi_mid': mean(b_mid), 'nba_edge': mean(b_ours), 'contracts': len(b_fair)},
           'recent_picks': picks[-40:],
           'method': f'Paper only. Kalshi NBA player-points contracts vs FanDuel no-vig points lines (fair chance at Kalshi\'s strike '
                     f'from a negative binomial fitted to FanDuel\'s line). Pick = buy YES or NO at Kalshi\'s ask when fair chance - ask - '
                     f'taker fee >= {int(EDGE * 100)} points; $1 risked per pick (fee included). An edge needs FanDuel fair to beat '
                     'Kalshi mid on Brier and picks to win.'}
    RESULTS.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    return out


def main():
    now = dt.datetime.now(dt.timezone.utc)
    day = now.astimezone(ET).date().isoformat()
    OUT.mkdir(exist_ok=True)
    grade(day)
    status = 'ok'
    path = OUT / f'{day}.json'
    doc = read(path) or {'date': day, 'rows': []}
    try:
        new = analyze(day, now)
        doc['rows'] = merge(doc.get('rows') or [], new, now)
        doc['updated_at'] = now.isoformat(timespec='seconds')
        path.write_text(json.dumps(doc, separators=(',', ':')) + '\n', encoding='utf-8')
        live = [r for r in new if r.get('pick')]
        print(f'kalshi: {len(new)} contracts matched to FanDuel lines, {len(live)} paper picks')
    except Exception as e:
        status = f'Kalshi fetch failed: {e}'[:200]
        print(f'::warning::{status}')
    s = summarize(status)
    print('kalshi record:', s['picks'], 'brier', s['brier'])


if __name__ == '__main__':
    main()
