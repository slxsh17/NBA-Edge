#!/usr/bin/env python3
"""Health alarms: a phone push (ntfy, same topic as the late-news alerts) when NBA EDGE is missing
a slate, its data stops refreshing, or the live site falls behind the repo. Nothing here fixes
projections; it makes failures visible within minutes instead of when you open the app.

Used by update_slate.py (slate coverage, every pipeline run) and injury_watch.py (staleness and
the live site, every few minutes on game nights). Each problem pushes once per day: update_slate
keeps that in data/health.json (committed by the pipeline); the watcher keeps it in memory.
"""
import datetime as dt
import json
import os
import pathlib
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
STATE = DATA / 'health.json'
SITE = 'https://slxsh17.github.io/NBA-Edge/'
_sent = set()


def topic():
    return os.environ.get('NTFY_TOPIC', '').strip() or 'nbaedge'


def push(title, body):
    req = urllib.request.Request(f'https://ntfy.sh/{topic()}', data=body.encode('utf-8'), method='POST',
                                 headers={'Title': title.encode('ascii', 'ignore').decode() or 'NBA EDGE',
                                          'Tags': 'warning', 'Priority': 'high', 'Click': SITE})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def alarm(day, key, title, body, persist=True):
    """Push once per (day, key). Returns True if it pushed."""
    print(f'::warning::{title}: {body}')
    if (day, key) in _sent:
        return False
    st = {}
    if persist:
        try:
            st = json.loads(STATE.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            st = {}
        if key in (st.get('sent') or {}).get(day, {}):
            _sent.add((day, key))
            return False
    try:
        push(title, body)
    except Exception as e:
        print(f'::warning::health push failed: {e}')
        return False
    _sent.add((day, key))
    if persist:
        sent = {day: {**(st.get('sent') or {}).get(day, {}), key: dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}}
        STATE.write_text(json.dumps({'schema': 1, 'sent': sent}, indent=1) + '\n', encoding='utf-8')
    return True


def slate_coverage(day, listed, main_id, singles, sides, main_games, espn_games):
    """After slate discovery: every slate DFF lists should be published, and every game on ESPN's
    schedule should be in at least one slate. Returns the problems found."""
    problems = []
    if listed is None:
        problems.append(('slatelist', "Couldn't read DFF's slate list",
                         'Fell back to scanning slate IDs, which can miss slates. Check the slate picker before entering.'))
    else:
        got = {x['id'] for x in singles} | {x['id'] for x in sides} | {main_id}
        miss = [x for x in listed if x.get('url') not in got]
        if miss:
            names = ', '.join(f"{x.get('slate_type')} {x.get('start_hhmm')}" for x in miss)
            problems.append(('slates:' + ','.join(sorted(x['url'] for x in miss)), f'NBA EDGE is missing {len(miss)} slate{"s" if len(miss) > 1 else ""}',
                             f'DFF lists {names} but they did not load. Main and the others are fine; use "Use my CSV" for these until fixed.'))
    covered = set(main_games) | {g for s in sides for g in ((x['away'], x['home']) for x in s.get('games', []))} | \
              {(s['away'], s['home']) for s in singles if s.get('away')}
    left = [g for g in espn_games if g not in covered]
    if espn_games and left:
        problems.append(('games:' + ','.join(f'{a}@{h}' for a, h in sorted(left)), f'{len(left)} game{"s" if len(left) > 1 else ""} not in any slate',
                         ', '.join(f'{a} @ {h}' for a, h in sorted(left)) + " are on ESPN's schedule but in no published slate."))
    for key, title, body in problems:
        alarm(day, key, title, body)
    return problems


def live_built_at():
    """built_at of today.json on the live site (GitHub Pages), or None."""
    try:
        req = urllib.request.Request(SITE + f'data/today.json?h={int(dt.datetime.now().timestamp())}', headers={'User-Agent': 'NBA-EDGE-health'})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode('utf-8')).get('built_at')
    except Exception as e:
        print(f'::warning::live site check failed: {e}')
        return None
