/* 現金水位儀 — service worker
   策略：app shell 預快取 + cache-first(離線可開);
   跨來源資料(VIX/S&P CSV)走網路直通,由頁面端 localStorage 負責離線回退,確保資料新鮮。 */
const CACHE = "cwgauge-shell-v1";
const SHELL = [
  "./",
  "./index.html",
  "./manifest.json",
  "./icon-192.png",
  "./icon-512.png"
];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  // 跨來源(githubusercontent 資料源):直通網路,不攔截 → 頁面自行做離線回退
  if (url.origin !== self.location.origin) return;

  // 同源 app shell:cache-first,背景更新
  e.respondWith(
    caches.match(req).then(hit => {
      const net = fetch(req).then(res => {
        if (res && res.status === 200) {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(req, copy));
        }
        return res;
      }).catch(() => hit);
      return hit || net;
    })
  );
});
