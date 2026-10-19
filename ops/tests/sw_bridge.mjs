/**
 * Service Worker 橋接器 —— 在 Node 裡載入【線上真的在跑的那個 sw.js】,
 * 給它一組假的 Cache API 與假的網路,然後真的派送 fetch 事件,
 * 最後把「快取裡到底存了什麼」吐成 JSON 給 Python 判讀。
 *
 * 為什麼不用 grep 原始碼:快取污染是**行為**問題,不是字串問題。
 * 「有沒有寫 res.ok」這種檢查,任何一次改寫分支結構都會失效卻仍然通過。
 * 這裡問的是唯一重要的那件事 —— 一個 404 打進來之後,快取裡有沒有多一筆。
 *
 * 用法:node tests/sw_bridge.mjs <sw.js>
 * 輸出:stdout 的 JSON
 */
import { readFileSync } from "node:fs";

const swPath = process.argv[2];
if (!swPath) {
  console.error("用法: node sw_bridge.mjs <sw.js>");
  process.exit(2);
}

const ORIGIN = "https://example.github.io";
const source = readFileSync(swPath, "utf8");

// sw.js 的 `caches.open(...).then(c => c.put(...))` 後面沒有 .catch,
// put 失敗就是一個 unhandled rejection。Node 預設會直接結束行程 ——
// 那會讓測試看到「橋接器崩潰」而不是「206 被交給了 Cache.put()」。
// 記下來、讓行程活著,把判斷交給測試,錯誤訊息才指得出真正的原因。
const unhandled = [];
process.on("unhandledRejection", (e) => unhandled.push(String(e)));

/** 最小可用的 Cache API 假件 —— 以 url 當鍵,足夠回答「存了沒」。
 *
 * `fetchRef()` 取當下的網路函式。`addAll` **必須真的走網路**,
 * 否則 install 永遠成功 —— 於是「離線時 shell 沒裝起來」這種情境
 * 根本到不了,而測試會看起來綠燈。(2026-10-19 實測:第一版的 addAll
 * 無條件塞 5 個合成的 200 回應,我新加的離線情境因此什麼都沒測到。)
 */
function makeCaches(putErrors, fetchRef = () => async () => new Response("shell")) {
  const stores = new Map();
  const store = (name) => {
    if (!stores.has(name)) stores.set(name, new Map());
    return stores.get(name);
  };
  const entry = (c, req) => c.get(typeof req === "string" ? new URL(req, ORIGIN + "/").href : req.url);
  return {
    stores,
    async open(name) {
      const c = store(name);
      return {
        async put(req, res) {
          // 規格行為:Cache.put() 遇到 206 Partial Content 會直接拋 TypeError。
          // 少了這一條,`res.status === 200` 被改成 `res.ok` 也測不出差別。
          if (res.status === 206) {
            putErrors.push(`206: ${req.url}`);
            throw new TypeError("Cache.put() 不接受 206 Partial Content");
          }
          c.set(req.url, res);
        },
        async match(req) { return entry(c, req) || undefined; },
        async addAll(list) {
          // 規格行為:addAll 是【原子】的 —— 任何一個請求失敗或回非 2xx,
          // 整批就 reject,一個都不會進快取。sw.js 的 install 把它包在
          // `.catch(() => {})` 裡,所以失敗是完全無聲的。
          const fetched = await Promise.all(list.map(async (p) => {
            const res = await fetchRef()(p);
            if (!res.ok) throw new TypeError(`addAll 失敗:${p} 回 ${res.status}`);
            return [new URL(p, ORIGIN + "/").href, res];
          }));
          fetched.forEach(([u, res]) => c.set(u, res));
        },
        async keys() { return [...c.keys()].map((u) => ({ url: u })); },
      };
    },
    async keys() { return [...stores.keys()]; },
    async delete(name) { return stores.delete(name); },
    async match(req) {
      for (const c of stores.values()) {
        const hit = entry(c, req);
        if (hit) return hit;
      }
      return undefined;
    },
  };
}

/** 依情境回傳指定狀態碼的假網路。 */
function makeFetch(status, body = "x") {
  return async () => {
    if (status === 0) throw new TypeError("offline");
    return new Response(status === 204 ? null : body, { status });
  };
}

function load(cachesObj, fetchFn) {
  const listeners = {};
  const self = {
    addEventListener: (t, fn) => { (listeners[t] ||= []).push(fn); },
    skipWaiting: () => {},
    clients: { claim: async () => {} },
    location: { origin: ORIGIN },
  };
  // sw.js 裡的 `caches` / `fetch` / `self` 都是裸識別字,當成參數傳進去即可綁定
  new Function("self", "caches", "fetch", source)(self, cachesObj, fetchFn);
  return listeners;
}

/** 派送一次 fetch 事件,回傳 {responded, status} 與事後的快取內容。 */
async function dispatchFetch({ url, mode = "no-cors", method = "GET", status,
                               installStatus = 200 }) {
  const putErrors = [];

  // install 與之後那次 fetch 要能用【不同】的網路狀態,否則測不了
  // 「shell 已經裝好了、然後才離線」—— 而那正是離線使用 PWA 的唯一情境。
  // 兩者共用一個 makeFetch(0) 的話,install 的 addAll 會先 reject,快取是空的,
  // 於是「離線但快取命中」根本到不了。
  let phase = installStatus;
  const net = async (...a) => makeFetch(phase)(...a);
  const cachesObj = makeCaches(putErrors, () => net);
  const listeners = load(cachesObj, net);

  // 先讓 install 把 shell 放進去,重現真實的初始狀態
  for (const fn of listeners.install || []) {
    await new Promise((done) => fn({ waitUntil: (p) => Promise.resolve(p).then(done, done) }));
  }
  phase = status;
  const shellSize = (await (await cachesObj.open([...cachesObj.stores.keys()][0])).keys()).length;

  let responded = null;
  const req = { url, method, mode };
  for (const fn of listeners.fetch || []) {
    fn({ request: req, respondWith: (p) => { responded = p; } });
  }

  let outStatus = null, threw = null, resolvedUndefined = null, respType = null;
  if (responded) {
    try {
      const r = await responded;
      // ⚠ 必須和「沒攔截」分得開。原本只記 status,而 `undefined?.status ?? null`
      // 也是 null —— 於是「respondWith 收到 undefined」(瀏覽器會直接報網路錯誤)
      // 和「SW 放行給網路」在測試看起來一模一樣。
      resolvedUndefined = r === undefined || r === null;
      outStatus = r?.status ?? null;
      respType = r?.type ?? null;        // Response.error() 的 type 是 "error"
    } catch (e) { threw = String(e); }
  }
  await new Promise((r) => setTimeout(r, 20));   // c.put 是 fire-and-forget,等它落地

  const name = [...cachesObj.stores.keys()][0];
  const after = [...cachesObj.stores.get(name).keys()];
  return {
    intercepted: responded !== null,
    status: outStatus,
    respType,
    resolvedUndefined,
    threw,
    cachedNow: after.length - shellSize,
    cachedUrl: after.includes(url),
    putErrors,
    unhandled: unhandled.splice(0),
  };
}

const cacheName = (source.match(/const CACHE\s*=\s*"([^"]+)"/) || [])[1] ?? null;

console.log(JSON.stringify({
  cacheName,
  scenarios: {
    // ③ 靜態檔分支:快取優先 —— 存錯了就會一直錯到版本號 +1。
    //    三個情境刻意用【同一個不在 SHELL 裡的 URL】,唯一的差別就是狀態碼,
    //    否則「已被 install 預先放進去」會讓 200 那條看起來永遠是對的。
    static_200: await dispatchFetch({ url: ORIGIN + "/later-added.png", status: 200 }),
    static_404: await dispatchFetch({ url: ORIGIN + "/later-added.png", status: 404 }),
    static_500: await dispatchFetch({ url: ORIGIN + "/later-added.png", status: 500 }),
    // 206 是 res.ok 為 true、卻不能存進 Cache 的那個洞
    static_206: await dispatchFetch({ url: ORIGIN + "/later-added.png", status: 206 }),
    // ② 導覽分支:404 若被存起來,離線首頁就會變成 404 頁
    navigate_200: await dispatchFetch({ url: ORIGIN + "/", mode: "navigate", status: 200 }),
    navigate_404: await dispatchFetch({ url: ORIGIN + "/gone/", mode: "navigate", status: 404 }),
    // ① 跨網域資料 API:完全放行,SW 不得介入
    cross_origin: await dispatchFetch({ url: "https://query1.finance.yahoo.com/v8/x", status: 200 }),
    // 非 GET 不處理
    post: await dispatchFetch({ url: ORIGIN + "/later-added.png", method: "POST", status: 200 }),

    // ── 離線:這整條路徑在 2026-10-19 之前【從來沒有任何情境走過】 ──────────
    //    makeFetch(0) 這個機制一直都在,但沒有一個情境用它。
    //    第二意見審閱時被點出來(他當時以為是 Promise 真值的 bug,
    //    那是我在交接文件裡把程式碼抄錯了;但「離線沒測過」這點完全正確)。
    //
    // ③ 離線 + 快取命中 → 必須回快取裡那一份
    static_offline_hit: await dispatchFetch({ url: ORIGIN + "/icon-192.png", status: 0 }),
    // ③ 離線 + 快取沒有 → respondWith 不得收到 undefined,也不得 reject
    static_offline_miss: await dispatchFetch({ url: ORIGIN + "/later-added.png", status: 0 }),
    // ② 離線導覽 + 該網址沒快取 → 要退回 SHELL 裡的 ./index.html
    navigate_offline: await dispatchFetch({ url: ORIGIN + "/deep/link", mode: "navigate", status: 0 }),
    // ② 離線導覽 + 連 install 都失敗(快取全空)→ 仍然不得收到 undefined
    navigate_offline_no_shell: await dispatchFetch({
      url: ORIGIN + "/", mode: "navigate", status: 0, installStatus: 0 }),
  },
}, null, 1));
