/**
 * 體制面板橋接器 —— 餵一份 signals.json 給【線上真的在跑的那支
 * `renderRegime()`】,把畫面吐回來。
 *
 * 為什麼不用 grep:「已觸發的那條有沒有把門檻露在外面」是一個**畫面**問題。
 * 原始碼比對會在任何一次模板改寫後失效卻仍然通過 —— 和 sw.js 那次同一個理由。
 *
 * 用法:
 *   node tests/regime_bridge.mjs <index.html> <signals.json>
 * 輸出:stdout 的 JSON { panel_on, flaglist_html, flaglist_text, note, mainstate }
 */
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const [htmlPath, signalsPath] = process.argv.slice(2);
if (!htmlPath || !signalsPath) {
  console.error("用法: node regime_bridge.mjs <index.html> <signals.json>");
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
if (typeof w.renderRegime !== "function") {
  console.error("index.html 沒有 renderRegime —— 這個橋接器的前提不成立了");
  process.exit(3);
}

w.renderRegime(JSON.parse(readFileSync(signalsPath, "utf8")));

const el = (id) => w.document.getElementById(id);
process.stdout.write(JSON.stringify({
  panel_on: el("regimepanel").classList.contains("on"),
  flaglist_html: el("flaglist").innerHTML,
  flaglist_text: el("flaglist").textContent,
  note: el("regimenote").textContent,
  // 對照組:renderRegime 不得動到這一行(它由核心寫)
  mainstate: el("mainstate").textContent,
}));
