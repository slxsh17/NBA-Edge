#!/usr/bin/env python3
"""NBA EDGE · DraftKings (paper mode): fetch today's DK main-slate player pool.

Same public source as the FanDuel pool (DailyFantasyFuel), DraftKings pages. Only roster facts
are kept (name, team, positions, salary, injury tag). Writes data/dk/slate_YYYY-MM-DD.json.
Does not touch any FanDuel file.
"""
import datetime as dt
import json
import re
import sys
import time
from zoneinfo import ZoneInfo

import update_slate as US
from dk_common import DK

URL = 'https://www.dailyfantasyfuel.com/nba/projections/draftkings/{day}/'
SLATE_URL = 'https://www.dailyfantasyfuel.com/nba/projections/draftkings/{day}/?slate={sid}'


def find_main(day):
    page = US.fetch(URL.format(day=day))
    try:
        return US.parse(page, day), URL.format(day=day)
    except ValueError as first:
        start = US.page_var(page, 'url')
        cands = [s for s in re.findall(r'[?&]slate=([0-9A-Za-z]{4,8})', page) if s != start]
        if start:
            f = b = start
            for _ in range(8):
                f, b = US.next_id(f), US.prev_id(b)
                cands += [f, b]
        for sid in dict.fromkeys(cands):
            try:
                pg = US.fetch(SLATE_URL.format(day=day, sid=sid))
                time.sleep(1)
                return US.parse(pg, day), SLATE_URL.format(day=day, sid=sid)
            except Exception:
                continue
        raise first


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
