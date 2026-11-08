#!/usr/bin/env python3
"""NBA EDGE · DraftKings (paper mode): fetch today's DK main-slate player pool.

Same public source as the FanDuel pool (DailyFantasyFuel), DraftKings pages. Only roster facts
are kept (name, team, positions, salary, injury tag). Writes data/dk/slate_YYYY-MM-DD.json.
Does not touch any FanDuel file.
"""
import datetime as dt
import json
import sys
import time
import urllib.request
from zoneinfo import ZoneInfo

import update_slate as US
from dk_common import DK

URL = 'https://www.dailyfantasyfuel.com/nba/projections/draftkings/{day}/'
SLATE_URL = 'https://www.dailyfantasyfuel.com/nba/projections/draftkings/{day}/?slate={sid}'


def slate_list(day, sid=''):
    """DraftKings slates DFF lists for the day (same feed its slate picker uses)."""
    url = f'https://www.dailyfantasyfuel.com/data/slates/recent/nba/draftkings?date={day}' + (f'&url={sid}' if sid else '')
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) NBA-EDGE-personal',
                                                   'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'})
        with urllib.request.urlopen(req, timeout=30) as res:
            doc = json.loads(res.read().decode('utf-8', 'replace'))
    except Exception as e:
        print(f'::warning::DFF DraftKings slate list unavailable ({e})')
        return []
    out = [x for x in doc.get('slates') or [] if x.get('url')]
    print('DK slates listed:', [(x.get('url'), x.get('slate_type'), x.get('game_count')) for x in out][:12])
    return out


def find_main(day):
    """DFF labels the DraftKings main slate 'Main' or leaves it blank; showdowns and the
    Early/Turbo/Night slates have their own labels. Pick the main-type slate with the most games."""
    page = US.fetch(URL.format(day=day))
    start = US.page_var(page, 'url')
    listed = slate_list(day, start)
    mains = sorted((x for x in listed if (x.get('slate_type') or '') in ('', 'Main') and not x.get('showdown_flag')),
                   key=lambda x: -int(x.get('game_count') or 0))
    if not listed and (US.page_var(page, 'slate') or '') in ('', 'Main'):
        return US.parse(page, day, strict_main=False), URL.format(day=day)
    for x in mains:
        if x['url'] == start:
            return US.parse(page, day, strict_main=False), URL.format(day=day)
        try:
            pg = US.fetch(SLATE_URL.format(day=day, sid=x['url']))
            time.sleep(1)
            return US.parse(pg, day, strict_main=False), SLATE_URL.format(day=day, sid=x['url'])
        except Exception as e:
            print(f"::warning::DK slate {x['url']}: {e}")
    raise ValueError(f'no DraftKings main slate among {[(x.get("url"), x.get("slate_type")) for x in listed]}')


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat()
    try:
        players, url = find_main(day)
    except Exception as e:
        print(f'::warning::No DraftKings main slate for {day}: {e}')
        return
    teams = sorted({p['team'] for p in players})
    games = sorted({'@'.join(sorted((p['team'], p['_opp']))) for p in players if p.get('_opp')})
    doc = {'schema': 1, 'site': 'draftkings', 'date': day, 'source_url': url,
           'fetched_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
           'teams': teams, 'games': games,
           'players': [{'name': p['name'], 'team': p['team'], 'opp': p.get('_opp', ''), 'position': p['position'],
                        'salary': p['salary'], 'status': p['status'], 'dff_proj': p.get('_ext')} for p in players]}
    DK.mkdir(parents=True, exist_ok=True)
    out = DK / f'slate_{day}.json'
    out.write_text(json.dumps(doc, indent=1) + '\n', encoding='utf-8')
    print(f'DK slate {day}: {len(players)} players, {len(games)} games -> {out}')


if __name__ == '__main__':
    main()
