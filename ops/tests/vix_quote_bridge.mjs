/**
 * 盤中報價橋接器 —— 用【線上真的在跑的那支 withLiveVixRow()】驗證兩件事:
 *
 *   1. 哪些報價會被接上去、哪些不會(每一條「不接」都是一種已知的壞法);
 *   2. 接上去之後,**除了最後那個月,沒有任何一個月的讀數改變**。
 *
 * 第 2 條才是重點。黃金基準、parity、健檢全部錨在 vix_daily.csv 上,
 * 一筆 15 分鐘延遲的盤中讀數如果能改到歷史,那整套驗證就失去意義。
 * 這裡不是去讀 withLiveVixRow 的原始碼確認它「看起來只會 append」,
 * 而是真的跑 buildVixMonthly 前後比對 —— 判準要對準那個宣稱。
 *
 * 用法:
 *   node tests/vix_quote_bridge.mjs <index.html> <cases.json>
 * cases.json: { csv: "<vix_daily.csv 內容>", quotes: [ {name, quote}, ... ] }
 * 輸出 stdout 的 JSON 陣列。
 */
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const [htmlPath, casesPath] = process.argv.slice(2);
if (!htmlPath || !casesPath) {
  console.error("用法: node vix_quote_bridge.mjs <index.html> <cases.json>");
  process.exit(2);
}

const dom = new JSDOM(readFileSync(htmlPath, "utf8"), {
  runScripts: "dangerously",
  pretendToBeVisual: true,
  url: "http://localhost/",
  beforeParse(window) {
    window.fetch = () => Promise.reject(new TypeError("bridge: 網路已停用"));
    window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
    Object.defineProperty(window.navigator, "serviceWorker", {
      value: { register: () => Promise.reject(new Error("bridge: SW 已停用")) },
      configurable: true,
    });
  },
});

const w = dom.window;
for (const fn of ["withLiveVixRow", "parseCSV", "buildVixMonthly", "vixRiskSeries"]) {
  if (typeof w[fn] !== "function") {
    console.error(`index.html 缺少 ${fn}(或已不是 function 宣告)`);
    process.exit(3);
  }
}

const { csv, quotes } = JSON.parse(readFileSync(casesPath, "utf8"));
const vmBefore = w.buildVixMonthly(w.parseCSV(csv));
const riskBefore = w.vixRiskSeries(vmBefore);

const out = quotes.map(({ name, quote }) => {
  const [next, live] = w.withLiveVixRow(csv, quote);
  const vmAfter = w.buildVixMonthly(w.parseCSV(next));
  const riskAfter = w.vixRiskSeries(vmAfter);
  // 「歷史沒被動到」= 應該不變的每一個月,其 ym/close/max/n 與風險分數全等。
  //
  // ⚠ 「應該不變的」有三種情況,第一版把它們混成一種(永遠排除最後一個月),
  //   於是「接出新月份時把前一個月改壞」這個突變逃得掉 —— 那個月被當成
  //   允許變動的那一個。判準要對準那個宣稱:允許變動的只有**被寫進去的那個月**。
  // ⚠ 單獨把這一行改成 `false` 不會讓任何測試變紅 —— 生產端沒有任何突變
  //   會去弄壞「前一個完整月」,所以沒有輸入能分辨。它**不是**裝飾:
  //   `實測`(配對突變,2026-11-02)把生產端改成「覆蓋最後一列而不是 append」
  //   單獨跑會紅,再加上 `grew = false` 就變綠 —— 也就是抓到那個突變的
  //   正是這一行。測試程式自己的守衛要用配對突變證明,不是單獨突變。
  const grew = vmAfter.length > vmBefore.length;
  const n = (live !== null && !grew) ? vmBefore.length - 1  // 寫進既有的月份
                                     : vmBefore.length;    // 新月份,或根本沒接
  const historyIntact =
    vmAfter.length >= vmBefore.length &&
    vmBefore.slice(0, n).every((m, i) => {
      const a = vmAfter[i];
      return a && a.ym === m.ym && a.close === m.close && a.max === m.max &&
             a.n === m.n && riskAfter[i] === riskBefore[i];
    });
  return {
    name,
    appended: live !== null,
    live,
    changed: next !== csv,
    monthsBefore: vmBefore.length,
    monthsAfter: vmAfter.length,
    lastYmAfter: vmAfter[vmAfter.length - 1].ym,
    lastNAfter: vmAfter[vmAfter.length - 1].n,
    lastMaxAfter: vmAfter[vmAfter.length - 1].max,
    lastCloseAfter: vmAfter[vmAfter.length - 1].close,
    historyIntact,
  };
});

process.stdout.write(JSON.stringify(out));
