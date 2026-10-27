#!/usr/bin/env python3
"""NBA EDGE: player news from X -> data/x_news.json.

Reads NEW posts from a short list of fast NBA news accounts (data/x_accounts.txt) with
the official X API (pay-per-use: you pay per post read). `since_id` makes every post
read only once, so cost tracks how much the accounts post, not how often this runs.
Needs the repository secret X_BEARER_TOKEN; without it the script does nothing.
Posts become "check this" flags on matched players; they never change minutes alone.
"""
import datetime as dt
import json
import os
import pathlib
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
OUT = DATA / 'x_news.json'
ACTIVE_HOURS = range(11, 24)   # Eastern; NBA news that matters for DFS lands in this window
MAX_PER_RUN = 60               # hard ceiling on posts read per run (cost guard)


def accounts():
    p = DATA / 'x_accounts.txt'
    if not p.exists():
        return ['UnderdogNBA', 'ShamsCharania']
    return [ln.strip().lstrip('@') for ln in p.read_text(encoding='utf-8').splitlines() if ln.strip() and not ln.startswith('#')]


def main():
    token = os.environ.get('X_BEARER_TOKEN', '').strip()
    if not token:
        print('X_BEARER_TOKEN not set; skipping X news')
        return
    now = dt.datetime.now(ZoneInfo('America/New_York'))
    if now.hour not in ACTIVE_HOURS:
        print('Outside news hours; skipping')
        return
    old = json.loads(OUT.read_text(encoding='utf-8')) if OUT.exists() else {'posts': [], 'since_id': None}
    q = '(' + ' OR '.join(f'from:{a}' for a in accounts()) + ') -is:retweet -is:reply'
    params = {'query': q, 'max_results': str(min(100, max(10, MAX_PER_RUN))),
              'tweet.fields': 'created_at,author_id', 'expansions': 'author_id', 'user.fields': 'username'}
    if old.get('since_id'):
        params['since_id'] = old['since_id']
    else:
        params['start_time'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=6)).strftime('%Y-%m-%dT%H:%M:%SZ')
    url = 'https://api.x.com/2/tweets/search/recent?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}', 'User-Agent': 'NBA-EDGE'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode('utf-8'))
    except Exception as e:
        print(f'::warning::X API: {e}')
        return
    users = {u['id']: u['username'] for u in (d.get('includes') or {}).get('users', [])}
    new = [{'id': t['id'], 'text': t['text'], 'at': t.get('created_at'), 'account': users.get(t.get('author_id'), ''),
            'url': f"https://x.com/{users.get(t.get('author_id'), 'i')}/status/{t['id']}"} for t in d.get('data', [])]
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=30)).isoformat()
    posts = {p['id']: p for p in old.get('posts', []) + new if (p.get('at') or '') >= cutoff}
    since = (d.get('meta') or {}).get('newest_id') or old.get('since_id')
    out = {'schema': 1, 'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'), 'since_id': since,
           'accounts': accounts(), 'read_this_run': len(new),
           'posts': sorted(posts.values(), key=lambda p: p.get('at') or '', reverse=True)}
    OUT.write_text(json.dumps(out, indent=0) + '\n', encoding='utf-8')
    print(f'X news: {len(new)} new posts read, {len(out["posts"])} kept')


if __name__ == '__main__':
    main()
