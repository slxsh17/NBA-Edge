#!/usr/bin/env python3
"""Public NBA starting-five and rotation headline snapshot for NBA EDGE.

This is not an X/Twitter API client. The free mode reads a public lineup page
and recent Google News RSS headlines. It does not infer confirmed lineup status
from an unverified headline. Fail closed on source or date mismatch.
"""
import datetime as dt
import email.utils
import html
import json
import os
import pathlib
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

OUT = pathlib.Path('data/lineup_news.json')
TEAMS = {'ATL','BOS','BKN','CHA','CHI','CLE','DAL','DEN','DET','GSW','HOU','IND','LAC','LAL','MEM','MIA','MIL','MIN','NOP','NYK','OKC','ORL','PHI','PHX','POR','SAC','SAS','TOR','UTA','WAS'}
ALIASES = {'GS':'GSW','NO':'NOP','NY':'NYK','PHO':'PHX','SA':'SAS','UTAH':'UTA','WSH':'WAS','BRK':'BKN'}

def normalize_team(code):
    c = re.sub('[^A-Z]','',str(code).upper())
    return ALIASES.get(c,c)

def utc_now():
    return dt.datetime.now(dt.timezone.utc)

def get(url, timeout=20):
    req = urllib.request.Request(url, headers={
        'User-Agent':'Mozilla/5.0 (compatible; NBAEdgeResearch/1.0; data research)',
        'Accept':'text/html,application/rss+xml,application/xml;q=0.9,*/*;q=0.5',
    })
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read(2_500_000).decode('utf-8','replace')

def lineup_source(raw, slate_date):
    soup = BeautifulSoup(raw,'html.parser')
    # The public page can be cached at the previous slate date; fail closed.
    heading=soup.get_text(' ',strip=True)[:1200]
    found=re.search(r'Starting lineups for\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})',heading,re.I)
    if found:
        try:
            page_day=dt.datetime.strptime(found.group(1),'%B %d, %Y').date().isoformat()
            if page_day!=slate_date:return []
        except ValueError:return []
    lineups=[]
    for match in soup.select('.lineup__box'):
        visitor=match.select_one('.lineup__team.is-visit')
        home=match.select_one('.lineup__team.is-home')
        if not visitor or not home: continue
        teams=[]
        for el in (visitor,home):
            # RotoWire displays 2-4 letter team abbreviations. Strictly reject unknown.
            tokens=re.findall(r'\b[A-Z]{2,4}\b',el.get_text(' ',strip=True).upper())
            code=next((normalize_team(x) for x in tokens if normalize_team(x) in TEAMS),None)
            teams.append(code)
        if not all(teams): continue
        for loc,code in [('is-visit',teams[0]),('is-home',teams[1])]:
            block=match.select_one('ul.lineup__list.'+loc)
            if block is None: continue
            players=[]
            for li in block.select('li'):
                a=li.select_one('a')
                if a is None: continue
                name=a.get_text(' ',strip=True)
                if not name or len(name)<4 or name.lower().startswith(('subscribe','lineup')):continue
                if name not in players: players.append(name)
            # Only five starters count as a complete lineup. Never infer bench
            # from an incomplete page.
            if len(players)!=5:continue
            status_el=match.select_one('.lineup__status.'+loc)
            if status_el is None:
                status_list=match.select('.lineup__status')
                if len(status_list)==2:status_el=status_list[0 if loc=='is-visit' else 1]
            # If no side-specific status exists, don't imply either team confirmed.
            context=(status_el.get_text(' ',strip=True) if status_el else '').lower()
            status='confirmed' if 'confirmed lineup' in context else 'projected' if any(v in context for v in ('expected lineup','projected lineup')) else 'unknown'
            if status=='unknown':continue
            lineups.append({'team':code,'status':status,'players':players,'source':'RotoWire NBA Daily Lineups','source_url':'https://www.rotowire.com/basketball/nba-lineups.php','date':slate_date})
    # One report per team, confirmed wins when both found.
    result={}
    for x in lineups:
        old=result.get(x['team'])
        if not old or (old['status']!='confirmed' and x['status']=='confirmed'):result[x['team']]=x
    return list(result.values())

def news_source(xml_text, slate_day, now=None):
    now=now or utc_now()
    root=ET.fromstring(xml_text)
    out=[]
    words=re.compile(r'\b(starting (?:five|lineup)|will start|to start|gets (?:the )?start|starting tonight|named (?:a )?starter|minutes? (?:restriction|limit|cap)|limited (?:to|minutes)|rotation|moved to (?:the )?bench|benched|won.t start|late scratch|ruled out|out (?:tonight|for)|will(?: not|n.t) play|questionable|doubtful|game-time decision|expected to (?:play|return)|(?:to|will) return|cleared to|upgraded|downgraded|sidelined|injur(?:y|ed)|sprain|available (?:tonight|vs))\b',re.I)
    seen=set()
    for item in root.findall('.//item')[:150]:
        title=html.unescape(re.sub('<[^>]+>','',item.findtext('title') or '')).strip()
        link=(item.findtext('link') or '').strip()
        posted=(item.findtext('pubDate') or '').strip()
        if not words.search(title) or not link.startswith('https://'):continue
        try:
            when=email.utils.parsedate_to_datetime(posted)
            if when.tzinfo is None:when=when.replace(tzinfo=dt.timezone.utc)
            if abs((now-when).total_seconds())>24*3600:continue
        except Exception:continue
        key=re.sub(r'\W+','',title.casefold())
        if key in seen:continue
        seen.add(key)
        out.append({'headline':title[:210],'source_url':link,'published_at':when.isoformat(),'type':'headline_only','confirmed':False,'scope_date':slate_day})
    return out[:120]

def build_snapshot(day=None, lineup_html=None, news_xml=None):
    now=utc_now()
    day=day or now.astimezone(ZoneInfo('America/New_York')).strftime('%Y-%m-%d')
    errors=[];groups=[];news=[]
    if lineup_html is None:
        try:
            lineup_html=get('https://www.rotowire.com/basketball/nba-lineups.php')
        except Exception as ex:errors.append('lineups: '+type(ex).__name__+': '+str(ex)[:120])
    if lineup_html:
        try:groups=lineup_source(lineup_html,day)
        except Exception as ex:errors.append('lineup parse: '+str(ex)[:120])
    feeds=[news_xml] if news_xml is not None else []
    if news_xml is None:
        # Several focused searches: starters, minutes limits, injury/availability news.
        for search in ('NBA ("starting lineup" OR "will start" OR "to start" OR "moved to the bench" OR "rotation") when:1d',
                       'NBA ("minutes restriction" OR "minutes limit" OR "limited minutes" OR "minutes cap") when:2d',
                       'NBA ("ruled out" OR "questionable" OR "doubtful" OR "game-time decision" OR "will not play" OR "expected to play" OR "will return") when:1d'):
            query=urllib.parse.urlencode({'q':search,'hl':'en-US','gl':'US','ceid':'US:en'})
            try:feeds.append(get('https://news.google.com/rss/search?'+query))
            except Exception as ex:errors.append('headlines: '+type(ex).__name__+': '+str(ex)[:120])
    seen=set()
    for xml_text in feeds:
        try:
            for h in news_source(xml_text,day,now):
                k=re.sub(r'\W+','',h['headline'].casefold())
                if k not in seen:
                    seen.add(k);news.append(h)
        except Exception as ex:errors.append('headline parse: '+str(ex)[:120])
    news.sort(key=lambda h:h['published_at'],reverse=True)
    return {'schema':1,'slate_date':day,'checked_at':now.isoformat(),
        'lineup_provider':'RotoWire public lineup page',
        'headlines_provider':'Google News public RSS (not a direct X feed)',
        'lineups':groups,'headlines':news,'source_errors':errors,
        'disclaimer':'Projected lineups and headlines are not confirmations. Only teams marked confirmed qualify for small starter adjustments. A source failure must never imply a player is out.'}

def main():
    d=os.environ.get('NBA_EDGE_SLATE_DATE')
    result=build_snapshot(d)
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Lineups:',len(result['lineups']),'confirmed:',sum(x['status']=='confirmed' for x in result['lineups']),
          'news headlines:',len(result['headlines']),'source errors:',result['source_errors'])
    if not result['lineups'] and not result['headlines']:
        print('WARNING: No usable starting-lineup or minute-news evidence. No adjustments should be made.')

if __name__=='__main__':main()
