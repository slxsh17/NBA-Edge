#!/usr/bin/env python3
"""NBA EDGE · DraftKings: threes made (fg3m) for older box scores.

Box scores saved before threes were recorded lack fg3m, which DK scoring needs. Re-reads those
games' ESPN box scores (up to MAX_EVENTS per run) and stores the threes in data/dk/threes.json
({event_id: {player_id: fg3m}}). season_logs.json itself is never modified, so this can't
collide with the FanDuel pipeline's commits; dk_common.load_logs() merges the two.
"""
import json
import sys
import time

import update_player_stats as UPS
from dk_common import DK, THREES, load, load_logs

MAX_EVENTS = int(sys.argv[1]) if len(sys.argv) > 1 else 250


def main():
    store = load(THREES, {}) or {}
    todo = {}
    for r in load_logs():
        if r.get('fg3m') is None and r.get('event_id'):
            todo.setdefault(str(r['event_id']), r.get('date'))
    print(f'{len(todo)} games need threes made')
    done = errors = 0
    for eid in sorted(todo)[:MAX_EVENTS]:
        try:
            rows = UPS.parse_summary(UPS.fetch_json(UPS.BASE + 'summary?event=' + eid), eid, todo[eid])
        except Exception as e:
            errors += 1
            print(f'::warning::{eid}: {e}')
            continue
        threes = {r['id']: r['fg3m'] for r in rows if r.get('fg3m') is not None}
        if threes:
            store[eid] = threes
            done += 1
        time.sleep(0.15)
    if done:
        DK.mkdir(parents=True, exist_ok=True)
        THREES.write_text(json.dumps(store, separators=(',', ':'), sort_keys=True) + '\n', encoding='utf-8')
    print(f'filled {done} games, {errors} errors')


if __name__ == '__main__':
    main()
