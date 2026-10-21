#!/usr/bin/env python3
"""Best-effort ESPN injury snapshot. Fails closed; never publishes an empty feed on API errors."""
import json
from pathlib import Path
from datetime import datetime, timezone
from urllib.request import Request, urlopen

URL='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries'
TEAMS={'ATL','BOS','BKN','CHA','CHI','CLE','DAL','DEN','DET','GS','GSW','HOU','IND','LAC','LAL','MEM','MIA','MIL','MIN','NO','NOP','NY','NYK','OKC','ORL','PHI','PHX','POR','SAC','SA','SAS','TOR','UTAH','UTA','WAS','WSH'}
ALIASES={'GS':'GSW','NO':'NOP','NY':'NYK','SA':'SAS','UTAH':'UTA','WSH':'WAS'}

def name(v):
    if isinstance(v,dict):return str(v.get('displayName') or v.get('fullName') or v.get('name') or '')
    return str(v or '')

def team(v):
    if isinstance(v,dict):return team(v.get('abbreviation') or v.get('team') or v.get('displayName'))
    t=str(v or '').upper();return ALIASES.get(t,t) if t in TEAMS else ''

def status(v):
    if isinstance(v,dict): v=v.get('name') or v.get('description') or v.get('status') or v.get('type') or ''
    s=str(v or '').lower().strip()
    if 'out' in s or 'inactive' in s:return 'OUT'
    if 'doubt' in s:return 'DOUBTFUL'
    if 'question' in s:return 'QUESTIONABLE'
    if 'probab' in s:return 'PROBABLE'
    if 'day' in s:return 'DAY-TO-DAY'
    return 'UNKNOWN'

def main():
    req=Request(URL,headers={'User-Agent':'Mozilla/5.0 (compatible; NBAEdgeResearch/1.0)','Accept':'application/json'})
    with urlopen(req, timeout=35) as response:data=json.load(response)

    print("Response type:", type(data).__name__)
    print("Top-level keys:", list(data.keys()) if isinstance(data, dict) else "Not a dictionary")
    print("First injury group:", str(data.get("injuries", [])[:1])[:1500] if isinstance(data, dict) and isinstance(data.get("injuries"), list) else "No injuries list found")

    groups=data.get('injuries',[]) if isinstance(data,dict) else []
    if not isinstance(groups,list):raise RuntimeError('unexpected injuries format')
    records=[]
    for group in groups:
        if not isinstance(group,dict):continue
        groupteam=team(group.get('team') or group.get('abbreviation'))
        entries=group.get('injuries') or group.get('athletes') or []
        if not isinstance(entries,list):continue
        for entry in entries:
            if not isinstance(entry,dict):continue
            athlete=entry.get('athlete') or entry.get('player') or entry
            n=name(athlete) or name(entry.get('name'))
            t=team(entry.get('team')) or groupteam
            st=status(entry.get('status') or entry.get('type'))
            if n and t and st!='UNKNOWN':records.append({'name':n,'team':t,'status':st})
    # A structural change or empty response must never erase previously reported injuries.
    if not records:raise RuntimeError('No parseable injuries in ESPN response; preserving previous snapshot')
    out={'updated_at':datetime.now(timezone.utc).isoformat(),'source':'ESPN unofficial NBA injuries endpoint (best effort)','injuries':records}
    path=Path('data/injuries.json');path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(out,indent=2)+'\n')
    print('Published',len(records),'injury statuses')

if __name__=='__main__':main()
