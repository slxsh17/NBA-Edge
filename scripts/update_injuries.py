#!/usr/bin/env python3
"""NBA EDGE injury snapshot from ESPN's unofficial endpoint.
Only recognized statuses/teams published. Fail closed if no records match.
"""
import json
from pathlib import Path
from datetime import datetime, timezone
from urllib.request import Request, urlopen

URL = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries'
TEAM_NAMES = {
    'ATL':'Atlanta Hawks','BOS':'Boston Celtics','BKN':'Brooklyn Nets','CHA':'Charlotte Hornets',
    'CHI':'Chicago Bulls','CLE':'Cleveland Cavaliers','DAL':'Dallas Mavericks','DEN':'Denver Nuggets',
    'DET':'Detroit Pistons','GSW':'Golden State Warriors','HOU':'Houston Rockets','IND':'Indiana Pacers',
    'LAC':'LA Clippers','LAL':'Los Angeles Lakers','MEM':'Memphis Grizzlies','MIA':'Miami Heat',
    'MIL':'Milwaukee Bucks','MIN':'Minnesota Timberwolves','NOP':'New Orleans Pelicans',
    'NYK':'New York Knicks','OKC':'Oklahoma City Thunder','ORL':'Orlando Magic',
    'PHI':'Philadelphia 76ers','PHX':'Phoenix Suns','POR':'Portland Trail Blazers',
    'SAC':'Sacramento Kings','SAS':'San Antonio Spurs','TOR':'Toronto Raptors',
    'UTA':'Utah Jazz','WAS':'Washington Wizards',
}
ALIASES={'GS':'GSW','NO':'NOP','NY':'NYK','SA':'SAS','UTAH':'UTA','WSH':'WAS','PHO':'PHX','BRK':'BKN'}
NAME_TO_TEAM={name.lower():abbr for abbr,name in TEAM_NAMES.items()}
NAME_TO_TEAM.update({'los angeles clippers':'LAC','l.a. clippers':'LAC','la clippers':'LAC','golden state':'GSW','new york':'NYK','portland':'POR','utah':'UTA','washington':'WAS'})

def resolve_team(v):
    if isinstance(v,dict):
        for key in ('abbreviation','displayName','name','shortDisplayName','team'):
            r=resolve_team(v.get(key)) if v.get(key) else ''
            if r:return r
        return ''
    t=str(v or '').strip()
    upper=t.upper()
    upper=ALIASES.get(upper,upper)
    if upper in TEAM_NAMES:return upper
    return NAME_TO_TEAM.get(t.lower(),'')

def get_name(v):
    if isinstance(v,dict):
        result=v.get('displayName') or v.get('fullName') or v.get('name')
        if result:return str(result).strip()
        return (str(v.get('firstName') or '')+' '+str(v.get('lastName') or '')).strip()
    return str(v or '').strip()

def get_status(v):
    if isinstance(v,dict):
        for key in ('name','description','status','type','displayName'):
            if key in v:
                s=get_status(v[key])
                if s!='UNKNOWN':return s
        return 'UNKNOWN'
    s=str(v or '').strip().lower()
    if s in ('out','inactive') or 'ruled out' in s:return 'OUT'
    if 'doubt' in s:return 'DOUBTFUL'
    if 'question' in s:return 'QUESTIONABLE'
    if 'probab' in s:return 'PROBABLE'
    if 'day-to-day' in s or 'day to day' in s:return 'DAY-TO-DAY'
    return 'UNKNOWN'

def parse(data):
    groups=data.get('injuries',[]) if isinstance(data,dict) else []
    if not isinstance(groups,list):raise RuntimeError('Unexpected ESPN injury response')
    records=[]
    for group in groups:
        if not isinstance(group,dict):continue
        groupteam=resolve_team(group.get('team')) or resolve_team(group.get('abbreviation')) or resolve_team(group.get('displayName'))
        entries=group.get('injuries') or group.get('athletes') or []
        if not isinstance(entries,list):continue
        for entry in entries:
            if not isinstance(entry,dict):continue
            player=entry.get('athlete') or entry.get('player') or entry
            name=get_name(player) or get_name(entry.get('name'))
            team=resolve_team(entry.get('team')) or groupteam
            status=get_status(entry.get('status') or entry.get('type'))
            if name and team and status!='UNKNOWN':
                records.append({'name':name,'team':team,'status':status})
    return records

def main():
    req=Request(URL,headers={'User-Agent':'Mozilla/5.0 (compatible; NBAEdgeResearch/1.0)','Accept':'application/json'})
    with urlopen(req,timeout=35) as response:data=json.load(response)
    records=parse(data)
    print('Groups:',len(data.get('injuries',[])),'Recognized injuries:',len(records))
    if not records:raise RuntimeError('No parseable injuries in ESPN response; previous snapshot preserved')
    result={'updated_at':datetime.now(timezone.utc).isoformat(),
            'source':'ESPN unofficial NBA injury endpoint; statuses must be independently verified',
            'injuries':records}
    path=Path('data/injuries.json');path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print('Saved',path,'with',len(records),'injuries')

if __name__=='__main__':main()
