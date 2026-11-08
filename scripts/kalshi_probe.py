"""One-off: how Kalshi lists NBA markets (series, events, market fields)."""
import json, urllib.request, urllib.parse, time
B = 'https://api.elections.kalshi.com/trade-api/v2'
def get(path, **q):
    url = B + path + ('?' + urllib.parse.urlencode(q) if q else '')
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'NBA-EDGE-personal'}), timeout=30) as r:
        return json.loads(r.read())
s = get('/series', category='Sports')
ser = [x for x in s.get('series', []) if 'NBA' in (x.get('ticker', '') + x.get('title', '')).upper() and 'WNBA' not in x.get('ticker','').upper()]
print('NBA series', len(ser))
for x in ser[:60]:
    print('SERIES', x.get('ticker'), '|', x.get('title'), '|', x.get('frequency'), '|', x.get('tags'))
for tk in [x['ticker'] for x in ser][:60]:
    try:
        m = get('/markets', series_ticker=tk, status='open', limit=200)
    except Exception as e:
        print('fail', tk, e); continue
    ms = m.get('markets', [])
    if not ms: continue
    print(f'== {tk}: {len(ms)} open markets')
    for x in ms[:3]:
        keep = {k: x.get(k) for k in ('ticker','event_ticker','title','subtitle','yes_sub_title','no_sub_title','yes_bid','yes_ask','no_bid','no_ask','yes_bid_dollars','yes_ask_dollars','no_ask_dollars','last_price','volume','volume_24h','open_interest','liquidity','close_time','expected_expiration_time','strike_type','floor_strike','cap_strike','custom_strike','rules_primary')}
        print('  M', json.dumps(keep)[:900])
    time.sleep(0.3)
