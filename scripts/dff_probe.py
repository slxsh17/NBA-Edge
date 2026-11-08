"""One-off diagnostic: which FanDuel slates DFF has today and where their IDs are."""
import re, sys, time, datetime as dt
from zoneinfo import ZoneInfo
sys.path.insert(0, 'scripts')
from update_slate import fetch, page_var, next_id, prev_id, MAIN_URL, ROW
day = dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat()
page = fetch(f'https://www.dailyfantasyfuel.com/nba/projections/fanduel/{day}/')
print('default page: slate=%r url=%r start_date=%r team_count=%r rows=%s' % (page_var(page, 'slate'), page_var(page, 'url'), page_var(page, 'url_start_date'), page_var(page, 'team_count'), bool(ROW.search(page))))
print('window vars:', sorted(set(re.findall(r'window\.(\w+)\s*=', page)))[:60])
links = re.findall(r'[^\s"\'<>]{0,80}[?&]slate=([0-9A-Za-z]{3,10})[^\s"\'<>]{0,40}', page)
print('slate ids linked on page:', sorted(set(links)))
for m in list(re.finditer(r'slate=([0-9A-Za-z]{3,10})', page))[:40]:
    s = page[max(0, m.start() - 160):m.end() + 120].replace('\n', ' ')
    print('  ctx:', re.sub(r'\s+', ' ', s)[:300])
for k in ('slate-select', 'slate_select', 'data-slate', 'slates'):
    for m in list(re.finditer(k, page))[:5]:
        print('  %s ctx:' % k, re.sub(r'\s+', ' ', page[max(0, m.start() - 100):m.end() + 300])[:400])
start = page_var(page, 'url')
sid = start
for _ in range(60):
    sid = prev_id(sid)
for _ in range(140):
    try:
        pg = fetch(MAIN_URL.format(day=day, sid=sid))
    except Exception as e:
        pg = ''
    if pg and page_var(pg, 'url_start_date') == day:
        print('HIT', sid, page_var(pg, 'slate'), 'teams', page_var(pg, 'team_count'), 'rows', bool(ROW.search(pg)))
    time.sleep(0.5)
    sid = next_id(sid)
print('scanned', 140, 'ids from', start)
