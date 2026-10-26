// ============================================================================
//  現金水位儀 · Service Worker
//  策略:HTML 網路優先(部署新版立即生效)· 靜態檔快取優先 · 資料 API 不快取
//  ⚠ 每次改動 index.html 後,把下面版本號 +1(v4→v5),舊快取才會被清掉!
// ============================================================================
const CACHE = "cwgauge-v23";
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

// 只有真正成功的回應才准進快取。
// 少了這道判斷,一次 404 就會被當成正常內容存起來,而 ③ 是快取優先 ——
// 之後就算檔案已經部署上去,SW 仍會一直回那個 404,直到版本號被 +1 為止。
// 排除 206:Range 回應存進 Cache API 會直接拋例外。
function cacheable(res) {
  return res.status === 200;
}

// 寫快取失敗就放棄,不要變成沒人看得到的 unhandled rejection。
// `cacheable()` 已經擋掉 206(Cache.put 對 206 會拋),但配額超限、
// 使用者清掉 storage、私密模式都還是會讓 put reject。
//
// 這個 catch 吞得起:快取【寫】失敗的後果只是「下次還要走網路」,
// 不會產生錯誤資料。這和「別把失敗存進快取」是兩件不同的事。
function store(req, res) {
  const copy = res.clone();
  caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
}

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  // ① 跨網域(資料 API:本專案資料鏡像 / 自有 Worker / TwelveData / Yahoo 經代理 /
  //    GitHub 上的 Shiller …)→ 完全放行,SW 不介入、不快取,確保每次拿到最新資料。
  //
  //    ⚠ 2026-10-19 起主要資料來自 raw.githubusercontent.com 的資料鏡像,
  //      那仍然是跨網域,所以照樣走這一條。**不要為了「加速」把它快取起來** ——
  //      鏡像每天更新,而這個 SW 的 ③ 是快取優先:存進去就會一直回舊的那份,
  //      直到有人想起來 bump CACHE 為止。離線時的保底是 PWA 自己的
  //      localStorage 快取(load() 的 catch),不是 SW。
  if (url.origin !== self.location.origin) return;

  // ② HTML / 導覽 → 網路優先(重部署立即生效),離線才退回快取
  if (req.mode === "navigate" || url.pathname.endsWith("/") || url.pathname.endsWith("index.html")) {
    e.respondWith(
      fetch(req)
        .then((res) => {
          if (cacheable(res)) store(req, res);             // 別把 404 頁面存成離線首頁
          return res;
        })
        // 離線 → 先找這個網址、再退回離線首頁。
        // ⚠ 最後那個 `|| Response.error()` 不是裝飾:install 的 addAll 是原子的,
        //    而它整批失敗時 `.catch(() => {})` 會無聲吞掉 —— 於是「使用者第一次
        //    開 PWA 時就離線」或「Pages 部署空窗期」會留下一個【完全空的快取】。
        //    那時兩次 match 都是 undefined,respondWith 就收到 Promise<undefined>,
        //    而規格說那不是 Response 就直接算網路錯誤。
        //    分支 ③ 本來就有這層兜底,② 沒有 —— 兩個分支不一致遲早會變成 bug。
        //    (2026-10-19 由 sw_bridge 的 navigate_offline_no_shell 實測到。)
        .catch(() =>
          caches.match(req)
            .then((r) => r || caches.match("./index.html"))
            .then((r) => r || Response.error())
        )
    );
    return;
  }

  // ③ 其他同網域靜態檔(icon、manifest)→ 快取優先,沒有才抓網路並存起來
  e.respondWith(
    caches.match(req).then((cached) =>
      cached ||
      fetch(req)
        .then((res) => {
          if (cacheable(res)) store(req, res);             // 快取優先 → 存錯了就一直錯
          return res;
        })
        // 離線且這個檔案不在快取裡(例如 SHELL 漏了它)→ 至少別讓整個
        // respondWith 以 reject 收場,那會變成一個沒人接手的失敗。
        //
        // ⚠ 這裡【不要】再 caches.match(req) 一次。這條分支是快取優先 ——
        //    能走到 fetch 就表示外層那次 match 已經 miss 了,再查一次必然還是
        //    miss。那段是死碼。(2026-10-19 突變測試發現:把它拿掉後所有測試
        //    行為完全相同 —— 突變「逃掉」是因為兩種寫法等價,不是測試不夠力。)
        .catch(() => Response.error())
    )
  );
});
