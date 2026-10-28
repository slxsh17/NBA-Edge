// NBA EDGE v27. Network-first; offline fallback. Cache entries are keyed by path
// (query strings ignored) so cache-busting ?t= requests no longer pile up copies.
const CACHE='nba-edge-v38';
const ASSETS=['./','./index.html','./manifest.webmanifest','./icon-192.png','./icon-512.png'];
const keyFor=req=>{const u=new URL(req.url);u.search='';return u.toString()};
self.addEventListener('install',e=>{e.waitUntil(caches.open(CACHE).then(c=>c.addAll(ASSETS)));self.skipWaiting()});
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',e=>{
  const req=e.request;
  if(req.method!=='GET'||new URL(req.url).origin!==self.location.origin)return;
  const key=keyFor(req);
  e.respondWith(fetch(req).then(r=>{
    if(r.ok){const copy=r.clone();caches.open(CACHE).then(c=>c.put(key,copy))}
    return r;
  }).catch(()=>caches.match(key)));
});
