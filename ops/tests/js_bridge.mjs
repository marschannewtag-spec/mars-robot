/**
 * JS 橋接器 —— 在 Node 裡載入 PWA 的 index.html,取出它的純計算函式,
 * 餵進與 engine.py 完全相同的原始資料,把結果吐成 JSON 給 Python 比對。
 *
 * 為什麼要這樣做而不是把 JS 抄一份過來:抄一份就等於多一個會漂移的實作。
 * 這裡讀的是【線上真的在跑的那個檔案】,所以測試失敗就代表使用者看到的東西
 * 真的和真理來源不一致。
 *
 * 用法:
 *   node tests/js_bridge.mjs <index.html> <vix.csv> <sp_daily.csv> <shiller.csv>
 * 輸出:stdout 的 JSON
 */
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const [htmlPath, vixPath, spDailyPath, shillerPath] = process.argv.slice(2);
if (!htmlPath) {
  console.error("用法: node js_bridge.mjs <index.html> <vix.csv> <sp_daily.csv> <shiller.csv>");
  process.exit(2);
}

const html = readFileSync(htmlPath, "utf8");

// index.html 的腳本在載入時會自動跑 load() 去抓網路、註冊 SW、開 ResizeObserver。
// 這些在 Node 裡都沒有意義,全部擋掉 —— 我們只要它的純函式。
const dom = new JSDOM(html, {
  runScripts: "dangerously",
  pretendToBeVisual: true,
  url: "http://localhost/",
  beforeParse(window) {
    window.fetch = () => Promise.reject(new TypeError("bridge: 網路已停用"));
    window.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
    Object.defineProperty(window.navigator, "serviceWorker", {
      value: { register: () => Promise.reject(new Error("bridge: SW 已停用")) },
      configurable: true,
    });
  },
});

const w = dom.window;

// 這些必須是 index.html 裡的 function 宣告才會掛到 window 上。
// 若某天被改成 const 箭頭函式,這裡會立刻抓到 —— 那本身就是有價值的警報。
const REQUIRED = [
  "parseCSV", "momCash", "vixCash", "buildVixMonthly",
  "vixRiskSeries", "spMonthly", "spFromYahoo", "compute",
];
const missing = REQUIRED.filter((n) => typeof w[n] !== "function");
if (missing.length) {
  console.error(`index.html 缺少預期的函式(或已不是 function 宣告):${missing.join(", ")}`);
  process.exit(3);
}

const vixRows = w.parseCSV(readFileSync(vixPath, "utf8"));
const vm = w.buildVixMonthly(vixRows);
const risk = w.vixRiskSeries(vm);

// S&P 月收盤:把日K CSV 轉成 spFromYahoo 吃的 Yahoo chart JSON 形狀,
// 走的就是線上真正在用的那條程式路徑。
const spCsv = w.parseCSV(readFileSync(spDailyPath, "utf8"));
const yahooLike = {
  chart: {
    result: [{
      timestamp: spCsv.map((r) => Math.floor(Date.parse(r.date + "T00:00:00Z") / 1000)),
      indicators: { quote: [{ close: spCsv.map((r) => parseFloat(r.close)) }] },
    }],
  },
};
const spArr = w.spFromYahoo(yahooLike);

// 依 compute() 內部的做法組出逐月訊號,但不截斷成最後 96 個月
const spSig = spArr.map((x, i) => {
  if (i < 11) return null;
  const win = spArr.slice(i - 11, i + 1).map((p) => p.price);
  const sma = win.reduce((a, b) => a + b, 0) / win.length;
  return { ym: x.ym, price: x.price, sma, mom: x.price / sma - 1 };
});

const riskByYm = {};
vm.forEach((x, i) => { riskByYm[x.ym] = risk[i]; });

const series = [];
for (const s of spSig) {
  if (!s) continue;
  const rk = riskByYm[s.ym];
  if (rk == null) continue;
  const mc = w.momCash(s.mom);
  const vc = w.vixCash(rk);
  series.push({
    ym: s.ym, price: s.price, mom: s.mom, risk: rk,
    mom_cash: mc, vix_cash: vc, cash: Math.max(mc, vc),
  });
}

// 另外跑一次完整的 compute(),驗證組裝層(不只是各別公式)也一致
const full = w.compute(spArr, vixRows);

// Shiller 路徑(退回來源)也一併輸出,確認月均價定義兩邊一致
const shillerArr = w.spMonthly(w.parseCSV(readFileSync(shillerPath, "utf8")));

console.log(JSON.stringify({
  series,
  vix_monthly: vm.map((x, i) => ({ ym: x.ym, close: x.close, max: x.max, n: x.n, risk: risk[i] })),
  compute_last: {
    ym: full.sLast.ym, mom: full.sLast.mom, risk: full.rLast,
    mom_cash: full.momC, vix_cash: full.vixC, cash: full.uni,
    dd_peak: full.ddPeak, peak_ym: full.peakYM, span_from: full.spanFrom,
  },
  compute_hist: full.hist,
  shiller_monthly: shillerArr,
}));
