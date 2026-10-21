#!/usr/bin/env python3
"""Fetch recent completed NBA box scores. Historical observations only, not forecasts."""
import datetime, json, pathlib, time, urllib.request
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/'
NOW = datetime.datetime.now(ZoneInfo('America/New_York'))

def fetch_json(url):
    request = urllib.request.Request(url, headers={'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.load(response)

def number(value):
    try: return float(str(value).replace(',',''))
    except (TypeError, ValueError): return 0.0

def minutes(value):
    text=str(value or '0')
    if ':' in text:
        left,right=text.split(':',1)
        return round(number(left)+number(right)/60,2)
    return number(text)

def parse_summary(data, event_id, game_date):
    rows=[]
    for team in data.get('boxscore',{}).get('players',[]):
        team_name=team.get('team',{}).get('abbreviation', '')
        for stat_group in team.get('statistics',[]):
            names=[str(x).upper() for x in stat_group.get('names',[])]
            for row in stat_group.get('athletes',[]):
                athlete=row.get('athlete',{})
                vals=dict(zip(names,row.get('stats',[])))
                if not vals or row.get('didNotPlay') or row.get('inactive'): continue
                def get(*keys):
                    return next((number(vals[k]) for k in keys if k in vals),0.0)
                mins=minutes(vals.get('MIN',0))
                if mins<=0:continue
                pts=get('PTS'); reb=get('REB'); ast=get('AST'); stl=get('STL'); blk=get('BLK'); tov=get('TO','TOV')
                # Standard FanDuel NBA scoring, not platform-specific historical bonus variations.
                fantasy=pts+1.2*reb+1.5*ast+3*stl+3*blk-tov
                rows.append({'id':str(athlete.get('id','')),'name':athlete.get('displayName','Unknown'),
                    'team':team_name,'date':game_date,'event_id':str(event_id),
                    'minutes':round(mins,2),'pts':pts,'reb':reb,'ast':ast,'stl':stl,'blk':blk,
                    'tov':tov,'fantasy_points':round(fantasy,2)})
    return rows

def main():
    all_rows=[]; games_checked=0; errors=[]
    # Previous 7 days, because today's active games are generally not final yet.
    for days_ago in range(1,8):
        day=(NOW.date()-datetime.timedelta(days=days_ago)).strftime('%Y%m%d')
        try:
            events=fetch_json(BASE+'scoreboard?dates='+day).get('events',[])
        except Exception as e:
            errors.append(f'{day} scoreboard: {e}'); continue
        for event in events:
            if not event.get('status',{}).get('type',{}).get('completed',False): continue
            game_id=event.get('id')
            if not game_id:continue
            try:
                summary=fetch_json(BASE+'summary?event='+str(game_id))
                rows=parse_summary(summary,game_id,day)
                all_rows.extend(rows); games_checked+=1
            except Exception as e:
                errors.append(f'{game_id}: {e}')
            time.sleep(0.15)
    if not games_checked and errors:
        raise RuntimeError('No box scores retrieved; refusing to overwrite earlier stats. '+errors[0])
    latest={}
    for row in all_rows:
        key=(row['id'],row['event_id'])
        latest[key]=row
    history=sorted(latest.values(),key=lambda x:(x['date'],x['name']),reverse=True)
    aggregates={}
    for row in history:
        key=row['id'] or row['name']
        p=aggregates.setdefault(key,{'id':row['id'],'name':row['name'],'team':row['team'],'games':0,'total_minutes':0.,'total_fp':0.,'total_pts':0.})
        p['games']+=1;p['total_minutes']+=row['minutes'];p['total_fp']+=row['fantasy_points'];p['total_pts']+=row['pts']
    players=[]
    for p in aggregates.values():
        count=p['games']; played=p['total_minutes']; players.append({
            'id':p['id'],'name':p['name'],'team':p['team'],'games':count,
            'avg_minutes':round(played/count,1),
            'avg_fantasy_points':round(p['total_fp']/count,1),
            'fantasy_points_per_minute':round(p['total_fp']/played,3) if played else 0,
            'avg_points':round(p['total_pts']/count,1)})
    players.sort(key=lambda p:p['avg_fantasy_points'],reverse=True)
    result={'updated_at':NOW.isoformat(),'source':'ESPN public unofficial game box scores',
        'description':'Observed player stats from completed games in the previous 7 days, not projections, salaries or ownership',
        'games_processed':games_checked,'errors':errors[:8],'players':players,'game_logs':history}
    path=ROOT/'data/player_stats.json';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(f'Saved {len(players)} players from {games_checked} games to {path}')

if __name__=='__main__':main()
