/**
 * 來源退回鏈橋接器 —— 在 jsdom 裡跑【線上真的在跑的那支 `fetchSP()`】,
 * 用假的 fetch 控制每一層的死活,看它最後選了哪個來源。
 *
 * 為什麼需要它
 * ------------
 * `fetchSP()` 是整個 PWA 裡唯一決定「使用者看到的數字來自哪裡」的地方,
 * 而它從來沒有被任何測試走過 —— 因為它碰網路。結果這條鏈已經壞過兩次:
 *
 *   2026-08..10  allorigins 死了兩個月,PWA 不知道,每次冷啟動白等 14 秒
 *   2026-10-19   我自己加的 `if(!worker && !key)` 讓設了 key 的使用者
 *                在 key 失效時【跳過鏡像】直接掉到 Shiller 月均價
 *
 * 兩次都是同一個形狀:**一條寫了但走不到的退回路徑**。
 * 那種 bug 不會讓畫面壞掉,只會讓數字默默換一個定義 —— 所以必須有測試。
 *
 * 用法:
 *   node tests/fetchsp_bridge.mjs <index.html> <scenario.json>
 *
 * scenario.json:
 *   {
 *     "worker": "https://w.example/" | null,
 *     "key": "abc" | null,
 *     "alive": { "worker": bool, "key": bool, "mirror": bool,
 *                "proxy": bool, "shiller": bool }
 *   }
 * 輸出 stdout 的 JSON:{ src, months, altDef, calls: [...], error }
 * `calls` 是實際被 fetch 的網址順序 —— 用來斷言「某一層真的被試過」,
 * 而不只是「最後的結果看起來對」。
 */
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const [htmlPath, scenarioPath] = process.argv.slice(2);
if (!htmlPath || !scenarioPath) {
  console.error("用法: node fetchsp_bridge.mjs <index.html> <scenario.json>");
  process.exit(2);
}
const sc = JSON.parse(readFileSync(scenarioPath, "utf8"));
const alive = sc.alive || {};

// ---- 假資料 ---------------------------------------------------------------
// 每個來源都給「足夠長」的歷史(>= 13 個月),這樣失敗一定是因為退回鏈的
// 邏輯,不是因為資料太短 —— 兩種失敗必須分得開。
const MONTHS = 40;
function isoMonths(n) {
  const out = [];
  for (let i = n - 1; i >= 0; i--) {
    const d = new Date(Date.UTC(2026, 9 - i, 15));   // 2026-10 往前推
    out.push(d);
  }
  return out;
}
function yahooShape(mark) {
  const ds = isoMonths(MONTHS);
  return {
    chart: { error: null, result: [{
      timestamp: ds.map((d) => Math.floor(d.getTime() / 1000)),
      indicators: { quote: [{ close: ds.map((_, i) => 1000 + i + mark) }] },
    }] },
  };
}
function twelveShape() {
  const ds = isoMonths(MONTHS).slice().reverse();     // TwelveData 由新到舊
  return { values: ds.map((d, i) => ({
    datetime: d.toISOString().slice(0, 10), close: String(2000 + i),
  })) };
}
function shillerCsv() {
  const rows = ["Date,SP500"];
  for (const d of isoMonths(MONTHS)) {
    rows.push(`${d.toISOString().slice(0, 10)},${3000 + rows.length}`);
  }
  return rows.join("\n");
}

const calls = [];
function ok(body, json = true) {
  return Promise.resolve({
    ok: true, status: 200,
    json: () => Promise.resolve(json ? body : JSON.parse(body)),
    text: () => Promise.resolve(json ? JSON.stringify(body) : body),
  });
}
function dead(status = 503) {
  return Promise.resolve({
    ok: false, status,
    json: () => Promise.reject(new TypeError("dead")),
    text: () => Promise.resolve("dead"),
  });
}

function fakeFetch(url) {
  const u = String(url);
  calls.push(u);
  if (sc.worker && u.startsWith(sc.worker)) {
    return alive.worker ? ok(yahooShape(1)) : dead();
  }
  if (u.includes("twelvedata.com")) {
    // TwelveData 的失敗是 HTTP 200 + {status:"error"} —— 刻意照它真的樣子模擬,
    // 因為 fetchSP 對這一層的判斷不是看 r.ok
    return alive.key ? ok(twelveShape())
                     : ok({ status: "error", message: "quota exceeded" });
  }
  if (u.includes("/pwa/sp_monthly.json")) {
    return alive.mirror ? ok(yahooShape(2)) : dead(404);
  }
  if (u.includes("/pwa/vix_daily.csv")) {
    return ok("DATE,HIGH,CLOSE\n2026-10-01,20,19\n", false);
  }
  if (u.includes("corsproxy.io")) {
    return alive.proxy ? ok(yahooShape(3)) : dead(401);
  }
  if (u.includes("datasets/s-and-p-500")) {
    return alive.shiller ? ok(shillerCsv(), false) : dead();
  }
  return dead(404);
}

const dom = new JSDOM(readFileSync(htmlPath, "utf8"), {
  runScripts: "dangerously",
  pretendToBeVisual: true,
  url: "http://localhost/",
  beforeParse(window) {
    window.fetch = (u, o) => fakeFetch(u, o);
    window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
    Object.defineProperty(window.navigator, "serviceWorker", {
      value: { register: () => Promise.reject(new Error("bridge: SW 已停用")) },
      configurable: true,
    });
  },
});

const w = dom.window;
if (typeof w.fetchSP !== "function") {
  console.error("index.html 沒有 fetchSP —— 退回鏈的前提不成立了");
  process.exit(3);
}

// 設定使用者的 Worker / key。index.html 在載入時就會自己跑一次 load(),
// 那一次讀到的是空設定 —— 無所謂,我們等一下自己呼叫 fetchSP。
try {
  if (sc.worker) w.localStorage.setItem("cwgauge_worker", sc.worker);
  else w.localStorage.removeItem("cwgauge_worker");
  if (sc.key) w.localStorage.setItem("cwgauge_apikey", sc.key);
  else w.localStorage.removeItem("cwgauge_apikey");
} catch (e) { /* jsdom 一定有 localStorage;真的沒有就讓下面的斷言去失敗 */ }

calls.length = 0;                      // 不要把自動 load() 的呼叫算進來
let out;
try {
  const r = await w.fetchSP(false);
  out = { src: r.src, months: r.spArr.length, altDef: !!r.altDef, error: null };
} catch (err) {
  out = { src: null, months: 0, altDef: false, error: String(err && err.message || err) };
}
out.calls = calls;
process.stdout.write(JSON.stringify(out));
