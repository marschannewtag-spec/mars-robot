# 啟用清單

自動化的骨架已經全部就位並實測過。以下是**只有你能做**的幾件事 ——
我沒有你的帳號,也不會替你申請或輸入任何憑證。

依重要性排序。第 1 項不做,自動修復就不會啟動(其餘功能不受影響)。

---

## 1. Anthropic 帳戶儲值 ← **目前唯一的阻礙**

`ANTHROPIC_API_KEY` 你已經設好了。**但實測跑出來是:**

```
result: Credit balance is too low
num_turns: 1   total_cost_usd: 0   duration_ms: 486
```

帳戶沒有餘額,所以 API 呼叫在第一步就被拒絕,486 毫秒、零花費就結束。

**要做的:** 到 https://console.anthropic.com/settings/billing 儲值。

**費用概念:** 只有健檢失敗時才會觸發,而健檢平常是綠的 —— 這個專案實際上
一年可能只跑幾次。一次診斷大約幾萬到十幾萬 token。最低儲值額度就足以撐很久。

**不做會怎樣:** 健檢照常跑、Issue 照常開,但沒有人去修 —— 回到「你手動把
Issue 貼給 Claude Code」。而且要注意:**autofix job 會顯示失敗**,
但健檢 Issue 照常開,所以表面上看不出自動修復其實沒跑起來。

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
