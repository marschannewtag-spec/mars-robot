/**
 * 心跳指示器橋接器 —— 在 jsdom 裡跑【線上真的在跑的那份 `checkOpsHeartbeat()`】,
 * 問它一個行為問題:這個情境下,`#opsnote` 到底顯示了什麼?
 *
 * 為什麼需要它
 * ------------
 * 原本這幾條是「原始碼性質測試」:用正則確認 `.catch(()=>{})` 是空的。
 * 那是在對準**字串形狀**,不是對準那個宣稱(「離線時不得誤報監控死了」)。
 * 2026-10-20 要讓失敗路徑退回舊判準,catch 就不再是空的 —— 宣稱沒變,
 * 形狀變了,而只認形狀的測試會擋下一個正確的修改。
 *
 * 所以改成問行為:餵它六種情境,看畫面。
 *
 * 用法:
 *   node tests/heartbeat_bridge.mjs <index.html> <scenario.json>
 *
 * scenario.json:
 *   { "heartbeat": <回應>, "manifest": <回應> }
 * <回應> 可以是:
 *   null                              -> fetch 直接 reject(離線)
 *   { "status": 404 }                 -> 非 200
 *   { "status": 200, "text": "..." }  -> 文字回應
 *   { "status": 200, "json": {...} }  -> JSON 回應
 *
 * 輸出 stdout 的 JSON:{ on: bool, text: string, html: string }
 */
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const [htmlPath, scenarioPath] = process.argv.slice(2);
if (!htmlPath || !scenarioPath) {
  console.error("用法: node heartbeat_bridge.mjs <index.html> <scenario.json>");
  process.exit(2);
}
const scenario = JSON.parse(readFileSync(scenarioPath, "utf8"));

function respond(spec) {
  if (spec === null || spec === undefined) {
    return Promise.reject(new TypeError("bridge: 網路失敗"));
  }
  const status = spec.status ?? 200;
  const body = spec.json !== undefined ? JSON.stringify(spec.json) : (spec.text ?? "");
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(body),
    json: () => Promise.resolve(JSON.parse(body)),
  });
}

const dom = new JSDOM(readFileSync(htmlPath, "utf8"), {
  runScripts: "dangerously",
  pretendToBeVisual: true,
  url: "http://localhost/",
  beforeParse(window) {
    window.fetch = (url) => {
      const u = String(url);
      // 只攔這兩個端點;其餘(價格、代理、體制訊號)一律失敗,
      // 它們本來就設計成失敗即靜默,不會干擾 #opsnote。
      if (u.includes("heartbeat.txt")) return respond(scenario.heartbeat);
      if (u.includes("manifest.json")) return respond(scenario.manifest);
      return Promise.reject(new TypeError("bridge: 未攔截的端點"));
    };
    window.AbortController = class {
      constructor() { this.signal = {}; }
      abort() {}
    };
    window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
    Object.defineProperty(window.navigator, "serviceWorker", {
      value: { register: () => Promise.reject(new Error("bridge: SW 已停用")) },
      configurable: true,
    });
  },
});

const w = dom.window;
if (typeof w.checkOpsHeartbeat !== "function") {
  console.error("index.html 沒有 checkOpsHeartbeat —— 這個橋接器的前提不成立了");
  process.exit(3);
}

// 載入時頁面自己已經呼叫過一次。兩層 fetch 串接 + 各自的 .then,
// 給足夠的 microtask/macrotask 讓它們走完。
await new Promise((r) => setTimeout(r, 50));

const el = w.document.getElementById("opsnote");
process.stdout.write(JSON.stringify({
  on: !!(el && el.classList.contains("on")),
  text: el ? el.textContent : "",
  html: el ? el.innerHTML : "",
}));
process.exit(0);
