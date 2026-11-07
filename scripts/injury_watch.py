#!/usr/bin/env python3
"""Game-night injury watch: rebuild the site the minute injury news changes.

GitHub runs scheduled jobs late (the "every 10 minutes" injury job really runs every
40-60 minutes), so one long job stays up through the evening instead:
  * every minute: ESPN's injury feed,
  * every 5 minutes (from 4 hours before the first tip): RotoWire starters + news headlines,
  * when the news no longer matches what today.json was built from (its sources.news_sig):
    rebuild today.json, run the late-news alerts (phone push + your entries' swaps), push.
Projections reach the site about a minute after the push (Pages deploy).
It stops 10 minutes after the last tip, after 5h40m (the next scheduled run takes over),
or right away on days with no games.
"""
import datetime as dt
import json
import pathlib
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import build_slate as BS  # noqa: E402

POLL = 60
NEWS_EVERY = 300
NEWS_FROM = 4 * 3600       # starters/headlines from 4 hours before the first tip
MAX_RUN = 5 * 3600 + 40 * 60
AFTER_LAST = 10 * 60
COMMIT = ['data/today.json', 'data/injuries.json', 'data/lineup_news.json', 'data/alerts.json']


def sh(*cmd, check=False, timeout=600):
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(cmd)} failed: {(r.stderr or r.stdout)[-400:]}")
    return r


def py(script, timeout=600):
    r = sh(sys.executable, f'scripts/{script}', timeout=timeout)
    if r.returncode:
        print(f'::warning::{script} failed: {(r.stderr or r.stdout)[-300:]}')
    return r.returncode == 0


def sync():
    """Start every check from the latest published data (the pipeline pushes too)."""
    sh('git', 'fetch', '-q', 'origin', 'main', check=True)
    sh('git', 'reset', '-q', '--hard', 'origin/main', check=True)


def tips(today):
    out = []
    for g in today.get('games', []):
        try:
            out.append(dt.datetime.fromisoformat(str(g.get('time')).replace('Z', '+00:00')))
        except ValueError:
            pass
    return sorted(out)


def publish(why):
    paths = [p for p in COMMIT if (ROOT / p).exists()]
    sh('git', 'add', '--', *paths)
    if sh('git', 'diff', '--cached', '--quiet').returncode == 0:
        return True
    sh('git', 'commit', '-q', '-m', f'Injury watch: {why}', check=True)
    for _ in range(3):
        if sh('git', 'push', '-q', 'origin', 'HEAD:main').returncode == 0:
            return True
        # Someone else pushed first: the next loop syncs and rebuilds on top of theirs.
        time.sleep(3)
    return False


def main():
    start = time.time()
    last_news = 0.0
    tried = None   # news fingerprint we last rebuilt for (a rebuild that changed nothing isn't retried)
    sh('git', 'config', 'user.name', 'github-actions[bot]')
    sh('git', 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    while time.time() - start < MAX_RUN:
        loop = time.time()
        try:
            sync()
            today = BS.read('today.json')
            now = dt.datetime.now(dt.timezone.utc)
            T = tips(today)
            et = now.astimezone(ZoneInfo('America/New_York'))
            if not T or today.get('date') != et.date().isoformat():
                if et.hour >= 20:
                    print('No games today; nothing to watch.')
                    return
                print(f"{now:%H:%M}Z today's slate isn't built yet; checking again in 10 minutes")
                time.sleep(600)
                continue
            if now > T[-1] + dt.timedelta(seconds=AFTER_LAST):
                print('Every game has started; done for the night.')
                return
            py('update_injuries.py', timeout=90)
            if time.time() - last_news >= NEWS_EVERY and now >= T[0] - dt.timedelta(seconds=NEWS_FROM):
                py('update_lineup_news.py', timeout=150)
                last_news = time.time()
            teams = {p['team'] for p in today.get('players', [])}
            sig = BS.news_sig(today['date'], teams)
            pub = (today.get('sources') or {}).get('news_sig')
            if sig != pub and sig != tried:
                t0 = time.time()
                py('build_slate.py')
                py('alerts.py', timeout=300)
                tried = sig
                ok = publish('news changed')
                print(f'{now:%H:%M:%S}Z news changed ({pub} -> {sig}); rebuilt in {time.time() - t0:.0f}s, '
                      f"{'pushed' if ok else 'push failed, retrying next loop'}")
                if not ok:
                    tried = None
        except Exception as e:  # keep watching; one bad loop shouldn't end the night
            print(f'::warning::watch loop: {e}')
        time.sleep(max(5, POLL - (time.time() - loop)))
    print('Time limit reached; the next scheduled run takes over.')


if __name__ == '__main__':
    main()
