#!/usr/bin/env python3
"""Public NBA DFS media radar. Only public RSS headlines/snippets. No paywall bypass.
Mentions are NOT observed ownership or validated projected ownership.
"""
import csv, json, re, urllib.parse, urllib.request, xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
ROOT=Path(__file__).resolve().parent.parent
DATA=ROOT/'data'
# Site-specific searches. A provider may have no accessible public reports.
SOURCES={'RotoGrinders':'rotogrinders.com','Stokastic':'stokastic.com','FantasyLabs':'fantasylabs.com','NumberFire':'numberfire.com','RotoWire':'rotowire.com','FantasyPros':'fantasypros.com','SportsLine':'sportsline.com'}
HEADERS={'User-Agent':'NBAEdgeResearch/1.0 (public RSS citations only)','Accept':'application/rss+xml, application/xml, text/xml'}
def normalize(s):
 return re.sub(r'[^a-z0-9]','',str(s or '').lower())
def clean(s):
 return re.sub(r'\s+',' ',re.sub(r'<[^>]+>',' ',str(s or ''))).strip()
def fetch(domain):
 query=f'site:{domain} NBA DFS (picks OR plays OR value OR projections OR ownership) when:2d'
 url='https://news.google.com/rss/search?q='+urllib.parse.quote(query)+'&hl=en-US&gl=US&ceid=US:en'
 req=urllib.request.Request(url,headers=HEADERS)
 with urllib.request.urlopen(req,timeout=22) as r:payload=r.read(1_000_000)
 root=ET.fromstring(payload)
 result=[]
 for item in root.findall('./channel/item')[:35]:
  title=clean(item.findtext('title'));body=clean(item.findtext('description'));link=item.findtext('link') or ''
  try:dt=parsedate_to_datetime(item.findtext('pubDate')).astimezone(timezone.utc)
  except:continue
  if dt<datetime.now(timezone.utc)-timedelta(hours=48):continue
  result.append({'title':title,'snippet':body[:400],'url':link,'published_at':dt.isoformat()})
 return result

def main():
 position_csv=DATA/'player_positions.csv'
 player_names=[]
 if position_csv.exists():
  with position_csv.open(encoding='utf-8-sig',newline='') as f:
   for r in csv.DictReader(f):
    n=(r.get('name') or '').strip()
    if n and n not in player_names:player_names.append(n)
 # Optional slate CSV may be committed to data/slate_names.csv (names only; no salary info needed).
 optional=DATA/'slate_names.csv'
 if optional.exists():
  with optional.open(encoding='utf-8-sig',newline='') as f:
   for row in csv.DictReader(f):
    n=(row.get('name') or '').strip()
    if n and n not in player_names:player_names.append(n)
 mention={n:{'name':n,'sources':set(),'articles':[]} for n in player_names}
 records=[];errors={};seen=set()
 for provider,domain in SOURCES.items():
  try:articles=fetch(domain)
  except Exception as e:errors[provider]=str(e)[:160];continue
  for a in articles:
   key=(provider,a['title'].lower())
   if key in seen:continue
   seen.add(key)
   # Match exact player names with tolerant spaces/apostrophes, but only in article titles;
   # generic headline mentions are not endorsements and get a conservative signal.
   hay=' '+re.sub(r'[^a-z0-9 ]',' ',a['title'].lower())+' '
   matched=[]
   for name in player_names:
    target=re.sub(r'[^a-z0-9 ]',' ',name.lower()).strip()
    if target and re.search(r'(?<![a-z])'+re.escape(target)+r'(?![a-z])',hay):matched.append(name)
   if not matched:continue
   row=dict(a,provider=provider,mentioned_players=matched)
   records.append(row)
   for n in matched:
    mention[n]['sources'].add(provider)
    mention[n]['articles'].append({'provider':provider,'title':a['title'],'url':a['url'],'published_at':a['published_at']})
 people=[]
 for v in mention.values():
  if not v['articles']:continue
  people.append({'name':v['name'],'source_count':len(v['sources']),'mention_count':len(v['articles']),'articles':v['articles'][:8]})
 people.sort(key=lambda p:(-p['source_count'],-p['mention_count'],p['name']))
 output={'updated_at':datetime.now(timezone.utc).isoformat(),'window_hours':48,'method':'Player names in recent public article HEADLINES only; one source per provider. Mentions are not endorsements or ownership percentages.','sources_checked':list(SOURCES),'source_errors':errors,'players':people,'article_count':len(records)}
 DATA.mkdir(exist_ok=True)
 (DATA/'public_consensus.json').write_text(json.dumps(output,indent=2)+'\n',encoding='utf-8')
 print('Public consensus sources',len(SOURCES)-len(errors),'matched articles',len(records),'players',len(people))
if __name__=='__main__':main()
