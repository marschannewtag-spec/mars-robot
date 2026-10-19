/**
 * 資料鏡像橋接器 —— 用【線上真的在跑的那支 `spFromYahoo()`】驗證
 * publish_pwa.py 的核心恆等式:
 *
 *   spFromYahoo(全段日K) === spFromYahoo(只有每月最後一個交易日)
 *
 * 這條恆等式是「S&P 只發 19 KB 而不是 421 KB」的全部依據。
 * 它在 Python 側也有一份測試,但 Python 的月分組是我自己寫的 ——
 * 真正會跑在使用者瀏覽器裡的是這一支,所以必須拿它來問。
 *
 * 用法:
 *   node tests/mirror_bridge.mjs <index.html> <full.json> <monthend.json>
 * 兩個 json 都是 Yahoo chart 格式。輸出 stdout 的 JSON:
 *   { full: [{ym,price}...], monthEnd: [...], identical: bool }
 */
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const [htmlPath, fullPath, monthEndPath] = process.argv.slice(2);
if (!htmlPath || !fullPath || !monthEndPath) {
  console.error("用法: node mirror_bridge.mjs <index.html> <full.json> <monthend.json>");
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
if (typeof w.spFromYahoo !== "function") {
  console.error("index.html 沒有 spFromYahoo —— 鏡像格式的前提不成立了");
  process.exit(3);
}

const full = w.spFromYahoo(JSON.parse(readFileSync(fullPath, "utf8")));
const monthEnd = w.spFromYahoo(JSON.parse(readFileSync(monthEndPath, "utf8")));
const identical = JSON.stringify(full) === JSON.stringify(monthEnd);

process.stdout.write(JSON.stringify({ full, monthEnd, identical }));
