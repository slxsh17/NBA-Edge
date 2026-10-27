#!/usr/bin/env python3
"""NBA EDGE: publish today's FanDuel main-slate player pool automatically.

Source: DailyFantasyFuel's free public FanDuel NBA projections page, which lists
each main-slate player's FanDuel salary, position(s), team and injury tag.
Only roster facts are taken (name, team, position, salary, status) — not DFF's
projections. Writes data/slate_YYYY-MM-DD.csv in NBA EDGE's own CSV format; the
website already loads that file automatically. A manual CSV upload on the site
still overrides it (e.g. for early/late/turbo slates).
"""
import csv
import datetime as dt
import html
import io
import pathlib
import re
import sys
import urllib.request
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
ALIASES = {'GS': 'GSW', 'NO': 'NOP', 'NY': 'NYK', 'PHO': 'PHX', 'SA': 'SAS',
           'UTAH': 'UTA', 'WSH': 'WAS', 'BRK': 'BKN'}
STATUS = {'O': 'OUT', 'OUT': 'OUT', 'Q': 'QUESTIONABLE', 'GTD': 'QUESTIONABLE',
          'D': 'DOUBTFUL', 'P': 'ACTIVE', '': 'ACTIVE'}
POSITIONS = {'PG', 'SG', 'SF', 'PF', 'C'}
FIELDS = ['name', 'team', 'position', 'salary', 'minutes', 'fp_per_min', 'ownership',
          'matchup_factor', 'usage_factor', 'foul_lost_min', 'blowout_lost_min', 'ceiling_sd', 'status']
ROW = re.compile(r'<tr[^>]*class="[^"]*projections-listing[^"]*"[^>]*>', re.S)
ATTR = re.compile(r'(data-[a-z_]+)=\s*"([^"]*)"')


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


TEAM_NAMES = {'Atlanta Hawks': 'ATL', 'Boston Celtics': 'BOS', 'Brooklyn Nets': 'BKN', 'Charlotte Hornets': 'CHA',
    'Chicago Bulls': 'CHI', 'Cleveland Cavaliers': 'CLE', 'Dallas Mavericks': 'DAL', 'Denver Nuggets': 'DEN',
    'Detroit Pistons': 'DET', 'Golden State Warriors': 'GSW', 'Houston Rockets': 'HOU', 'Indiana Pacers': 'IND',
    'LA Clippers': 'LAC', 'Los Angeles Clippers': 'LAC', 'Los Angeles Lakers': 'LAL', 'Memphis Grizzlies': 'MEM',
    'Miami Heat': 'MIA', 'Milwaukee Bucks': 'MIL', 'Minnesota Timberwolves': 'MIN', 'New Orleans Pelicans': 'NOP',
    'New York Knicks': 'NYK', 'Oklahoma City Thunder': 'OKC', 'Orlando Magic': 'ORL', 'Philadelphia 76ers': 'PHI',
    'Phoenix Suns': 'PHX', 'Portland Trail Blazers': 'POR', 'Sacramento Kings': 'SAC', 'San Antonio Spurs': 'SAS',
    'Toronto Raptors': 'TOR', 'Utah Jazz': 'UTA', 'Washington Wizards': 'WAS'}


def game_times(day):
    """Tip-off times from data/games.json (published by the Daily Data Updates workflow)."""
    import json
    try:
        g = json.loads((DATA / 'games.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if str(g.get('date')) != day.replace('-', ''):
        return {}
    out = {}
    for x in g.get('games', []):
        a, h = TEAM_NAMES.get(x.get('away')), TEAM_NAMES.get(x.get('home'))
        if a and h:
            out[(a, h)] = x.get('time')
    return out


def build_games(players, day):
    times = game_times(day)
    games = {}
    for p in players:
        home, away = (p['team'], p['_opp']) if p['_home'] else (p['_opp'], p['team'])
        g = games.setdefault((away, home), {'away': away, 'home': home, 'time': times.get((away, home)),
                                            'total': None, 'home_spread': None})
        if p['_ou'] and not g['total']:
            g['total'] = p['_ou']
        if p['_spread'] is not None and g['home_spread'] is None:
            g['home_spread'] = p['_spread'] if p['_home'] else -p['_spread']
    return sorted(games.values(), key=lambda g: (g['time'] or '9', g['away']))


def fetch(url):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36 NBA-EDGE-personal',
        'Accept': 'text/html'})
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.read().decode('utf-8', 'replace')


def parse(page, day, strict_main=True, min_players=18, min_salary=3000):
    if strict_main and not re.search(r"window\.slate\s*=\s*'Main'", page):
        raise ValueError('page is not the Main slate')
    players, seen = [], set()
    for tag in ROW.findall(page):
        a = {k: html.unescape(v).strip() for k, v in ATTR.findall(tag)}
        if a.get('data-start_date') != day:
            continue
        name = a.get('data-name', '')
        team = a.get('data-team', '').upper()
        team = ALIASES.get(team, team)
        pos = [p for p in (a.get('data-pos', ''), a.get('data-pos_alt', '')) if p in POSITIONS]
        try:
            salary = int(float(a.get('data-salary', '0')))
        except ValueError:
            continue
        if not name or not team or not pos or not min_salary <= salary <= 25000:
            continue
        key = (name.lower(), team)
        if key in seen:
            continue
        seen.add(key)
        status = STATUS.get(a.get('data-inj', '').upper(), 'ACTIVE')
        opp = a.get('data-opp', '').upper()
        players.append({'_opp': ALIASES.get(opp, opp), '_home': a.get('data-loc', '') == 'vs',
                        '_ext': num(a.get('data-ppg_proj')),   # DFF's own public FanDuel projection (comparison only)
                        '_ou': num(a.get('data-ou')), '_spread': num(a.get('data-spread')),
                        'name': name, 'team': team, 'position': '/'.join(dict.fromkeys(pos)),
                        'salary': salary, 'minutes': 0, 'fp_per_min': 0, 'ownership': '',
                        'matchup_factor': 1, 'usage_factor': 1, 'foul_lost_min': 0,
                        'blowout_lost_min': 0, 'ceiling_sd': 8, 'status': status})
    teams = {p['team'] for p in players}
    if len(players) < min_players or len(teams) < 2:
        raise ValueError(f'only {len(players)} players / {len(teams)} teams found for {day}')
    return sorted(players, key=lambda p: -p['salary'])


SG_URL = 'https://www.dailyfantasyfuel.com/nba/showdown-single-game-projections/fanduel/{day}/?slate={sid}'


def b36(n):
    digits = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    out = ''
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out or '0'


def page_var(page, name):
    m = re.search(r"window\.%s\s*=\s*'([^']*)'" % name, page)
    return m.group(1) if m else ''


def single_games(day, main_page):
    """FanDuel single-game slates: DFF numbers each day's slates consecutively after the
    main slate. Scan forward, keep 2-team slates for this date, stop after 3 empty IDs."""
    import time
    start = page_var(main_page, 'url')
    if not start:
        return []
    found, misses, n = [], 0, int(start, 36) + 1
    while misses < 3 and n < int(start, 36) + 30:
        sid = b36(n)
        n += 1
        try:
            page = fetch(SG_URL.format(day=day, sid=sid))
        except Exception:
            misses += 1
            continue
        time.sleep(1)
        if page_var(page, 'url_start_date') != day:
            misses += 1
            continue
        misses = 0
        if page_var(page, 'team_count') != '2':
            continue                       # multi-game showdowns (Express, After Hours...)
        try:
            players = parse(page, day, strict_main=False, min_players=8, min_salary=500)
        except Exception as e:
            print(f'::warning::single game {sid}: {e}')
            continue
        games = build_games(players, day)
        if len(games) != 1:
            continue
        g = games[0]
        found.append({'id': sid, 'label': f"{g['away']} @ {g['home']}", **g,
                      'players': [{'name': p['name'], 'team': p['team'], 'opp': p['_opp'], 'home': p['_home'],
                                   'position': p['position'], 'salary': p['salary'], 'status': p['status'], 'ext_proj': p['_ext']} for p in players]})
    return sorted(found, key=lambda x: (x['time'] or '9', x['label']))


def write_singles(day, singles):
    import json
    doc = {'schema': 1, 'date': day, 'source': 'DailyFantasyFuel public FanDuel single-game pages',
           'fetched_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'), 'slates': singles}
    (DATA / f'single_{day}.json').write_text(json.dumps(doc, separators=(',', ':')) + '\n', encoding='utf-8')
    arch = DATA / 'archive'
    arch.mkdir(parents=True, exist_ok=True)
    doc.pop('fetched_at')
    (arch / f'single_{day}.json').write_text(json.dumps(doc, separators=(',', ':')) + '\n', encoding='utf-8')
    for pattern in ('single_*.json',):
        for old in sorted(DATA.glob(pattern))[:-7]:
            old.unlink()
        for old in sorted(arch.glob(pattern))[:-150]:
            old.unlink()


def backfill(day):
    """--date YYYY-MM-DD: archive a past slate (for backtests/learning). Doesn't touch today's files."""
    import json
    url = f'https://www.dailyfantasyfuel.com/nba/projections/fanduel/{day}/'
    try:
        page = fetch(url)
        players = parse(page, day, strict_main=False)
    except Exception as e:
        label = re.search(r"window\.slate\s*=\s*'([^']*)'", page).group(1) if 'page' in locals() and re.search(r"window\.slate\s*=\s*'([^']*)'", page) else '?'
        dates = sorted(set(re.findall(r'data-start_date="([^"]*)"', page)))[:5] if 'page' in locals() else []
        msg = f'{day}: {e} | slate label={label} | row dates={dates} | size={len(page) if "page" in locals() else 0}'
        print('::warning::' + msg)
        with (DATA / 'archive' / 'backfill_log.txt').open('a', encoding='utf-8') as f:
            f.write(msg + '\n')
        return 1
    games = build_games(players, day)
    write_archive(day, url, players, games)
    print(f'Archived {day}: {len(players)} players, {len(games)} games, {sum(g["total"] is not None for g in games)} with Vegas lines')
    return 0


def slate_doc(day, url, players, games):
    return {'schema': 1, 'date': day, 'slate': 'Main', 'source': 'DailyFantasyFuel public FanDuel page (salaries, positions, injury tags, Vegas lines)',
            'source_url': url, 'fetched_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'), 'games': games,
            'players': [{'name': p['name'], 'team': p['team'], 'opp': p['_opp'], 'home': p['_home'],
                         'position': p['position'], 'salary': p['salary'], 'status': p['status'], 'ext_proj': p['_ext']} for p in players]}


def write_archive(day, url, players, games):
    """Permanent copy of each day's slate so the learner can rebuild past projections."""
    import json
    arch = DATA / 'archive'
    arch.mkdir(parents=True, exist_ok=True)
    doc = slate_doc(day, url, players, games)
    doc.pop('fetched_at')
    (arch / f'slate_{day}.json').write_text(json.dumps(doc, separators=(',', ':')) + '\n', encoding='utf-8')
    for old in sorted(arch.glob('slate_*.json'))[:-150]:
        old.unlink()


def main():
    import json
    if '--date' in sys.argv:
        return max(backfill(d) for d in sys.argv[sys.argv.index('--date') + 1:] if re.match(r'\d{4}-\d{2}-\d{2}$', d))
    day = dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat()
    # The pipeline runs every 15 min; only re-read the source about hourly.
    try:
        prev = json.loads((DATA / f'slate_{day}.json').read_text(encoding='utf-8'))
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(prev['fetched_at'])
        if age < dt.timedelta(minutes=50) and '--force' not in sys.argv:
            print(f'Slate fetched {int(age.total_seconds() // 60)} min ago; skipping')
            return 0
    except (OSError, ValueError, KeyError):
        pass
    url = f'https://www.dailyfantasyfuel.com/nba/projections/fanduel/{day}/'
    try:
        main_page = fetch(url)
        players = parse(main_page, day)
    except Exception as e:  # keep whatever is already published; never write a partial slate
        # Fail the workflow (red X in GitHub Actions) so a broken source is visible.
        print(f'::error::No slate published for {day}: {e}')
        return 1
    games = build_games(players, day)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator='\n', extrasaction='ignore')
    w.writeheader()
    w.writerows(players)
    DATA.mkdir(parents=True, exist_ok=True)
    out = DATA / f'slate_{day}.csv'   # legacy page format
    out.write_text(buf.getvalue(), encoding='utf-8')
    slate = slate_doc(day, url, players, games)
    write_archive(day, url, players, games)
    (DATA / f'slate_{day}.json').write_text(json.dumps(slate, separators=(',', ':')) + '\n', encoding='utf-8')
    # Keep the repo tidy: only the last 7 daily slates.
    for pattern in ('slate_*.csv', 'slate_*.json'):
        for old in sorted(DATA.glob(pattern))[:-7]:
            old.unlink()
    try:
        singles = single_games(day, main_page)
        write_singles(day, singles)
        print(f'Single-game slates: {", ".join(x["label"] for x in singles) or "none"}')
    except Exception as e:
        print(f'::warning::single-game discovery failed: {e}')
    outs = sum(p['status'] == 'OUT' for p in players)
    print(f'Published {out.name}: {len(players)} players, {len({p["team"] for p in players})} teams, {outs} OUT')
    return 0


if __name__ == '__main__':
    sys.exit(main())
