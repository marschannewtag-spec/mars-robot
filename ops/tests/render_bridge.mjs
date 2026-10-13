/**
 * 渲染層橋接器 —— 在 jsdom 裡載入【線上真的在跑的那個 index.html】,
 * 用捏好的資料呼叫它自己的 `render()`,再把實際的 DOM 狀態吐成 JSON。
 *
 * 為什麼不用 grep 原始碼:「有沒有寫 vLast.ym < sLast.ym 這個判斷」這種檢查,
 * 任何一次分支改寫都會讓它失效卻仍然通過(和 sw.js 那次是同一個理由)。
 * 這裡問的是唯一重要的問題:**餵它一份 VIX 落後一個月的資料,畫面上有沒有說**。
 *
 * 用法:
 *   node tests/render_bridge.mjs <index.html> <scenario>
 * 輸出:stdout 的 JSON
 */
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const [htmlPath, scenario, fixedNow] = process.argv.slice(2);
if (!htmlPath || !scenario) {
  console.error("用法: node render_bridge.mjs <index.html> <scenario> [ISO 固定時間]");
  process.exit(2);
}

// 「整份讀數落後日曆」的判斷依賴 `new Date()`,所以測它必須能控制時鐘。
// 預設 2026-10-14(實測到那個故障的那天),各情境可以覆寫。
const NOW = Date.parse(fixedNow || "2026-10-14T12:00:00Z");
if (Number.isNaN(NOW)) {
  console.error(`無法解析固定時間:${fixedNow}`);
  process.exit(5);
}

const dom = new JSDOM(readFileSync(htmlPath, "utf8"), {
  runScripts: "dangerously",
  pretendToBeVisual: true,
  url: "http://localhost/",
  beforeParse(window) {
    // index.html 載入時會自動抓網路、註冊 SW、開 ResizeObserver。
    // 這裡只要它的渲染函式,其餘全部擋掉。
    window.fetch = () => Promise.reject(new TypeError("bridge: 網路已停用"));
    window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
    Object.defineProperty(window.navigator, "serviceWorker", {
      value: { register: () => Promise.reject(new Error("bridge: SW 已停用")) },
      configurable: true,
    });
    // 固定時鐘:無參數的 new Date() 回 NOW,其餘用法行為不變
    const RealDate = window.Date;
    class FixedDate extends RealDate {
      constructor(...args) { super(...(args.length ? args : [NOW])); }
      static now() { return NOW; }
    }
    window.Date = FixedDate;
  },
});

const w = dom.window;

const REQUIRED = ["render", "monthsBetween"];
const missing = REQUIRED.filter((n) => typeof w[n] !== "function");
if (missing.length) {
  console.error(`index.html 缺少預期的函式(或已不是 function 宣告):${missing.join(", ")}`);
  process.exit(3);
}

/** 一份「什麼都正常」的 render() 輸入。各情境只覆寫需要的欄位。 */
function baseline() {
  return {
    // 預設兩條腿都在 2026-10,也就是固定時鐘的當月 —— 完全正常,不該有任何警示
    sLast: { ym: "2026-10", price: 6800, sma: 6300, mom: 0.0794 },
    vLast: { ym: "2026-10", close: 14.2, n: 21 },
    rLast: 0.04,
    momC: 0.0,
    vixC: 0.0,
    uni: 0.0,
    hist: [{ ym: "2026-09", price: 6700, cash: 0 }, { ym: "2026-10", price: 6800, cash: 0 }],
    ddPeak: 0.0,
    peakPx: 6800,
    peakYM: "2026-10",
    spanFrom: "2016-10",
    src: "每日 · Yahoo^GSPC(免金鑰)",
    altDef: false,
  };
}

// 每個情境只動最少的欄位,其餘保持不變 —— 這樣差異一定來自落後判斷本身。
const SCENARIOS = {
  aligned:        (d) => d,                                    // 兩腿皆當月:不該報警
  vix_one_behind: (d) => { d.vLast.ym = "2026-09"; return d; },
  vix_two_behind: (d) => { d.vLast.ym = "2026-08"; return d; },
  // VIX 腿【超前】是刻意的設計(允許用進行中的當月),不是故障 → 不該報警
  vix_ahead:      (d) => { d.sLast.ym = "2026-09"; return d; },
  // 月份字串壞掉時寧可不報警,也不要算出一個假的月數
  malformed_ym:   (d) => { d.vLast.ym = "壞掉的值"; return d; },
  // 兩條腿一起落後日曆 —— 2026-10-14 線上實測就是這個(退回 Shiller 後
  // 趨勢腿也掉到 2026-09)。月份看起來一致,反而更像正常,所以更危險。
  both_stale:     (d) => { d.sLast.ym = "2026-09"; d.vLast.ym = "2026-09"; return d; },
};

const build = SCENARIOS[scenario];
if (!build) {
  console.error(`未知情境 ${scenario};可用:${Object.keys(SCENARIOS).join(", ")}`);
  process.exit(4);
}

const data = build(baseline());
w.render(data, Date.parse("2026-10-14T00:00:00Z"), false);

const el = (id) => w.document.getElementById(id);
const note = el("lagnote");

console.log(JSON.stringify({
  scenario,
  now: new w.Date().toISOString(),
  lag_on: note.classList.contains("on"),
  lag_text: note.textContent,
  months_between: w.monthsBetween(data.vLast.ym, data.sLast.ym),
  // 對照組:這一塊不得改變核心讀數。落後警示是平行的第二個訊息,不是輸入。
  bigpct: el("bigpct").textContent,
  verdict: el("verdict").textContent,
  momcash: el("momcash").textContent,
  vixcash: el("vixcash").textContent,
  // 定義警示列是另一條獨立的訊息,不該被落後警示帶著亮
  defnote_on: el("defnote").classList.contains("on"),
}));
