#!/usr/bin/env python3
"""Keep every 2026-27 box score: player_stats.json only holds the last 7 days, so
merge it into data/season_logs.json (deduplicated by player + game)."""
import json
import pathlib

DATA = pathlib.Path(__file__).resolve().parent.parent / 'data'


def main():
    path = DATA / 'season_logs.json'
    old = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'season': '2026-27', 'game_logs': []}
    new = json.loads((DATA / 'player_stats.json').read_text(encoding='utf-8')).get('game_logs', [])
    logs = {(str(r.get('id') or r.get('name')), str(r.get('event_id') or r.get('date'))): r for r in old['game_logs']}
    before = len(logs)
    for r in new:
        if str(r.get('date', '')) >= '20261020':
            logs[(str(r.get('id') or r.get('name')), str(r.get('event_id') or r.get('date')))] = r
    if len(logs) == before and path.exists():
        print('season_logs.json unchanged', before)
        return
    out = {'season': '2026-27', 'source': 'ESPN box scores accumulated from data/player_stats.json',
           'game_logs': sorted(logs.values(), key=lambda r: (str(r.get('date')), str(r.get('id'))))}
    path.write_text(json.dumps(out, separators=(',', ':')) + '\n', encoding='utf-8')
    print('season_logs.json', len(logs), 'games (+%d)' % (len(logs) - before))


if __name__ == '__main__':
    main()
