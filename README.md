# 現金水位儀 · Cash-Weight Gauge

用「趨勢動能 × 真實 VIX」的**聯集**策略,計算 S&P 500 建議的現金比重(0–100%)。
純靜態 PWA — 沒有建置流程、沒有相依套件、沒有後端,丟上 GitHub Pages 就能跑。

**線上版:** https://marschannewtag-spec.github.io/mars-robot/

---

## 檔案結構

| 檔案 | 職責 |
|---|---|
| `index.html` | **整個 App** — CSS、HTML、JS 全部內嵌在這一個檔 |
| `sw.js` | Service Worker:HTML 網路優先、靜態檔快取優先、跨網域資料不介入 |
| `manifest.json` | PWA 安裝設定(直式、深色 `#0E1726`) |
| `icon-192.png` / `icon-512.png` | App 圖示 |
| `pwa_preview.png` | 截圖,程式未引用 |

---

## ⚠ 維護第一守則:改完 `index.html` 一定要 bump SW 版本

`sw.js` 開頭:

```js
const CACHE = "cwgauge-v5";   // ← 改一次 index.html 就 +1
```

不 bump 的話,已安裝的使用者可能繼續吃到舊殼。`activate` 事件會清掉所有非當前版本的快取。

---

## 策略邏輯

兩條腿各自算出建議現金,**取較高者**(聯集)。

**趨勢動能腿** — S&P 月收相對 12 月均線的乖離率:

| 乖離 | ≥ 0 | < 0 | < −3% | < −8% | < −13% |
|---|---|---|---|---|---|
| 現金 | 0% | 30% | 60% | 90% | 100% |

**VIX 領先腿** — 風險分數 `0.55×水位 + 0.30×36月z-score + 0.15×月內飆升`
(水位 = `(VIX−16)/9`,飆升 = `(當月最高 − 近6月均)/12`,三項皆先取 `max(·,0)`):

| 分數 | ≤0.5 | >0.5 | >1.0 | >1.8 | >2.6 | >3.4 |
|---|---|---|---|---|---|---|
| 現金 | 0% | 24% | 42% | 66% | 90% | 100% |

**兩腿的階梯不同,別互相套用。**

參數 `MA12 / lvl1.2` 由 1990–2010 樣本內選出,2011–2026 樣本外驗證 Calmar 1.25 > 買進持有 0.75。

---

## 資料來源(逐層自動退回)

**VIX**(固定):GitHub `datasets/finance-vix` 的 `vix-daily.csv`。

**S&P 趨勢腿**,由新鮮到穩定:

1. **自有 Cloudflare Worker** — 使用者在 UI 填網址,存 localStorage。最優先、最穩定。回傳 Yahoo chart JSON 格式。
2. **TwelveData** — 使用者填 API key。盤中即時 SPY。
3. **Yahoo `^GSPC` 經 CORS 代理**(預設、免金鑰)— 依序試 `query1`/`query2` 兩個主機 × `corsproxy.io`/`allorigins` 兩個代理。
4. **Shiller 月頻**(保底)— GitHub `datasets/s-and-p-500`,永遠可用,但延遲約一個月。

**逾時保護:** 每個請求單次上限 7–12 秒,第 3 層整段有 12 秒總預算。任何來源掛掉都不會拖住頁面。

### 已知不可用的來源(別再加回來)

- **Stooq** — 不送 CORS 標頭,瀏覽器直連必失敗;經代理則回傳擋爬蟲的 HTML(HTTP 200 但不是 CSV)。已於 2026-08 移除。
- **FRED `fredgraph.csv`** — 不送 CORS 標頭,瀏覽器無法直接讀。

---

## 本機開發

沒有建置流程,但**必須用 http:// 開**(Service Worker 和跨網域 fetch 在 `file://` 下不會動):

```bash
python -m http.server 8765
```

然後開 http://localhost:8765 。

改完 JS 後如果行為沒變,先確認 SW 沒有餵你舊檔 — DevTools → Application → Service Workers → Unregister,或直接 bump `CACHE` 版本號。

---

## 已知限制

- **月頻訊號,非日內。** 趨勢腿用月收算 MA12。
- **「距高點」的回看區間隨資料源而變** — 走每日 Yahoo 只回看約 10 年,退回 Shiller 可回看到 1871。UI 左上角會標示實際區間,別把「距 10 年高點」讀成「距歷史高點」。
- **缺真實 HY-OAS 信用利差**,純信用事件預警未納入。
- **免費 CORS 代理偶有不穩** — 要長期穩定就自架 Cloudflare Worker(第 1 層)。
- 本機快取(`localStorage`)存的是完整 CSV 原文,約 550 KB。
- **僅供研究,非投資建議,盈虧自負。**

---

## 資料源致謝

[finance-vix](https://github.com/datasets/finance-vix) ·
[s-and-p-500](https://github.com/datasets/s-and-p-500) ·
[Twelve Data](https://twelvedata.com)
