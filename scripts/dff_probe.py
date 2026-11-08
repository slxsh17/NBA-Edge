"""One-off diagnostic: find the feed DFF's page uses to list a day's slates."""
import re, sys, gzip, urllib.request
for url in ('https://dff-common.s3.amazonaws.com/projections_mini.gz.js?734f96', 'https://dff-common.s3.amazonaws.com/base_mini.gz.js?m0nksd'):
    raw = urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=30).read()
    try:
        js = gzip.decompress(raw).decode('utf-8', 'replace')
    except OSError:
        js = raw.decode('utf-8', 'replace')
    print('JS', url, len(js))
    seen = set()
    for m in re.finditer(r'all-slates-t|all-showdown|slates-target|\.ajax\(|\$\.get\w*\(|\$\.post\(|url\s*:\s*["\']', js):
        s = re.sub(r'\s+', ' ', js[max(0, m.start() - 300):m.end() + 300])
        if s[:80] in seen:
            continue
        seen.add(s[:80])
        print('  ctx:', s[:620])
