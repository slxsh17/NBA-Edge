"""One-off diagnostic: find the feed DFF's page uses to list a day's slates."""
import re, sys, json, datetime as dt
from zoneinfo import ZoneInfo
sys.path.insert(0, 'scripts')
from update_slate import fetch
day = dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat()
page = fetch(f'https://www.dailyfantasyfuel.com/nba/projections/fanduel/{day}/')
srcs = re.findall(r'<script[^>]+src="([^"]+)"', page)
print('script srcs:', srcs)
for m in re.finditer(r'(ajax|fetch\(|getJSON|\$\.get|/api/|/data/|\.json|slates?_?(?:url|list|data))', page, re.I):
    print('  page ctx:', re.sub(r'\s+', ' ', page[max(0, m.start() - 120):m.end() + 200])[:330])
for s in srcs:
    if 'dailyfantasyfuel' not in s and not s.startswith('/'):
        continue
    url = s if s.startswith('http') else 'https://www.dailyfantasyfuel.com' + s
    try:
        js = fetch(url)
    except Exception as e:
        print('fail', url, e); continue
    print('JS', url, len(js))
    for m in re.finditer(r'all-slates|showdown-slates|slates', js):
        print('  js ctx:', re.sub(r'\s+', ' ', js[max(0, m.start() - 250):m.end() + 250])[:520])
