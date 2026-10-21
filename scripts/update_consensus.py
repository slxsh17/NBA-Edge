#!/usr/bin/env python3
"""Conservative public NBA DFS *recommendation* monitor, no paywall bypass.
Only counts player-specific picks supported by parsed article headings or explicit
recommendation language in public page text. Inaccessible pages are NOT counted.
"""
import csv, html, json, re, urllib.parse, urllib.request, xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
ROOT=Path(__file__).resolve().parent.parent; DATA=ROOT/'data'
SOURCES={
 'RotoBaller':'rotoballer.com', 'RotoWire':'rotowire.com',
 'FantasyLabs':'fantasylabs.com', 'RotoGrinders':'rotogrinders.com',
 'Stokastic':'stokastic.com', 'DraftKings Network':'dknetwork.draftkings.com',
 'FantasyPros':'fantasypros.com', 'NumberFire':'numberfire.com',
 'SportsLine':'sportsline.com',
}
HEADERS={'User-Agent':'Mozilla/5.0 (compatible; NBAEdgeResearch/2.0; public articles only)','Accept':'text/html,application/rss+xml,application/xml'}
PICK_WORDS=re.compile(r'\b(?:top picks?|best picks?|core play|value plays?|bargain plays?|top plays?|favorite plays?|must.play|target|recommended|recommend|strong plays?|studs?|sleepers?|cash plays?|tournament plays?|gpp plays?|dfs picks?|priority plays?|building blocks?|top option|my favorite|love (?:the )?spot)\b',re.I)
NEGATIVE=re.compile(r'\b(?:avoid|fade|do not play|don.t play|not a play|stay away|overrated|injured|ruled out|questionable|doubtful)\b',re.I)
DFS=re.compile(r'\b(?:nba\s+dfs|fantasy basketball|draftkings|fanduel|daily fantasy|dfs\s+(?:picks|plays|lineups|targets)|nba fantasy)\b',re.I)
def clean(s):return re.sub(r'\s+',' ',html.unescape(re.sub(r'<[^>]*>',' ',str(s or '')))).strip()
class Reader(HTMLParser):
 def __init__(self):super().__init__();self.tag=None;self.stack=[];self.buf=[];self.blocks=[];self.ignore=0
 def handle_starttag(self,tag,attrs):
  self.stack.append(tag)
  if tag in ('script','style','nav','footer'):self.ignore+=1
  if tag in ('h1','h2','h3','h4','p','li') and not self.ignore:self.buf.append([tag,''])
 def handle_data(self,data):
  if not self.ignore and self.buf:self.buf[-1][1]+=data
 def handle_endtag(self,tag):
  if self.buf and self.buf[-1][0]==tag:self.blocks.append(tuple(self.buf.pop()))
  if tag in ('script','style','nav','footer') and self.ignore:self.ignore-=1
  if self.stack:self.stack.pop()
def names():
 result=[]
 for p in (DATA/'player_positions.csv',DATA/'slate_names.csv'):
  if p.exists():
   with p.open(encoding='utf-8-sig',newline='') as f:
    for row in csv.DictReader(f):
     n=(row.get('name') or '').strip()
     if n and n not in result:result.append(n)
 return result
def rss(domain):
 query=f'site:{domain} ("NBA DFS" OR "NBA fantasy") (picks OR plays OR targets OR values) when:2d'
 url='https://news.google.com/rss/search?q='+urllib.parse.quote(query)+'&hl=en-US&gl=US&ceid=US:en'
 with urllib.request.urlopen(urllib.request.Request(url,headers=HEADERS),timeout=18) as r:feed=r.read(800000)
 root=ET.fromstring(feed);out=[]
 for item in root.findall('./channel/item')[:20]:
  title=clean(item.findtext('title'));link=item.findtext('link') or ''
  try:published=parsedate_to_datetime(item.findtext('pubDate')).astimezone(timezone.utc)
  except (ValueError,TypeError):continue
  if published<datetime.now(timezone.utc)-timedelta(hours=48):continue
  if DFS.search(title) or PICK_WORDS.search(title):out.append((title,link,published.isoformat()))
 return out
def fetch_page(url,domain):
 try:
  with urllib.request.urlopen(urllib.request.Request(url,headers=HEADERS),timeout=12) as r:
   final=r.geturl();content=r.read(450000).decode('utf-8','replace')
  if not urllib.parse.urlparse(final).hostname.endswith(domain):return None,None
  reader=Reader();reader.feed(content)
  return final,[(tag,clean(text)) for tag,text in reader.blocks if len(clean(text))>15][:400]
 except Exception:return None,None
def make_pattern(name):
 parts=re.findall(r'[a-z0-9]+',name.lower());return re.compile(r'(?<![a-z])'+r'[\s\W]*'.join(map(re.escape,parts))+r'(?![a-z])',re.I) if parts else re.compile(r'(?!)')
def main():
 known=names();patterns={n:make_pattern(n) for n in known};results={};errors={};checked=0;hits=0
 for provider,domain in SOURCES.items():
  try:articles=rss(domain)
  except Exception as exc:errors[provider]=str(exc)[:100];continue
  for title,link,pub in articles:
   target,blocks=fetch_page(link,domain)
   if not blocks:continue
   checked+=1
   if not any(DFS.search(txt) for tag,txt in blocks[:35]) and not DFS.search(title):continue
   headings=[(ix,txt) for ix,(tag,txt) in enumerate(blocks) if tag in ('h2','h3','h4')]
   for name,pattern in patterns.items():
    evidence=[]
    for i,(tag,line) in enumerate(blocks):
     if not pattern.search(line):continue
     if tag in ('h2','h3','h4'):
      # Player-specific DFS pick heading in a verified recommendation article.
      intro=' '.join(t for _,t in blocks[:12])[:1800]
      if PICK_WORDS.search(title+' '+intro) and not NEGATIVE.search(line):evidence.append(line[:230])
     elif tag in ('p','li') and len(line)<650 and PICK_WORDS.search(line) and not NEGATIVE.search(line):
      evidence.append(line[:230])
    if not evidence:continue
    hits+=1
    record=results.setdefault(name,{'name':name,'sources':set(),'articles':[]})
    if any(a['provider']==provider and a['url']==target for a in record['articles']):continue
    record['sources'].add(provider)
    record['articles'].append({'provider':provider,'title':title,'url':target,'published_at':pub,'evidence':evidence[0],'signal':'explicit_pick_context'})
 people=[]
 for r in results.values():
  people.append({'name':r['name'],'source_count':len(r['sources']),'mention_count':len(r['articles']),'recommendation_count':len(r['articles']),'articles':r['articles'][:8]})
 people.sort(key=lambda r:(-r['source_count'],-r['recommendation_count'],r['name']))
 output={'updated_at':datetime.now(timezone.utc).isoformat(),'window_hours':48,'method':'Conservative public DFS pick recognition in publicly accessible article headings/paragraphs; not direct ownership predictions. Unreadable articles excluded.','sources_checked':list(SOURCES),'source_errors':errors,'articles_checked':checked,'players':people,'article_count':sum(len(r['articles']) for r in people)}
 DATA.mkdir(exist_ok=True)
 (DATA/'public_consensus.json').write_text(json.dumps(output,indent=2)+'\n',encoding='utf-8')
 print('Recommendation articles inspected:',checked,'unique players:',len(people),'verified recommendations:',output['article_count'])
if __name__=='__main__':main()
