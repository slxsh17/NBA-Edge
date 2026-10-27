#!/usr/bin/env python3
"""NBA EDGE v25: recent-rotation-aware, DNP-aware pregame projections.

Reads already published completed regular-season logs; uses no future box scores,
placeholder minutes, or unpublished/proprietary projection data. Output is
slate-dated and attaches source/uncertainty to each projection. Optional
external reference feeds must be authorized for NBA EDGE's use.
"""
import csv
import datetime as dt
import json
import math
import pathlib
import re
import statistics
import unicodedata
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'


LEAGUE_RATE=0.98
RATE_PRIOR_GAMES=6
TEAM_ALIASES={'GS':'GSW','NO':'NOP','NY':'NYK','PHO':'PHX','SA':'SAS','UTAH':'UTA','WSH':'WAS','BRK':'BKN'}


def canon_team(t):
    t=str(t or '').strip().upper()
    return TEAM_ALIASES.get(t,t)


def norm(s):
    return re.sub(r'[^a-z0-9]', '', unicodedata.normalize('NFKD', str(s or '')).lower())


def num(s, lo=None, hi=None):
    try:
        n=float(s)
        if not math.isfinite(n) or (lo is not None and n < lo) or (hi is not None and n > hi):
            return None
        return n
    except (ValueError, TypeError):
        return None


def read(name):
    path=DATA/name
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}


def parse_date(value):
    s=re.sub(r'[^0-9]','',str(value or ''))
    if len(s)<8:return None
    try:return dt.date(int(s[:4]),int(s[4:6]),int(s[6:8]))
    except ValueError:return None


def slate_rows(path):
    with path.open(newline='', encoding='utf-8-sig') as f:
        for original in csv.DictReader(f):
            r={str(k or '').strip().lower():str(v or '').strip() for k,v in original.items()}
            name=r.get('nickname') or r.get('name') or ' '.join(filter(None,[r.get('first name'),r.get('last name')]))
            team=(r.get('team') or '').upper()
            salary=num(r.get('salary'),2000,30000)
            if name and team and salary:
                yield {'name':name,'team':team,'salary':int(salary)}


def log_index(raw, date_floor=None, date_ceiling=None):
    grouped={}
    unique=set()
    for r in raw:
        date=parse_date(r.get('date'))
        if date is None or (date_floor and date<date_floor) or (date_ceiling and date>=date_ceiling):continue
        name=norm(r.get('name'))
        minutes=num(r.get('minutes'),.01,55)
        fp=num(r.get('fantasy_points'),-10,160)
        if not (name and minutes is not None and fp is not None):continue
        # Deduplicate repeated scoreboard games on event id; fallback by day and name.
        event=str(r.get('event_id') or date.isoformat())
        key=(name,event)
        if key in unique:continue
        unique.add(key)
        grouped.setdefault(name,[]).append({'event':event,'date':date,'minutes':minutes,'fp':fp,'team':canon_team(r.get('team'))})
    for rows in grouped.values():rows.sort(key=lambda x:x['date'])
    return grouped


def summary(rows):
    if not rows:return None
    mins=[r['minutes'] for r in rows]
    return {'games':len(rows),'minutes':statistics.mean(mins),'rate':sum(r['fp'] for r in rows)/sum(mins),
            'team':rows[-1]['team'],'last_minutes':mins[-5:]}


def prior_index(data):
    result={k:summary(v) for k,v in log_index(data.get('game_logs',[])).items()}
    for r in data.get('players',[]):
        key=norm(r.get('name'))
        if not key or result.get(key):continue
        m=num(r.get('avg_minutes'),.01,48);f=num(r.get('fantasy_points_per_minute'),.01,4)
        g=int(num(r.get('games'),0,100) or 0)
        if m and f and g:
            result[key]={'games':g,'minutes':m,'rate':f,'team':canon_team(r.get('team')),
                         'last_minutes':[]}
    return result


def team_schedule(raw, date_floor, date_ceiling):
    """Team -> ordered list of (date, event_id) for completed games in the feed window."""
    games={}
    for r in raw:
        date=parse_date(r.get('date'))
        team=canon_team(r.get('team'))
        if date is None or not team or date<date_floor or date>=date_ceiling:continue
        games.setdefault(team,{})[str(r.get('event_id') or date.isoformat())]=date
    return {t:sorted(((d,e) for e,d in ev.items()),key=lambda x:x[0]) for t,ev in games.items()}


def minute_series(player_rows, schedule):
    """Minutes in each of the team's recent games, 0.0 for a team game the player missed (DNP)."""
    played={str(r.get('event') or r['date'].isoformat()):r['minutes'] for r in (player_rows or [])}
    played_dates={r['date']:r['minutes'] for r in (player_rows or [])}
    out=[]
    for d,e in schedule:
        out.append(played.get(e, played_dates.get(d, 0.0)))
    return out


def dropwhile_zero(series):
    """Series with the trailing run of DNPs removed."""
    i=len(series)
    while i and series[i-1]==0:i-=1
    return series[:i]


def expected_minutes(old, current, series, injured):
    """Minutes robust to stale roles.

    `series` = the player's minutes in each of his team's recent games, 0.0 = DNP.
    Rules (research heuristics, not validated forecasts):
    - One DNP is treated as rest/minor injury, not a role change.
    - 2+ straight DNPs with no injury listing: if he was still playing his normal
      role right before, warn and trim; if he was already fading or never played
      this season, treat as out of the rotation (~3 min).
    - Played-game role collapse (two straight played games <55% of prior avg)
      overrides last season's minutes, as in v24.
    """
    warnings=[]
    if old and old['games']<3:old=None
    if not old and not current:return None,'missing',warnings
    prior=old['minutes'] if old else None
    played=[x for x in series if x>0] or (current['last_minutes'] if current else [])
    streak=len(series)-len(dropwhile_zero(series))
    if injured and streak:
        warnings.append(f'DNP in last {streak} team game(s); on injury report ({injured})')
    if streak>=2 and not injured:
        before=dropwhile_zero(series)
        last_played=before[-1] if before else None
        baseline=prior or (current['minutes'] if current else 0)
        if last_played is None:
            warnings.append(f'ROLE DROP: no minutes in any of team\'s last {len(series)} games and not on injury report; verify roster/rotation')
            return 3.0,'no_recent_minutes',warnings
        if baseline and last_played<baseline*.55:
            warnings.append(f'ROLE DROP: DNP in last {streak} team games after a reduced role, no injury listing; verify rotation')
            return 3.0,'recent_dnp',warnings
        warnings.append(f'DNP in last {streak} team games with no injury listing; possible unlisted injury/rest — verify status')
        m=prior*(.85) if prior else current['minutes']*.85
        return min(36,m),'dnp_streak_caution',warnings
    if not current:
        warnings.append('No recent regular-season minute observations')
        return min(40,old['minutes']),'prior_only',warnings
    cur=current['minutes']
    if not old:
        warnings.append('No reliable last-season baseline; current-season sample only')
        return min(40,cur),'current_only',warnings
    last2=played[-2:]
    if len(last2)>=2 and prior>=16 and all(x<=prior*.55 for x in last2):
        new=min(38,max(1.0,statistics.median(last2)+2))
        warnings.append('ROLE DROP: last two games played both <55% of prior average; verify rotation')
        return new,'recent_role_drop',warnings
    if cur<prior*.5 and current['games']==1 and prior>=16:
        warnings.append('One extremely short recent game; role/early exit uncertain')
        return min(26,max(cur+5,prior*.60)),'single_short_game_caution',warnings
    if len(played)>=3 and prior>=16 and statistics.median(played[-3:])<prior*.7:
        warnings.append('Lower current-season playing time; extra weight given to recent games')
        return min(40,prior*.25+statistics.median(played[-3:])*.75),'recent_minutes_decline',warnings
    # Last season counts for less when it was itself a small sample (e.g. injury year).
    prior_strength=3*min(1,old['games']/30)
    w=min(.85,current['games']/(current['games']+prior_strength))
    return min(40,prior*(1-w)+cur*w),'sample_blend',warnings


def build_for_player(key,name,team,old,cur,series,injured,authorized_refs):
    if old and old['games']<3:old=None
    m,method,warnings=expected_minutes(old,cur,series,injured)
    result={'name':name,'team':team,'previous_games':old['games'] if old else 0,
            'previous_team':old['team'] if old else None,
            'current_games':cur['games'] if cur else 0,
            'current_last_minutes':cur['last_minutes'][-5:] if cur else [],
            'team_recent_minutes':[round(x,1) for x in series[-5:]],
            'recent_dnps':sum(1 for x in series if x==0),
            'injury_status':injured or None,
            'prior_avg_minutes':round(old['minutes'],1) if old else None,
            'current_avg_minutes':round(cur['minutes'],1) if cur else None,
            'expected_minutes':None,'team_change_minutes':None,'fp_per_minute':None,'base_projected_fp':None,
            'minutes_method':method,'warnings':warnings,'confidence':'low',
            'reference_projection':None,'reference_source':None,'reference_delta':None}
    if m is None:return result
    if old and cur:
        w=min(.5,cur['games']/(cur['games']+15))
        rate=old['rate']*(1-w)+cur['rate']*w
    else:rate=(cur or old)['rate']
    # Small samples regress toward league average (~0.98 FP/min for rotation players,
    # 2025-26). A 50-game player barely moves; an 8-game hot streak gets pulled in.
    games=(old['games'] if old else 0)+(cur['games'] if cur else 0)
    raw_rate=rate
    k=RATE_PRIOR_GAMES*max(0,1-games/40)  # no pull at all once a player has 40+ games
    rate=(games*rate+k*LEAGUE_RATE)/(games+k) if games+k else rate
    if games<20 and abs(raw_rate-rate)>=.15:
        warnings.append(f'Small sample ({games} games): FP/min {raw_rate:.2f} regressed to {rate:.2f}')
    rate=min(2.5,max(.15,rate))
    result['expected_minutes']=round(m,2)
    result['fp_per_minute']=round(rate,4)
    result['base_projected_fp']=round(m*rate,2)
    # If the slate lists a different team (trade/signing), the browser uses this instead:
    # the DNP check above was against the wrong team's schedule.
    base=(cur or old)['minutes']
    result['team_change_minutes']=round(min(base,26),2)
    result['confidence']='medium' if method=='sample_blend' and cur and cur['games']>=3 else 'low'
    ref=authorized_refs.get((key,team))
    if ref is not None:
        result['reference_projection']=round(ref['projection'],2)
        result['reference_source']=ref['source']
        result['reference_delta']=round(result['base_projected_fp']-ref['projection'],2)
        if abs(result['reference_delta'])>=max(8,ref['projection']*.25):
            result['warnings'].append('Large disagreement with authorized outside projection')
    return result


def authorized_refs_for_date(data,day):
    # Consumes only feeds the operator has independently licensed and chosen to publish.
    if not data or data.get('slate_date')!=day:return {},False
    refs={}
    for r in data.get('players',[]):
        proj=num(r.get('projected_fp') or r.get('projection') or r.get('fantasy_points'),.01,160)
        name=norm(r.get('name'));team=canon_team(r.get('team'))
        source=str(r.get('source') or data.get('provider') or '').strip()
        if proj and name and team and source:refs[(name,team)]={'projection':proj,'source':source}
    return refs,bool(refs)


def build(day=None):
    """Projects every player with NBA history. No slate file needed: the browser
    matches these rows to whatever FanDuel CSV the user imports."""
    day=day or dt.datetime.now(ZoneInfo('America/New_York')).date()
    day_str=day.isoformat()
    old=read('last_season_stats.json');new=read('player_stats.json')
    prior=prior_index(old)
    floor=dt.date(2026,10,20)
    raw=new.get('game_logs',[])
    cur_logs=log_index(raw,floor,day)
    current={k:summary(v) for k,v in cur_logs.items()}
    schedules=team_schedule(raw,floor,day)
    names={}
    for r in old.get('players',[])+old.get('game_logs',[])+raw:
        k=norm(r.get('name'))
        if k:names[k]=r.get('name')
    injuries={}
    for r in read('injuries.json').get('injuries',[]):
        k=norm(r.get('name'))
        if k:injuries[k]=str(r.get('status') or 'listed')
    refs,have_refs=authorized_refs_for_date(read('authorized_projection_benchmarks.json'),day_str)
    out=[]
    for key in sorted(set(prior)|set(current)):
        o=prior.get(key);c=current.get(key)
        team=(c or o or {}).get('team') or ''
        if not team:continue
        series=minute_series(cur_logs.get(key),schedules.get(team,[]))
        out.append(build_for_player(key,names.get(key,key),team,o,c,series,injuries.get(key),refs))
    covered=sum(p['base_projected_fp'] is not None for p in out)
    risky=sum(any('ROLE DROP' in s or 'short recent game' in s or 'DNP in last' in s for s in p['warnings']) for p in out)
    compared=sum(p['reference_projection'] is not None for p in out)
    return {'schema':2,'model_revision':'v25-dnp-aware', 'slate_date':day_str,'scope':'all_players',
        'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),
        'source':'NBA completed game box scores; no premium data scraped',
        'model':'Observed historical FP per minute x rotation-aware expected minutes (team games missed without an injury listing count as 0 minutes); browser matchup and injury adjustments remain separately labeled',
        'recent_window':{'from':min((d for s in schedules.values() for d,_ in s),default=day).isoformat(),'to':(day-dt.timedelta(days=1)).isoformat()},
        'players_projected':covered,'role_warnings':risky,
        'outside_reference_status':('authorized references available' if have_refs else 'no authorized public/licensed numerical projection feed'),
        'outside_compared':compared,'players':out}


def main():
    output=build()
    DATA.mkdir(parents=True,exist_ok=True)
    prev=read('automatic_projections.json')
    if prev and {k:v for k,v in prev.items() if k!='created_at'}=={k:v for k,v in output.items() if k!='created_at'}:
        print('automatic_projections.json unchanged');return
    (DATA/'automatic_projections.json').write_text(json.dumps(output,separators=(',',':'))+'\n',encoding='utf-8')
    print('NBA EDGE v25',output['slate_date'], 'projected',output['players_projected'],
          'role warnings',output['role_warnings'],'external comparisons',output['outside_compared'])


if __name__=='__main__':main()
