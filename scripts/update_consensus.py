#!/usr/bin/env python3
"""NBA EDGE consensus radar -> data/public_consensus.json (+ daily copy in data/consensus/).

What other people are playing tonight, from public pages only:
  * Fantasy Team Advice: free projected ownership (FanDuel tab, main slate; top players
    at each position).
  * LineStar: free projected ownership for the players its logged-out view names.
  * DraftKings Network: today's public "top targets" and "value picks" articles; a
    player named in a pick article counts as a mention.
The ownership model blends these in by weights it learns against real ownership.
"""
import datetime as dt
import html
import json
import pathlib
import re
import unicodedata
import urllib.request
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36 NBA-EDGE-personal'}
ALIASES = {'GS': 'GSW', 'NO': 'NOP', 'NY': 'NYK', 'PHO': 'PHX', 'SA': 'SAS', 'UTAH': 'UTA', 'WSH': 'WAS', 'BRK': 'BKN'}


def canon(t):
    t = str(t or '').upper()
    return ALIASES.get(t, t)


def norm(s):
    return re.sub(r'[^a-z0-9]', '', unicodedata.normalize('NFKD', str(s or '')).lower())


def get(url, timeout=30):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read().decode('utf-8', 'replace')


def text(s):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', s))).strip()


def fta_fanduel(errors):
    """Fantasy Team Advice FanDuel panel: [(name, team, own%)] for its default (main) slate."""
    try:
        page = get('https://fantasyteamadvice.com/nba/dfs-ownership-projections')
    except Exception as e:
        errors.append(f'FantasyTeamAdvice: {e}')
        return [], None
    a = page.find('id="ownership-fd"')
    if a < 0:
        errors.append('FantasyTeamAdvice: FanDuel panel not found')
        return [], None
    b = page.find('role="tabpanel"', a + 30)
    panel = page[a:b if b > 0 else a + 200000]
    m = re.search(r'Showing:</span>\s*<span[^>]*>([^<]+)</span>', panel)
    label = text(m.group(1)) if m else None
    out = []
    for row in re.finditer(r'data-player-card-player-id-param="\d+"[^>]*>([^<]+)</a>(.{0,1500}?)>(\d+(?:\.\d+)?)%</span>', panel, re.S):
        team = re.search(r'</span></a>([A-Z]{2,4})</span>', row.group(2))
        out.append({'name': html.unescape(row.group(1)).strip(), 'team': canon(team.group(1)) if team else None, 'own': float(row.group(3))})
    return out, label


def linestar_projected(errors):
    """LineStar projected ownership for named players on today's main slate."""
    try:
        d = json.loads(get('https://www.linestarapp.com/DesktopModules/DailyFantasyApi/API/Fantasy/GetSalariesV5?sport=2&site=2&periodId=-1', 40))
    except Exception as e:
        errors.append(f'LineStar: {e}')
        return [], {}
    own = d.get('Ownership') or {}
    sc = json.loads(d.get('SalaryContainerJson') or '{}')
    named = {s['Id']: s for s in sc.get('Salaries', [])}
    groups = {}
    for sid, rows in (own.get('Projected') or {}).items():
        for r in rows:
            s = named.get(r['SalaryId'])
            if s:
                groups.setdefault(sid, []).append({'name': unicodedata.normalize('NFC', s['Name']), 'team': canon(s.get('PTEAM')), 'own': r['Owned']})
    # Each slate's numbers stay separate: the main slate is the one spanning the most teams;
    # two-team groups are single-game slates (ownership there totals 600%, not 900%).
    teams = {sid: {r['team'] for r in rows} for sid, rows in groups.items()}
    main = max(groups, key=lambda sid: (len(teams[sid]), len(groups[sid])), default=None)
    singles = {}
    for sid, rows in groups.items():
        if sid != main and len(teams[sid]) == 2:
            pair = '@'.join(sorted(teams[sid]))
            if len(rows) > len(singles.get(pair, [])):
                singles[pair] = rows
    return (groups.get(main, []) if main and len(teams[main]) > 2 else []), singles


def dk_articles(day, errors):
    """Today's DraftKings Network pick articles (targets, values, cheat sheets)."""
    try:
        page = get('https://dknetwork.draftkings.com/sport/nba/fantasy/')
    except Exception as e:
        errors.append(f'DK Network: {e}')
        return []
    mmdd = dt.date.fromisoformat(day).strftime('%-m-%-d-%y')
    links = sorted(set(re.findall(r'href="(https://dknetwork\.draftkings\.com/\d{4}/\d{2}/\d{2}/[^"]*(?:nba-dfs|fantasy-basketball)[^"]*' + re.escape(mmdd) + r'[^"]*)"', page)))
    arts = []
    for url in links[:6]:
        if 'pick6' in url:
            continue
        try:
            body = get(url)
        except Exception as e:
            errors.append(f'DK article: {e}')
            continue
        main = re.search(r'<article.*?</article>', body, re.S)
        arts.append({'url': url, 'title': text(re.search(r'<title>(.*?)</title>', body, re.S).group(1)) if '<title>' in body else url,
                     'text': text(main.group(0) if main else body)})
    return arts


def main():
    day = dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat()
    errors = []
    slate = {}
    try:
        slate = json.loads((DATA / f'slate_{day}.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        pass
    roster = {norm(p['name']) + '|' + p['team']: p for p in slate.get('players', [])}
    fta, fta_label = fta_fanduel(errors)
    ls, ls_singles = linestar_projected(errors)
    arts = dk_articles(day, errors)
    players = {}

    def slot(name, team):
        k = norm(name) + '|' + canon(team or '')
        if k not in roster:   # match by name only when team missing/odd
            hits = [kk for kk in roster if kk.split('|')[0] == norm(name)]
            k = hits[0] if len(hits) == 1 else k
        rec = players.setdefault(k, {'name': roster.get(k, {}).get('name', name), 'team': roster.get(k, {}).get('team', canon(team)),
                                     'fta_own': None, 'linestar_own': None, 'mentions': 0, 'articles': []})
        return rec
    for r in fta:
        slot(r['name'], r['team'])['fta_own'] = r['own']
    for r in ls:
        slot(r['name'], r['team'])['linestar_own'] = r['own']
    for a in arts:
        t = norm(a['text'])
        for k, p in roster.items():
            if norm(p['name']) in t:
                rec = slot(p['name'], p['team'])
                rec['mentions'] += 1
                rec['articles'].append({'title': a['title'][:140], 'url': a['url']})
    out_players = []
    for p in players.values():
        ext = [v for v in (p['fta_own'], p['linestar_own']) if v is not None]
        p['consensus_own'] = round(sum(ext) / len(ext), 1) if ext else None
        # fields the classic page reads
        p['source_count'] = (p['fta_own'] is not None) + (p['linestar_own'] is not None) + (1 if p['mentions'] else 0)
        p['mention_count'] = p['mentions']
        out_players.append(p)
    out_players.sort(key=lambda p: -((p['consensus_own'] or 0) + 5 * p['mentions']))
    doc = {'schema': 2, 'date': day, 'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
           'sources': {'fantasyteamadvice': {'players': len(fta), 'slate': fta_label}, 'linestar': {'players': len(ls), 'single_slates': len(ls_singles)},
                       'dknetwork_articles': [{'title': a['title'][:140], 'url': a['url']} for a in arts]},
           'source_errors': errors[:10], 'players': out_players, 'singles': ls_singles,
           'method': 'Public projected ownership (Fantasy Team Advice FanDuel tab, LineStar logged-out view) and player mentions in public DraftKings Network pick articles.'}
    if not out_players and not ls_singles and errors:
        print('every source failed; keeping the previous consensus file', errors[:3])
        return
    (DATA / 'public_consensus.json').write_text(json.dumps(doc, indent=0) + '\n', encoding='utf-8')
    (DATA / 'consensus').mkdir(exist_ok=True)
    (DATA / 'consensus' / f'{day}.json').write_text(json.dumps(doc, separators=(',', ':')) + '\n', encoding='utf-8')
    print(f"consensus {day}: FTA {len(fta)} ({fta_label}), LineStar {len(ls)}, DK articles {len(arts)}, players {len(out_players)}; errors {errors[:3]}")


if __name__ == '__main__':
    main()
