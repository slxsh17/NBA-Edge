#!/usr/bin/env python3
"""Free ESPN scoreboard snapshot. Unofficial endpoint may change or be blocked."""
import json, datetime, pathlib, urllib.request
from zoneinfo import ZoneInfo
now=datetime.datetime.now(ZoneInfo('America/New_York'))
date=now.strftime('%Y%m%d')
url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates='+date
req=urllib.request.Request(url,headers={'Accept':'application/json'})
with urllib.request.urlopen(req,timeout=25) as res:
    data=json.load(res)
games=[]
for e in data.get('events',[]):
    c=e.get('competitions',[{}])[0]
    teams=c.get('competitors',[])
    get=lambda side: next((t.get('team',{}).get('displayName','Unknown') for t in teams if t.get('homeAway')==side),'Unknown')
    games.append({'away':get('away'),'home':get('home'),'time':e.get('date',''),'status':e.get('status',{}).get('type',{}).get('description','Scheduled')})
out={'date':date,'updated_at':now.isoformat(),'source':'ESPN unofficial scoreboard','games':games}
path=pathlib.Path(__file__).resolve().parent.parent/'data'/'games.json';path.parent.mkdir(exist_ok=True)
path.write_text(json.dumps(out,indent=2)+'\n')
print('Saved',len(games),'games to',path)
