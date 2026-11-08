"""One-off check of update_slate's slate discovery against today's DFF pages (writes nothing)."""
import sys, datetime as dt
from zoneinfo import ZoneInfo
sys.path.insert(0, 'scripts')
import update_slate as U
day = dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat()
page = U.fetch(f'https://www.dailyfantasyfuel.com/nba/projections/fanduel/{day}/')
try:
    U.parse(page, day)
    main_page = page
except ValueError as e:
    print('default page:', e)
    main_page, players, url = U.find_main(day, page)
singles, sides = U.scan_slates(day, main_page)
for s in singles:
    print('SINGLE', s['id'], s['label'], s.get('time'), len(s['players']), 'players', 'salary range', min(p['salary'] for p in s['players']), max(p['salary'] for p in s['players']))
for s in sides:
    print('SIDE', s['id'], s['label'], [(g['away'], g['home']) for g in s['games']], len(s['players']), 'players')
