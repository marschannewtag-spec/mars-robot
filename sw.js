// ============================================================================
//  現金水位儀 · Service Worker
//  策略:HTML 網路優先(部署新版立即生效)· 靜態檔快取優先 · 資料 API 不快取
//  ⚠ 每次改動 index.html 後,把下面版本號 +1(v4→v5),舊快取才會被清掉!
// ============================================================================
const CACHE = "cwgauge-v8";
const SHELL = ["./", "./index.html", "./manifest.json", "./icon-192.png", "./icon-512.png"];

self.addEventListener("install", (e) => {
  self.skipWaiting();                                   // 新 SW 立即接管
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())                 // 清掉所有舊版本快取
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  // ① 跨網域(資料 API:自有 Worker / Yahoo / github VIX·Shiller / FRED …)
  //    → 完全放行,SW 不介入、不快取,確保每次拿到最新資料
  if (url.origin !== self.location.origin) return;

  // ② HTML / 導覽 → 網路優先(重部署立即生效),離線才退回快取
  if (req.mode === "navigate" || url.pathname.endsWith("/") || url.pathname.endsWith("index.html")) {
    e.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
          return res;
        })
        .catch(() => caches.match(req).then((r) => r || caches.match("./index.html")))
    );
    return;
  }

  // ③ 其他同網域靜態檔(icon、manifest)→ 快取優先,沒有才抓網路並存起來
  e.respondWith(
    caches.match(req).then((cached) =>
      cached ||
      fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
        return res;
      })
    )
  );
});
