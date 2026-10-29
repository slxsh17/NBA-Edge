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
# Rule settings. Defaults are the hand-picked values; tune_minutes.py learns better ones
# from past seasons and saves them to data/model_params.json.
DEFAULT_PARAMS={
    'role_ratio':0.55,      # two played games below this share of last season's minutes = role drop
    'role_bump':2.0,        # minutes added to the median of those two games
    'short_ratio':0.5,      # one game below this share = short-game caution
    'decline_ratio':0.7,    # median of last 3 below this share = declining role
    'decline_w':0.75,       # weight on recent median when declining
    'prior_k':3.0,          # how many 'games' last season is worth when blending
    'blend_cap':0.85,       # max weight on this season in the blend
    'recent_w':0.0,         # weight of last-5-games minutes inside this season's average
    'dnp_mult':0.85,        # minutes kept after a DNP streak with a normal role before it
    'rotation_out_min':3.0, # minutes for players out of the rotation
    'rate_k':15.0,          # games before this season's FP/min gets half weight
    'rate_cap':0.5,         # max weight on this season's FP/min
    'shrink_games':6.0,     # pull of small samples toward league FP/min
    'blowout_game_w':1.0,
    'b2b_mult':1.0,         # projection multiplier on the second night of a back-to-back
    'three_in_four_mult':1.0, # ... on the third game in four nights   # weight of 20+ point blowouts in recent minutes (below 0.5 they are also ignored by role-drop rules)
}
BLOWOUT=20
def load_params():
    try:
        d=json.loads((pathlib.Path(__file__).resolve().parent.parent/'data'/'model_params.json').read_text(encoding='utf-8'))
        return {**DEFAULT_PARAMS,**{k:float(v) for k,v in (d.get('params') or {}).items() if k in DEFAULT_PARAMS}}
    except (OSError,ValueError):
        return dict(DEFAULT_PARAMS)
P=load_params()
RATE_PRIOR_GAMES=P['shrink_games']
TEAM_ALIASES={'GS':'GSW','NO':'NOP','NY':'NYK','PHO':'PHX','SA':'SAS','UTAH':'UTA','WSH':'WAS','BRK':'BKN'}


def canon_team(t):
    t=str(t or '').strip().upper()
    return TEAM_ALIASES.get(t,t)


def norm(s):
    # Name key: accents, case and punctuation ignored, and a trailing Jr./Sr./II/III/IV dropped,
    # so "Darius Acuff" (FanDuel) matches "Darius Acuff Jr." (ESPN box scores).
    s = unicodedata.normalize('NFKD', str(s or '')).lower().strip()
    s = re.sub(r'[\s,]+(jr|sr|ii|iii|iv|v)\.?$', '', s)
    return re.sub(r'[^a-z0-9]', '', s)


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


MARGINS={}


def attach_margins(raw):
    """Final margin of every game from the box scores (team points summed), so the model
    can tell blowouts apart: starters sit late in blowouts, which distorts minutes."""
    pts={}
    for r in raw:
        ev=str(r.get('event_id') or '');t=canon_team(r.get('team'))
        if ev and t:
            pts.setdefault(ev,{}).setdefault(t,0.0)
            pts[ev][t]+=float(r.get('pts') or 0)
    for ev,teams in pts.items():
        if len(teams)==2:
            (a,pa),(b,pb)=teams.items()
            MARGINS[(ev,a)]=pa-pb;MARGINS[(ev,b)]=pb-pa


def log_index(raw, date_floor=None, date_ceiling=None):
    if not MARGINS:attach_margins(raw)
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
        team=canon_team(r.get('team'))
        grouped.setdefault(name,[]).append({'event':event,'date':date,'minutes':minutes,'fp':fp,'team':team,'margin':MARGINS.get((event,team))})
    for rows in grouped.values():rows.sort(key=lambda x:x['date'])
    return grouped


def summary(rows):
    if not rows:return None
    mins=[r['minutes'] for r in rows]
    return {'games':len(rows),'minutes':statistics.mean(mins),'rate':sum(r['fp'] for r in rows)/sum(mins),
            'team':rows[-1]['team'],'last_minutes':mins[-5:],'last_margins':[r.get('margin') for r in rows[-5:]]}


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
    if current and P['blowout_game_w']<0.5:
        # Recent games that were blowouts don't count toward role-drop/decline rules.
        lm,lg=current.get('last_minutes',[]),current.get('last_margins',[])
        clean=[m for m,g in zip(lm,lg) if g is None or abs(g)<BLOWOUT]
        if len(clean)>=2 and len(clean)<len(lm):played=clean
    streak=len(series)-len(dropwhile_zero(series))
    if injured and streak:
        warnings.append(f'DNP in last {streak} team game(s); on injury report ({injured})')
    if streak>=2 and not injured:
        before=dropwhile_zero(series)
        last_played=before[-1] if before else None
        baseline=prior or (current['minutes'] if current else 0)
        if last_played is None:
            warnings.append(f'ROLE DROP: no minutes in any of team\'s last {len(series)} games and not on injury report; verify roster/rotation')
            return P['rotation_out_min'],'no_recent_minutes',warnings
        if baseline and last_played<baseline*P['role_ratio']:
            warnings.append(f'ROLE DROP: DNP in last {streak} team games after a reduced role, no injury listing; verify rotation')
            return P['rotation_out_min'],'recent_dnp',warnings
        warnings.append(f'DNP in last {streak} team games with no injury listing; possible unlisted injury/rest — verify status')
        m=prior*P['dnp_mult'] if prior else current['minutes']*P['dnp_mult']
        return min(36,m),'dnp_streak_caution',warnings
    if not current:
        warnings.append('No recent regular-season minute observations')
        return min(40,old['minutes']),'prior_only',warnings
    cur=current['minutes']
    if P['recent_w'] and current.get('last_minutes'):
        lm=current['last_minutes'][-5:];lg=(current.get('last_margins') or [None]*len(lm))[-len(lm):]
        w=[P['blowout_game_w'] if g is not None and abs(g)>=BLOWOUT else 1.0 for g in lg]
        recent=sum(a*b for a,b in zip(lm,w))/sum(w) if sum(w)>0 else statistics.mean(lm)
        cur=(1-P['recent_w'])*cur+P['recent_w']*recent
    if not old:
        warnings.append('No reliable last-season baseline; current-season sample only')
        return min(40,cur),'current_only',warnings
    last2=played[-2:]
    if len(last2)>=2 and prior>=16 and all(x<=prior*P['role_ratio'] for x in last2):
        new=min(38,max(1.0,statistics.median(last2)+P['role_bump']))
        warnings.append('ROLE DROP: last two games played both <55% of prior average; verify rotation')
        return new,'recent_role_drop',warnings
    if cur<prior*P['short_ratio'] and current['games']==1 and prior>=16:
        warnings.append('One extremely short recent game; role/early exit uncertain')
        return min(26,max(cur+5,prior*.60)),'single_short_game_caution',warnings
    if len(played)>=3 and prior>=16 and statistics.median(played[-3:])<prior*P['decline_ratio']:
        warnings.append('Lower current-season playing time; extra weight given to recent games')
        return min(40,prior*(1-P['decline_w'])+statistics.median(played[-3:])*P['decline_w']),'recent_minutes_decline',warnings
    # Last season counts for less when it was itself a small sample (e.g. injury year).
    prior_strength=P['prior_k']*min(1,old['games']/30)
    w=min(P['blend_cap'],current['games']/(current['games']+prior_strength))
    return min(40,prior*(1-w)+cur*w),'sample_blend',warnings


def rate_for(old,cur):
    """FP per minute: last season and this season blended, small samples pulled toward
    league average (a 40+ game player isn't pulled at all)."""
    if old and cur:
        w=min(P['rate_cap'],cur['games']/(cur['games']+P['rate_k']))
        rate=old['rate']*(1-w)+cur['rate']*w
    else:rate=(cur or old)['rate']
    games=(old['games'] if old else 0)+(cur['games'] if cur else 0)
    raw=rate
    k=P['shrink_games']*max(0,1-games/40)
    rate=(games*rate+k*LEAGUE_RATE)/(games+k) if games+k else rate
    return min(2.5,max(.15,rate)),raw,games


def rest_flags(schedule, day):
    """(back_to_back, third_in_four, days_rest) for a team playing on `day`, from its game dates."""
    dates=[d for d,_ in schedule if d<day]
    if not dates:return False,False,None
    rest=(day-dates[-1]).days
    recent=sum(1 for d in dates if (day-d).days<=3)
    return rest==1,recent>=2,rest


def rest_mult(b2b,t34):
    return (P['b2b_mult'] if b2b else 1.0)*(P['three_in_four_mult'] if t34 else 1.0)


def build_for_player(key,name,team,old,cur,series,injured,authorized_refs,rest=(False,False,None)):
    if old and old['games']<3:old=None
    m,method,warnings=expected_minutes(old,cur,series,injured)
    b2b,t34,rest_days=rest
    if m is not None and (b2b or t34):
        m*=rest_mult(b2b,t34)
        warnings.append('Back-to-back (second night)' if b2b else 'Third game in four nights')
    if cur and cur.get('last_margins') and cur['last_margins'][-1] is not None and abs(cur['last_margins'][-1])>=BLOWOUT:
        g=cur['last_margins'][-1]
        warnings.append(f"Blowout last game ({'won' if g>0 else 'lost'} by {abs(g):.0f}): his {cur['last_minutes'][-1]:.0f} minutes may not reflect his role")
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
    rate,raw_rate,games=rate_for(old,cur)
    if games<20 and abs(raw_rate-rate)>=.15:
        warnings.append(f'Small sample ({games} games): FP/min {raw_rate:.2f} regressed to {rate:.2f}')
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


def season_logs():
    """This season's box scores: the permanent season file plus the latest 7-day feed."""
    seen={}
    for r in read('season_logs.json').get('game_logs',[])+read('player_stats.json').get('game_logs',[]):
        seen[(str(r.get('id') or r.get('name')),str(r.get('event_id') or r.get('date')))]=r
    return list(seen.values())


def build(day=None, injury_map=None):
    """Projects every player with NBA history. No slate file needed: the browser
    matches these rows to whatever FanDuel CSV the user imports."""
    day=day or dt.datetime.now(ZoneInfo('America/New_York')).date()
    day_str=day.isoformat()
    old=read('last_season_stats.json');new=read('player_stats.json')
    new={**new,'game_logs':season_logs()}
    prior=prior_index(old)
    floor=dt.date(2026,10,20)
    raw=new.get('game_logs',[])
    attach_margins(raw)
    cur_logs=log_index(raw,floor,day)
    current={k:summary(v) for k,v in cur_logs.items()}
    schedules=team_schedule(raw,floor,day)
    names={}
    for r in old.get('players',[])+old.get('game_logs',[])+raw:
        k=norm(r.get('name'))
        if k:names[k]=r.get('name')
    injuries=dict(injury_map) if injury_map is not None else {}
    for r in ([] if injury_map is not None else read('injuries.json').get('injuries',[])):
        k=norm(r.get('name'))
        if k:injuries[k]=str(r.get('status') or 'listed')
    refs,have_refs=authorized_refs_for_date(read('authorized_projection_benchmarks.json'),day_str)
    out=[]
    for key in sorted(set(prior)|set(current)):
        o=prior.get(key);c=current.get(key)
        team=(c or o or {}).get('team') or ''
        if not team:continue
        series=minute_series(cur_logs.get(key),schedules.get(team,[]))
        out.append(build_for_player(key,names.get(key,key),team,o,c,series,injuries.get(key),refs,rest_flags(schedules.get(team,[]),day)))
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
