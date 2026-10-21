#!/usr/bin/env python3
"""Incrementally backfill 2025-26 NBA regular-season player game logs.
Unverified ESPN public feed. Bounded requests per invocation; checkpoint progress.
"""
import datetime, json, pathlib, time, os
from update_player_stats import BASE, fetch_json, parse_summary
ROOT=pathlib.Path(__file__).resolve().parent.parent
DEST=ROOT/'data'/'last_season_stats.json'
START=datetime.date(2025,10,21)
END=datetime.date(2026,4,12)
BATCH=int(os.getenv('BACKFILL_DAYS','12'))
if not (1<=BATCH<=31): raise ValueError('BACKFILL_DAYS must be 1..31')

def summarize(logs):
    bucket={}
    for r in logs:
        key=r['id'] or r['name']
        p=bucket.setdefault(key,{'id':r['id'],'name':r['name'],'team':r['team'],'games':0,'total_minutes':0.,'total_fp':0.})
        p['games']+=1;p['total_minutes']+=r['minutes'];p['total_fp']+=r['fantasy_points']
    players=[]
    for p in bucket.values():
        count=p['games'];mins=p['total_minutes']
        players.append({'id':p['id'],'name':p['name'],'team':p['team'],'games':count,
            'avg_minutes':round(mins/count,2),'fantasy_points_per_minute':round(p['total_fp']/mins,4) if mins else 0,
            'avg_fantasy_points':round(p['total_fp']/count,2)})
    return sorted(players,key=lambda p:p['name'])

def main():
    old=json.loads(DEST.read_text()) if DEST.exists() else {}
    days=set(old.get('completed_dates',[]))
    logs={(r['id'],r['event_id']):r for r in old.get('game_logs',[])}
    cursor=START
    checked=0;errors=[];complete_dates=[]
    while cursor<=END and checked<BATCH:
        day=cursor.strftime('%Y%m%d');cursor+=datetime.timedelta(days=1)
        if day in days:continue
        checked+=1
        try:
            board=fetch_json(BASE+'scoreboard?dates='+day)
            events=board.get('events',[])
            daylogs=[]
            for ev in events:
                # A date may contain preseason/playoffs near boundaries. Accept only regular-season events.
                season=ev.get('season',{})
                season_type=season.get('type')
                if season_type is not None and int(season_type)!=2:continue
                if not ev.get('status',{}).get('type',{}).get('completed',False):continue
                event_id=str(ev.get('id',''))
                if not event_id:continue
                summary=fetch_json(BASE+'summary?event='+event_id)
                daylogs.extend(parse_summary(summary,event_id,day))
                time.sleep(.12)
            for r in daylogs:logs[(r['id'],r['event_id'])]=r
            complete_dates.append(day)
        except Exception as exc:
            errors.append(day+': '+str(exc))
        time.sleep(.08)
    if not complete_dates and not old.get('completed_dates'):
        raise RuntimeError('Historical fetch did not complete any dates: '+str(errors[:2]))
    days.update(complete_dates)
    result={'season':'2025-26','season_type':'regular','updated_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'completed_dates':sorted(days),'date_count':len(days),'expected_date_count':(END-START).days+1,
        'coverage':'partial' if len(days)<(END-START).days+1 else 'complete',
        'source':'ESPN unofficial completed regular-season NBA box scores',
        'errors':errors[:12],'players':summarize(logs.values()),
        'game_logs':sorted(logs.values(),key=lambda r:(r['date'],r['id']))}
    DEST.parent.mkdir(parents=True,exist_ok=True)
    DEST.write_text(json.dumps(result,separators=(',',':'))+'\n')
    print('Historical dates',len(days),'of',result['expected_date_count'],'player logs',len(logs),'errors',len(errors))

if __name__=='__main__':main()
