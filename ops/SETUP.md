# 啟用清單

自動化的骨架已經全部就位並實測過。以下是**只有你能做**的幾件事 ——
我沒有你的帳號,也不會替你申請或輸入任何憑證。

依重要性排序。第 1 項不做,自動修復就不會啟動(其餘功能不受影響)。

---

## 1. 認證(目前刻意未啟用)

**現況:兩個 secret 都沒設,`autofix` job 會乾淨地跳過。**

這是有意的選擇,不是漏做。系統目前運作方式:健檢偵測到異常 → 開 Issue →
**你手動把 Issue 貼給 Claude Code 處理**。和啟用自動修復的差別只在最後一步是誰按的。

實測過的降級行為(2026-08-08 drill):整體 run `success`,autofix job 只執行
guard 那一步就跳過,不浪費 runner 時間、不亮紅燈。

想啟用的話,有兩條路 —— **建議走免費的那條**。

### 🟢 走 Pro/Max 訂閱(不另外收費,建議)

你已經在用 Claude Code,那筆訂閱費已經付了。action 官方支援直接吃訂閱額度:

```bash
claude setup-token
```

會開瀏覽器走一次 OAuth,然後印出一個長效 token。**只會顯示一次,馬上複製。**

接著到 repo → **Settings → Secrets and variables → Actions → New repository secret**,
名稱填 `CLAUDE_CODE_OAUTH_TOKEN`,值貼上那個 token。

流程會優先用它。設好之後 `ANTHROPIC_API_KEY` 就用不到了(留著當備援無妨)。

**要知道的兩件事:**

- 它消耗的是**你訂閱的額度**,和你互動用 Claude Code 是同一個池子。
  以這個專案的規模(健檢平常是綠的,一年可能只跑幾次)完全可以忽略。
- token 綁在你的個人帳號上。存成 repo secret 是加密的,但別貼到別的地方。

### 🟡 走 API 額度(按量計費)

`ANTHROPIC_API_KEY` 曾經設過,但實測是:

```
result: Credit balance is too low
num_turns: 1   total_cost_usd: 0   duration_ms: 486
```

帳戶沒有餘額,呼叫在第一步就被拒絕。**該 secret 已於 2026-08-08 刪除** ——
留著只會讓 guard 誤判「已啟用」,每次白跑一分鐘裝相依然後失敗。

要走這條:到 https://console.anthropic.com/settings/billing 儲值,再把
`ANTHROPIC_API_KEY` 加回去。一次診斷大約幾萬到十幾萬 token。

但既然訂閱那條是免費的,除非你有理由要把用量分開計帳,否則沒必要。

### ⚠ 若哪天啟用了,記得驗證

設好任一 secret 後,到 Actions → 「L1 每日資料健檢」→ Run workflow → 勾 **drill**。

**不要假設設好就會動。** 這條鏈實測撞過三個坑,前兩個已修在 workflow 裡
(`id-token: write`、改傳內建 `github_token` 以免要裝官方 GitHub App),
第三個就是額度。認證方式換了之後,還可能有沒踩過的坑。

### 儲值後怎麼驗證

到 Actions → 「L1 每日資料健檢」→ **Run workflow**,把 **drill** 勾起來。

那會強制跑一次自動修復(即使健檢是綠的),用來驗證整條鏈接得起來,
不需要去改壞 config。健檢正常時 Claude 應該讀完報告後回報「沒有需要修的」。

### 已經替你踩掉的坑(不用再處理)

實測過程中撞到兩個,都已修正並驗證:

| 問題 | 現象 | 已做的處理 |
|---|---|---|
| 缺 `id-token: write` | `Could not fetch an OIDC token` | 已加進 autofix job 的 permissions |
| 官方 Claude GitHub App 未安裝 | `App token exchange failed: 401 — Claude Code is not installed on this repository` | 改為明確傳入內建的 `github_token`,**你不需要安裝那個 App** |

---

## 2. `FRED_API_KEY` — 補齊年度健檢的信用利差

**不做會怎樣:** 年度報告仍然產得出來,但沒有 HY-OAS 的長期脈絡。
`fredgraph.csv` 免金鑰端點只回近三年且忽略 `cosd` 參數 —— 這是實測過的,
不是設定問題。

1. 到 https://fred.stlouisfed.org/docs/api/api_key.html 免費申請(填個表就好)
2. 存成 repo secret,名稱 `FRED_API_KEY`

---

## 3. Pine 執行層驗證

**不做會怎樣:** `CashWeightGauge.pine` 的狀態停在「邏輯已驗證、執行未驗證」。
邏輯層 CI 每次都在驗,但**編譯錯誤 CI 抓不到** —— 一份邏輯完全正確卻編譯不過的
`.pine`,CI 會給你綠燈。

完整步驟見 [`pine/README.md`](pine/README.md)。花不到十分鐘。

---

## 4. 排程被停用的風險 —— 只需要留意一封信

GitHub 會在 repo 靜置一段時間後自動停用排程 workflow,而「一年碰一次」
正是會觸發它的使用模式。

**目前的緩解:** 每日健檢會把資料推到 `ops-data` 分支製造 repo 活動。
**但這個機制沒有被驗證過** —— 效果要兩個月後才看得出來,而且已知的不確定點是
`GITHUB_TOKEN` 產生的 bot commit 是否被計為「repository activity」,
有多方回報**不算**。

所以真正的防線是這兩件事:

1. **GitHub 停用前會寄 email。** 那封信不要當廣告忽略 —— 它是這套系統唯一會
   主動告訴你「監控要停了」的訊號。
2. **收到就把心跳改用 PAT 推送:** 建一個 fine-grained PAT(只需 `contents: write`),
   存成 secret `HEARTBEAT_PAT`,把 `daily-health.yml` 的 checkout 步驟加上
   `token: ${{ secrets.HEARTBEAT_PAT }}`。真人身分的推送一定算活動。

或者更簡單 —— 你每年手動觸發一次 L3 年度健檢,那本身就是一次 repo 活動。

---

## 已經做好、你不用管的事

- `main` 分支保護:要求 CI 通過。**實測** bot 推 `main` 會被 `GH006` 擋下,
  而你本人(管理員)仍可直推
- 所有自動流程都不碰 `main`(心跳與報告走 `ops-data`,Claude 走 `fix/`)
- `claude-code-action` 釘死 SHA `c038e4d`(v1.0.186)
- Claude 只由 L1 自己的健檢報告觸發,外人開的 Issue 碰不到它
- 外部回應內容經淨化才進報告
- 每日健檢、CI、年度報告三支流程都在真實 runner 上跑過

詳細的安全理由見 [`CLAUDE.md`](CLAUDE.md) 的「自動化架構與安全邊界」。
