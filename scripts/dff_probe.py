"""One-off diagnostic: DFF's own slate-list feed for today."""
import json, sys, urllib.request, datetime as dt
from zoneinfo import ZoneInfo
day = dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat()
for url in (f'https://www.dailyfantasyfuel.com/data/slates/recent/nba/fanduel?date={day}&url=214D3',
            f'https://www.dailyfantasyfuel.com/data/slates/recent/nba/fanduel?date={day}'):
    try:
        raw = urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 NBA-EDGE-personal', 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}), timeout=30).read().decode('utf-8', 'replace')
    except Exception as e:
        print('fail', url, e); continue
    print('URL', url, len(raw))
    try:
        d = json.loads(raw)
    except ValueError:
        print(raw[:500]); continue
    print('keys', list(d))
    print('curr', json.dumps(d.get('curr_slate'))[:400])
    print('dates', json.dumps(d.get('dates'))[:300])
    for s in d.get('slates') or []:
        print('SLATE', json.dumps(s)[:400])
