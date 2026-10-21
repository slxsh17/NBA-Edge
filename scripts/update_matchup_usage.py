#!/usr/bin/env python3
"""NBA EDGE: free, cautious matchup/usage snapshot.
DvP: observed opponent FanDuel points per player-game by position based on
historical ESPN logs, NOT opponent adjusted defensive impact.
Usage: possession-estimated share from the most recent regular-season ESPN box scores.
Outputs no usage adjustment until >=3 observed games; does not fabricate records.
"""
import csv, json, pathlib, datetime, statistics, urllib.request, time, collections, re
ROOT=pathlib.Path(__file__).resolve().parent.parent
DATA=ROOT/'data'
ESPN='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/'
ALIASES={'GS':'GSW','PHO':'PHX','NO':'NOP','NY':'NYK','SA':'SAS','WSH':'WAS'}
def canonical(x):
 x=str(x or '').upper().strip();return ALIASES.get(x,x)
def get(url):
 req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Accept':'application/json'})
 with urllib.request.urlopen(req,timeout=28) as response:return json.load(response)
def num(x):
 try:return float(str(x).replace(',',''))
 except:return 0.
def attempts(s):
 x=str(s or '')
 try:return float(x.split('-')[1]) if '-' in x else num(x)
 except:return 0.
def load_json(name):
 p=DATA/name
 return json.loads(p.read_text()) if p.exists() else {}
# Historical player positions from exported slate: position is a historical snapshot and can go stale.
positions={}
path=ROOT/'data'/'player_positions.csv'
if path.exists():
 with path.open(encoding='utf-8-sig',newline='') as f:
  for r in csv.DictReader(f):positions[re.sub(r'[^a-z0-9]','',r['name'].lower())]=r['position'].split('/')[0].upper()
old=load_json('last_season_stats.json');recent=load_json('player_stats.json')
logs=old.get('game_logs',[])+recent.get('game_logs',[])
seen={(str(r.get('event_id')),str(r.get('id') or r.get('name'))):r for r in logs if r.get('event_id')}
by_event=collections.defaultdict(list)
for r in seen.values():by_event[str(r['event_id'])].append(r)
raw=collections.defaultdict(list)
for rows in by_event.values():
 teams={canonical(r.get('team')) for r in rows if r.get('team')}
 if len(teams)!=2:continue
 a,b=sorted(teams)
 for r in rows:
  pos=positions.get(re.sub(r'[^a-z0-9]','',str(r.get('name','')).lower()))
  if not pos or pos not in ('PG','SG','SF','PF','C'):continue
  team=canonical(r.get('team'));opponent=b if team==a else a
  fp=num(r.get('fantasy_points'));mins=num(r.get('minutes'))
  if mins>0:raw[(opponent,pos)].append(fp)
position_league=collections.defaultdict(list)
for (t,p),values in raw.items():position_league[p].extend(values)
dvp=[]
for (team,pos),values in sorted(raw.items()):
 # Avoid inadequate samples and report observations, not opponent games.
 league=position_league[pos]
 if len(values)<5 or len(league)<30:continue
 dvp.append({'opponent':team,'position':pos,'fp_allowed':round(statistics.mean(values),2),
    'league_avg':round(statistics.mean(league),2),'games':len(values)})
# Derive usage from player FGA, FTA, TOV, team totals and on-court mins.
# Game usage percentage approximation, not NBA official USG%. If ESPN stats absent -> skip.
from zoneinfo import ZoneInfo
now=datetime.datetime.now(ZoneInfo('America/New_York'))
usage_logs=collections.defaultdict(list)
errors=[]
for days_ago in range(1,9):
 day=(now.date()-datetime.timedelta(days=days_ago)).strftime('%Y%m%d')
 try:board=get(ESPN+'scoreboard?dates='+day)
 except Exception as e:errors.append(f'{day}: {e}');continue
 for game in board.get('events',[]):
  if not game.get('status',{}).get('type',{}).get('completed'):continue
  if game.get('season',{}).get('type') not in (None,2):continue
  try:data=get(ESPN+'summary?event='+str(game['id']))
  except Exception as e:errors.append(f"{game.get('id')}: {e}");continue
  for block in data.get('boxscore',{}).get('players',[]):
   buckets=[]
   for group in block.get('statistics',[]):
    keys=[str(v).upper() for v in group.get('names',[])]
    for row in group.get('athletes',[]):
     if row.get('didNotPlay') or row.get('inactive'):continue
     v=dict(zip(keys,row.get('stats',[]))); athlete=row.get('athlete',{})
     m=str(v.get('MIN','0'))
     try:mins=float(m.split(':')[0])+float(m.split(':')[1])/60 if ':' in m else num(m)
     except:mins=0
     if mins<=0:continue
     fga=attempts(v.get('FG') or v.get('FGM-A') or v.get('FGMA'))
     fta=attempts(v.get('FT') or v.get('FTM-A') or v.get('FTMA'))
     tov=num(v.get('TO') or v.get('TOV'))
     buckets.append((athlete.get('displayName',''),mins,fga,fta,tov))
   total=sum(fga+.44*fta+tov for _,_,fga,fta,tov in buckets)
   team_minutes=sum(m for _,m,_,_,_ in buckets)
   if total<=0 or team_minutes<150:continue
   for name,mins,fga,fta,tov in buckets:
    # Oliver usage estimate: on-court per-minute attempts relative to team possessions.
    usg=100*(fga+.44*fta+tov)*team_minutes/(5*mins*total)
    if 0<usg<75:usage_logs[re.sub(r'[^a-z0-9]','',name.lower())].append((name,usg))
  time.sleep(.05)
usage=[]
for vals in usage_logs.values():
 if len(vals)<3:continue
 avg=sum(v for _,v in vals)/len(vals)
 # Historical/reference and projected usage same until injury-adjusted usage evidence exists.
 # This prevents using a made-up change multiplier.
 usage.append({'name':vals[-1][0],'historical_usage':round(avg,2),'projected_usage':round(avg,2),'games':len(vals)})
# Resolve today schedule from already-published game feed; no stale hardcoded opponents.
board=load_json('games.json');teams_full={
'Atlanta Hawks':'ATL','Boston Celtics':'BOS','Brooklyn Nets':'BKN','Charlotte Hornets':'CHA','Chicago Bulls':'CHI',
'Cleveland Cavaliers':'CLE','Dallas Mavericks':'DAL','Denver Nuggets':'DEN','Detroit Pistons':'DET',
'Golden State Warriors':'GSW','Houston Rockets':'HOU','Indiana Pacers':'IND','LA Clippers':'LAC',
'Los Angeles Clippers':'LAC','Los Angeles Lakers':'LAL','Memphis Grizzlies':'MEM','Miami Heat':'MIA',
'Milwaukee Bucks':'MIL','Minnesota Timberwolves':'MIN','New Orleans Pelicans':'NOP','New York Knicks':'NYK',
'Oklahoma City Thunder':'OKC','Orlando Magic':'ORL','Philadelphia 76ers':'PHI',
'Phoenix Suns':'PHX','Portland Trail Blazers':'POR','Sacramento Kings':'SAC','San Antonio Spurs':'SAS',
'Toronto Raptors':'TOR','Utah Jazz':'UTA','Washington Wizards':'WAS'}
opponents=[]
for game in board.get('games',[]):
 a=teams_full.get(game.get('away'));h=teams_full.get(game.get('home'))
 if a and h:opponents.extend([{'team':a,'opponent':h},{'team':h,'opponent':a}])
output={'updated_at':now.isoformat(),'source':'ESPN unofficial completed NBA box scores and local FanDuel positional mapping',
 'dvp_method':'Mean fantasy points allowed per player-game by nominal position; not pace or role adjusted',
 'usage_method':'Box-score estimated possession share, current-season sample only; projected_usage equals historical_usage until separately verified change',
 'opponents':opponents,'dvp':dvp,'usage':usage,'source_dates':{'schedule':board.get('date'),'historical_coverage':old.get('coverage','unknown')},'errors':errors[:5]}
DATA.mkdir(exist_ok=True)
(DATA/'matchup_usage.json').write_text(json.dumps(output,indent=2)+'\n')
print('Saved opponents',len(opponents),'DvP',len(dvp),'usage records',len(usage),'errors',len(errors))
