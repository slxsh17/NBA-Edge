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

Your entries: when you save lineups on the site ("I entered this" / "I entered all"), the site
posts them to the ntfy topic nbaedge-entries. Each run reads the latest snapshot (or
data/my_entries.json) and, if a player in one of your entries is ruled out before his game
starts, pushes the best single swap for that slot: highest projection that fits the salary
left, can play the slot, and whose game hasn't started.
"""
import datetime as dt
import json
import os
import pathlib
import re
import sys
import unicodedata
import urllib.request
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import field_sim as FS  # noqa: E402

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


ENTRY_TOPIC = os.environ.get('NTFY_ENTRIES_TOPIC', '').strip() or 'nbaedge-entries'
CAP = 60000


def norm(s):
    s = unicodedata.normalize('NFKD', str(s or '')).lower().strip()
    s = re.sub(r'[\s,]+(jr|sr|ii|iii|iv|v)\.?$', '', s)
    return re.sub(r'[^a-z0-9]', '', s)


def load_entries(day):
    """{format key: [entry]} from the newest complete snapshot the site posted today (ntfy), else
    data/my_entries.json. Entry: {'l': label, 's': [[name, team, slot], ...]}."""
    lines = []
    try:
        req = urllib.request.Request(f'https://ntfy.sh/{ENTRY_TOPIC}/json?poll=1&since=36h', headers={'User-Agent': 'NBA-EDGE'})
        with urllib.request.urlopen(req, timeout=20) as r:
            lines = r.read().decode('utf-8', 'replace').splitlines()
    except Exception as e:
        print(f'::warning::entries poll failed: {e}')
    snaps = {}
    for ln in lines:
        try:
            m = json.loads(ln)
            d = json.loads(m.get('message') or '') if m.get('event') == 'message' else None
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get('v') != 1 or d.get('day') != day:
            continue
        s = snaps.setdefault((d.get('fmt'), d.get('at') or 0), {'n': d.get('n', 1), 'parts': {}})
        s['parts'][d.get('i', 0)] = d.get('e') or []
    out = {}
    for (fmt, _at), s in sorted(snaps.items(), key=lambda kv: kv[0][1]):
        if len(s['parts']) == s['n']:
            out[fmt] = [e for i in sorted(s['parts']) for e in s['parts'][i]]
    if 'main' not in out:
        me = read(DATA / 'my_entries.json')
        if me.get('date') == day:
            out['main'] = [{'l': e.get('label', ''), 's': [[r['name'], r['team'], r['slot']] for r in e.get('players', [])]}
                           for e in me.get('entries', [])]
    return out


def entry_swaps(today, entries, now, tips):
    """For every entered player who is now out and whose game hasn't started: the best swap."""
    main = {norm(p['name']) + '|' + p['team']: p for p in today['players']}
    started = lambda team: bool(tips.get(team) and now >= tips[team][0])
    sgs = {('sg:' + str(sg['id'])): sg for sg in today.get('single_games', [])}
    out = []
    for fmt_key, ents in entries.items():
        fmt = 'classic' if fmt_key == 'main' else 'single'
        if fmt == 'classic':
            pool = {k: (p, p['salary']) for k, p in main.items() if p.get('main')}
        else:
            sg = sgs.get(fmt_key)
            if not sg:
                continue
            pool = {r['k']: (main[r['k']], r['salary']) for r in sg['players'] if r['k'] in main}
        for ei, e in enumerate(ents):
            rows = []
            for name, team, slot in e.get('s', []):
                k = norm(name) + '|' + team
                rows.append((k, slot, pool.get(k)))
            if any(r[2] is None for r in rows):
                continue
            keys = {k for k, _, _ in rows}
            for k, slot, (p, sal) in rows:
                if not p.get('out') or started(p['team']):
                    continue
                mult = FS.FORMATS[fmt]['mult'].get(slot, 1.0)
                smul = FS.FORMATS[fmt]['sal'].get(slot, 1.0)
                used = sum(int(s2 * FS.FORMATS[fmt]['sal'].get(sl2, 1.0)) for k2, sl2, (_, s2) in rows if k2 != k)
                room = CAP - used
                best = None
                for k2, (q, s2) in pool.items():
                    if k2 in keys or q.get('out') or (q.get('proj') or 0) <= 0 or started(q['team']):
                        continue
                    if not FS.can_fill(q['position'], slot, fmt) or int(s2 * smul) > room:
                        continue
                    if fmt == 'classic':
                        teams = [main[k3]['team'] for k3, _, _ in rows if k3 != k] + [q['team']]
                        if teams.count(q['team']) > FS.FORMATS['classic']['team_max']:
                            continue
                    if best is None or q['proj'] * mult > best[0]:
                        best = (q['proj'] * mult, q, s2)
                label = e.get('l') or f'Entry {ei + 1}'
                out.append({'id': f"swap|{fmt_key}|{ei}|{k}", 'fmt': fmt_key, 'entry': label, 'slot': slot,
                            'out_player': p['name'], 'team': p['team'],
                            'swap': best and {'name': best[1]['name'], 'team': best[1]['team'], 'salary': int(best[2] * smul),
                                              'proj': round(best[0], 1)},
                            'room': room})
    return out


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
    for g in today.get('games', []) + [g for cs in today.get('classic_slates', []) for g in cs.get('games', [])] + today.get('single_games', []):
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
    # Your entries: a swap push for every entered player now out whose game hasn't started.
    swaps = []
    try:
        swaps = entry_swaps(today, load_entries(day), now, tips)
    except Exception as e:
        print(f'::warning::entry swap check failed: {e}')
    sent = {a['id'] for a in state['alerts']}
    for sw in swaps:
        sid = f"{day}|{sw['id']}"
        if sid in sent:
            continue
        tip = tips.get(sw['team'], (None, ''))[0]
        by = tip.astimezone(ET).strftime('%-I:%M %p') if tip else 'his game starts'
        if sw['swap']:
            body = (f"{sw['entry']}, {sw['slot']}: {sw['out_player']} -> {sw['swap']['name']} ({sw['swap']['team']}, "
                    f"${sw['swap']['salary']:,}, {sw['swap']['proj']} proj). Swap on FanDuel before {by}. "
                    f"Tap to re-optimize every unlocked spot instead.")
        else:
            body = f"{sw['entry']}, {sw['slot']}: {sw['out_player']} is out and no one fits ${sw['room']:,}. Open the site to re-optimize before {by}."
        a = {'id': sid, 'kind': 'swap', 'late': False, 'player': sw['out_player'], 'team': sw['team'],
             'title': f"Swap {sw['out_player']} out of your lineup", 'body': body, 'at': now.isoformat(timespec='seconds')}
        state['alerts'].append(a)
        new_alerts.append(a)
        if topic:
            try:
                push(topic, a['title'], a['body'], 'warning')
                a['pushed'] = True
            except Exception as e:
                print(f'::warning::ntfy push failed: {e}')
        print('SWAP', a['title'], '|', a['body'])
    state['players'] = cur
    state['updated_at'] = now.isoformat(timespec='seconds')
    old = read(DATA / 'alerts.json')
    if new_alerts or old.get('date') != day or not old.get('players') or old.get('players') != cur:
        (DATA / 'alerts.json').write_text(json.dumps(state, separators=(',', ':')) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
