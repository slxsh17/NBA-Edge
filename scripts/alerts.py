#!/usr/bin/env python3
"""NBA EDGE late-news alerts -> data/alerts.json (+ phone push via ntfy).

After each projection rebuild, compare with the previous run and alert on:
  * a player newly ruled OUT/DOUBTFUL who mattered (projected 15+), with the teammates
    whose minutes went up (the beneficiaries),
  * a new minutes-restriction / bench headline on a player projected 15+,
  * a newly confirmed starter who picked up minutes.
News within LATE_WINDOW minutes of tip-off marks the beneficiaries as late-news leverage
(most of the field has built by then); build_slate.py reads that from alerts.json.
Push: set the repository secret NTFY_TOPIC (subscribe to the same topic in the ntfy app).
"""
import datetime as dt
import json
import os
import pathlib
import urllib.request
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
SITE = 'https://slxsh17.github.io/NBA-Edge/'
LATE_WINDOW = 120   # minutes before tip
ET = ZoneInfo('America/New_York')


def read(p, default=None):
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {} if default is None else default


def push(topic, title, body, tags):
    req = urllib.request.Request(f'https://ntfy.sh/{topic}', data=body.encode('utf-8'), method='POST',
                                 headers={'Title': title.encode('ascii', 'ignore').decode() or 'NBA EDGE', 'Tags': tags,
                                          'Priority': 'high', 'Click': SITE})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def main():
    today = read(DATA / 'today.json')
    if not today.get('players'):
        return
    now = dt.datetime.now(dt.timezone.utc)
    day = today['date']
    state = read(DATA / 'alerts.json', {'date': day, 'players': {}, 'alerts': [], 'late_news': []})
    if state.get('date') != day:
        state = {'date': day, 'players': {}, 'alerts': [], 'late_news': []}
    tips = {}
    for g in today.get('games', []) + today.get('single_games', []):
        if g.get('time'):
            t = dt.datetime.fromisoformat(g['time'].replace('Z', '+00:00'))
            tips[g['home']] = tips[g['away']] = (t, f"{g['away']} @ {g['home']}")
    first_run = not state['players']
    prev = state['players']
    cur = {}
    by_team = {}
    for p in today['players']:
        k = p['name'] + '|' + p['team']
        news_kinds = sorted({kd for n in p.get('news', []) for kd in n.get('kinds', [])})
        cur[k] = {'out': bool(p.get('out')), 'status': p.get('status'), 'inj': round(p.get('adj', {}).get('injury_min', 0), 1),
                  'starter': bool(p.get('starter')), 'proj': round(p.get('model_proj', p.get('proj', 0)) or 0), 'news': news_kinds}
        by_team.setdefault(p['team'], []).append(p)
    new_alerts = []
    sent = {a['id'] for a in state['alerts']}
    for p in today['players']:
        k = p['name'] + '|' + p['team']
        c, o = cur[k], prev.get(k)
        tip, label = tips.get(p['team'], (None, ''))
        if first_run or o is None or (tip and now >= tip):
            continue
        mins_to_tip = (tip - now).total_seconds() / 60 if tip else None
        when = tip.astimezone(ET).strftime('%-I:%M %p') if tip else ''
        if c['out'] and not o['out'] and o['proj'] >= 15:
            bens = []
            for q in by_team.get(p['team'], []):
                qk = q['name'] + '|' + q['team']
                gain = cur[qk]['inj'] - (prev.get(qk) or {}).get('inj', 0)
                if not q.get('out') and gain >= 1.5:
                    bens.append((gain, q))
            bens.sort(key=lambda x: -x[0])
            ben_txt = '; '.join(f"{q['name']} +{g:.1f} min, {q['proj']:.1f} proj, ~{q.get('own', 0):.0f}% owned" for g, q in bens[:4]) or 'no clear minutes winner yet'
            late = mins_to_tip is not None and mins_to_tip <= LATE_WINDOW
            new_alerts.append({'id': f'{day}|{k}|out', 'kind': 'out', 'late': late, 'player': p['name'], 'team': p['team'],
                               'title': f"{p['name']} {c['status'].lower()} ({label} {when})",
                               'body': f"Projected {o['proj']:.0f} before. Who gains: {ben_txt}." + (' Late news: most of the field already built.' if late else ''),
                               'beneficiaries': [q['name'] + '|' + q['team'] for _, q in bens[:4]]})
        for kind, word in (('restriction', 'minutes restriction'), ('bench', 'move to the bench')):
            if kind in c['news'] and kind not in o['news'] and c['proj'] >= 15:
                head = next((n['headline'] for n in p.get('news', []) if kind in n.get('kinds', [])), '')
                new_alerts.append({'id': f'{day}|{k}|{kind}', 'kind': kind, 'late': False, 'player': p['name'], 'team': p['team'],
                                   'title': f"{p['name']}: possible {word} ({label} {when})", 'body': head[:200]})
        if c['starter'] and not o['starter'] and p.get('adj', {}).get('starter_min', 0) >= 3:
            new_alerts.append({'id': f'{day}|{k}|starter', 'kind': 'starter', 'late': mins_to_tip is not None and mins_to_tip <= LATE_WINDOW,
                               'player': p['name'], 'team': p['team'],
                               'title': f"{p['name']} confirmed starting ({label} {when})",
                               'body': f"Now {p['proj']:.1f} proj (+{p['adj']['starter_min']:.1f} min), ~{p.get('own', 0):.0f}% owned, {p['salary']:,} salary."})
    topic = os.environ.get('NTFY_TOPIC', '').strip()
    for a in new_alerts:
        if a['id'] in sent:
            continue
        a['at'] = now.isoformat(timespec='seconds')
        state['alerts'].append(a)
        if a.get('late'):
            for b in a.get('beneficiaries', []):
                state['late_news'].append({'key': b, 'because': a['player'], 'at': a['at']})
            if a['kind'] == 'starter':
                state['late_news'].append({'key': a['player'] + '|' + a['team'], 'because': 'confirmed start', 'at': a['at']})
        if topic:
            try:
                push(topic, a['title'], a['body'], 'rotating_light' if a['kind'] == 'out' else 'basketball')
                a['pushed'] = True
            except Exception as e:
                print(f'::warning::ntfy push failed: {e}')
        print('ALERT', a['title'], '|', a['body'])
    state['players'] = cur
    state['updated_at'] = now.isoformat(timespec='seconds')
    old = read(DATA / 'alerts.json')
    if new_alerts or old.get('date') != day or not old.get('players') or old.get('players') != cur:
        (DATA / 'alerts.json').write_text(json.dumps(state, separators=(',', ':')) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
