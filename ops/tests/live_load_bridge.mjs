/**
 * 盤中報價的【整條鏈】橋接器 —— 在 jsdom 裡跑線上真的在跑的那支 `load()`,
 * 用假的 fetch 餵它三個來源,再把 DOM 與 localStorage 一起吐出來。
 *
 * 為什麼需要這一支(2026-11-03)
 * ------------------------------
 * 已經有兩支橋接器在驗這一層:`vix_quote_bridge` 跑純函式,
 * `render_bridge` 用捏好的 data 跑 `render()`。兩支都是綠的,
 * 而 `實測`(突變測試)這三個突變**全部逃掉**:
 *
 *   1. `load()` 把 appended 閘門傳成永遠 true
 *   2. `paintFromCache()` 丟掉 `o.vliveAt`
 *   3. `load()` 不再把 `vliveAt` 寫進 localStorage
 *
 * 三個都在同一個縫裡:**沒有任何測試走過 load() 與快取的來回。**
 * 純函式測得到規則,render 測得到畫面,但「規則算出來的東西有沒有真的
 * 被送到畫面、有沒有真的存下來、離線時有沒有真的回得來」沒有人在看。
 * 判準要對準那個宣稱 —— 宣稱是使用者看得到,那就得走使用者走的那條路。
 *
 * 用法:
 *   node tests/live_load_bridge.mjs <index.html> <scenario>
 * 情境:accepted | rejected | no_quote | broken_time
 * 輸出 stdout 的 JSON。
 */
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const [htmlPath, scenario] = process.argv.slice(2);
if (!htmlPath || !scenario) {
  console.error("用法: node live_load_bridge.mjs <index.html> <scenario>");
  process.exit(2);
}

// ---- 假資料 ---------------------------------------------------------------
// S&P 側照 fetchsp_bridge 的形狀,40 個月到 2026-10 為止。
const MONTHS = 40;
function isoMonths(n) {
  const out = [];
  for (let i = n - 1; i >= 0; i--) out.push(new Date(Date.UTC(2026, 9 - i, 15)));
  return out;
}
function yahooShape() {
  const ds = isoMonths(MONTHS);
  return { chart: { error: null, result: [{
    timestamp: ds.map((d) => Math.floor(d.getTime() / 1000)),
    indicators: { quote: [{ close: ds.map((_, i) => 1000 + i) }] },
  }] } };
}
// VIX 日檔:同樣 40 個月,每月三天,最後一天固定 2026-10-30。
// 固定最後一天是刻意的 —— 報價的日期與它的差值決定接不接,
// 那個差值不可以隨著「今天是幾號」漂移。
function vixCsv() {
  const rows = ["DATE,HIGH,CLOSE"];
  for (const d of isoMonths(MONTHS)) {
    const ym = d.toISOString().slice(0, 7);
    for (const day of ["05", "15", "25"]) rows.push(`${ym}-${day},18.00,17.00`);
  }
  rows.push("2026-10-30,23.93,19.42");
  return rows.join("\n") + "\n";
}

const QUOTE = {
  date: "2026-11-02", high: 20.13, low: 19.07, close: 19.55,
  quote_time_et: "2026-11-02T13:18:31",
  generated_at: "2026-11-02T18:33:33+00:00",
  source: "bridge", note: "bridge",
};
const SCENARIOS = {
  accepted:    () => QUOTE,
  // 日期不晚於日檔最後一天 → 必定不接。它的時間不得外洩到畫面或快取。
  rejected:    () => ({ ...QUOTE, date: "2026-10-29" }),
  no_quote:    () => null,
  broken_time: () => ({ ...QUOTE, quote_time_et: "2026-11-02T99:99:99" }),
};
const build = SCENARIOS[scenario];
if (!build) {
  console.error(`未知情境 ${scenario};可用:${Object.keys(SCENARIOS).join(", ")}`);
  process.exit(4);
}
const quote = build();

function ok(body, json = true) {
  return Promise.resolve({
    ok: true, status: 200,
    json: () => Promise.resolve(json ? body : JSON.parse(body)),
    text: () => Promise.resolve(json ? JSON.stringify(body) : body),
  });
}
function dead(status = 404) {
  return Promise.resolve({
    ok: false, status,
    json: () => Promise.reject(new TypeError("dead")),
    text: () => Promise.resolve("dead"),
  });
}
function fakeFetch(url) {
  const u = String(url);
  if (u.includes("/pwa/vix_today.json")) return quote === null ? dead() : ok(quote);
  if (u.includes("/pwa/vix_daily.csv")) return ok(vixCsv(), false);
  if (u.includes("/pwa/sp_monthly.json")) return ok(yahooShape());
  return dead();
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
for (const fn of ["load", "paintFromCache"]) {
  if (typeof w[fn] !== "function") {
    console.error(`index.html 缺少 ${fn}(或已不是 function 宣告)`);
    process.exit(3);
  }
}
try { w.localStorage.clear(); } catch (e) { /* jsdom 一定有 */ }

const el = (id) => {
  const e = w.document.getElementById(id);
  return e ? e.textContent.trim() : null;
};
const surfaces = () => ({ asof: el("asof"), vixdesc: el("vixdesc"), svixd: el("svixd") });

await w.load(true);
const online = surfaces();

// 快取裡存了什麼。鍵名刻意用「找出唯一那一筆有 vt 的」而不是寫死字串 ——
// 寫死的話改了鍵名這支會靜靜地回 null,而不是紅掉。
let cache = null;
for (let i = 0; i < w.localStorage.length; i++) {
  const k = w.localStorage.key(i);
  try {
    const o = JSON.parse(w.localStorage.getItem(k));
    if (o && typeof o.vt === "string") { cache = o; break; }
  } catch (e) { /* 不是我們要的那一筆 */ }
}

// 離線路徑:把剛剛存下來的那一筆餵回 paintFromCache,畫面必須一樣(多一個「離線」)。
let offline = null;
if (cache) {
  for (const id of ["asof", "vixdesc", "svixd"]) w.document.getElementById(id).textContent = "";
  const painted = w.paintFromCache(cache);
  offline = { painted, ...surfaces() };
}

process.stdout.write(JSON.stringify({
  scenario,
  online,
  cache: cache && { hasVt: typeof cache.vt === "string",
                    vlive: cache.vlive === undefined ? "<缺鍵>" : cache.vlive,
                    vliveAt: cache.vliveAt === undefined ? "<缺鍵>" : cache.vliveAt },
  offline,
}));
