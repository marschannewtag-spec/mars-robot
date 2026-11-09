# 換一台電腦接手這個專案

**結論先講:這個 repo 不需要「打包」。能帶走的東西全部都在 GitHub 上,
`git clone` 就是完整的一份。** 下面是新機器上從零到能跑的完整步驟,
以及**哪些東西不在 repo 裡**(那才是真正會卡住的地方)。

---

## 1. 需要先有的東西

| 工具 | 版本 | 為什麼 |
|---|---|---|
| Git | 任意近期版本 | 含 Git Bash(Windows 上測試需要 `bash`) |
| Python | **3.12** | CI 釘 3.12。3.13 多半也能跑,但 CI 綠不代表你那台綠 |
| Node.js | **22** | 測試用 jsdom 載入真的 `index.html`,版本差異會影響 `Date.parse` 之類的行為 |
| GitHub CLI (`gh`) | 任意 | 開 PR / 看 CI 用。非必要,但沒有它很麻煩 |

⚠ 本機版本與 CI 宣告不同時,`ops/tests/` 裡有一條測試會把差異講出來。
它不會自動擋你,但看到那條訊息請當真 —— 這個 repo 記錄過兩次
「CI 全綠、使用者一跑就爆」。

---

## 2. 從零到能跑

```bash
git clone https://github.com/marschannewtag-spec/mars-robot.git
```

```bash
cd mars-robot && pip install -r ops/requirements.txt -r ops/requirements-test.txt
```

```bash
cd ops && npm ci
```

⚠ **兩份 requirements 都要裝。** 只裝第一份的話 pytest 根本不在 —— 執行期相依
與測試工具自 2026-10-19 起刻意分開,理由見 `ops/DEPENDENCIES.md` 的 D1。

---

## 3. 驗證裝對了

```bash
cd ops && python -m pytest -q
```

2026-11-03 當時是 **619 passed**(這個數字會長,只要全綠就對了)。
跑不完整代表上面某一步漏了 —— 這套測試**刻意不跳過**任何一條:
外部工具缺席一律 `pytest.fail`,並說出「失去了什麼驗證」。

本機看畫面:

```bash
python -m http.server 8788
```

然後開 `http://localhost:8788/`。(Claude Code 裡直接用 `.claude/launch.json`
的 `cwgauge` 設定,不要自己開 server。)

---

## 4. 不在 repo 裡的東西 —— 這才是會卡住的地方

### 4.1 GitHub secrets(新機器**不需要**,但換帳號會需要)

這五個住在 GitHub repo 的 Settings → Secrets,不在任何檔案裡,
也拿不回來(只能重新產生):

```
CLAUDE_CODE_OAUTH_TOKEN
ANTHROPIC_API_KEY
ANTHROPIC_ORGANIZATION_ID
ANTHROPIC_SERVICE_ACCOUNT_ID
ANTHROPIC_FEDERATION_RULE_ID
```

只有 `daily-health.yml` 的 autofix job 會用到。**在新電腦上開發完全不需要它們**
—— workflow 跑在 GitHub 上,不是你的機器上。只有在你把 repo 搬到**另一個
GitHub 帳號**時才要重設。

### 4.2 `ops/data/`(gitignore,會自己長出來)

| 檔案 | 哪裡來 |
|---|---|
| `ops/data/raw/*.csv` | `python ops/src/fetch_data.py` 重抓 |
| `ops/data/pwa/`、`manifest.json`、`health_log.csv`、`signals.json` | 住在 **`ops-data` 分支**,每日 workflow 維護 |

所以不用複製。要看現況:`git fetch origin ops-data && git show origin/ops-data:ops/data/health_log.csv`

### 4.3 分支保護與 Pages(帳號層級設定,不隨 clone 走)

- `main` 有分支保護,要求 `L2 邏輯一致性` 通過才能合併。**不要關掉它** ——
  autofix 的 `contents: write` 之所以安全,靠的就是這道牆。
- GitHub Pages 從 `main` 部署到 https://marschannewtag-spec.github.io/mars-robot/
- `ops-data` 是資料分支,**不要刪、不要 rebase**。心跳、鏡像、報告都在上面。

### 4.4 這台機器上、repo 外面的檔案

`D:\GITHUB\ROBOT\` 底下除了 `mars-robot-main/` 還有:

- `.claude/launch.json` —— 外層的預覽設定(指向子資料夾、port 8765)。
  **不需要帶走**,repo 自己有一份 `.claude/launch.json`(port 8788)。
- `base44-prompt.md` —— 給 Base44 重做一份的需求描述。跟專案無關,
  想留的話自己複製。

### 4.5 Claude Code 自己的設定(帳號層級,不隨 repo 走)

`ops/CLAUDE.md` 最後一節寫了這個專案與外部規則集的交界,其中提到
**ponytail**。那是個 plugin,裝在你的 Claude Code 帳號上而不是 repo 裡。
新機器上登入同一個帳號通常就有;沒有也不影響專案跑。

---

## 5. 新機器上第一件該做的事

開 Claude Code,`cd` 進 repo,然後讀這三份(**順序有意義**):

| 檔案 | 什麼時候讀 |
|---|---|
| `ops/CLAUDE.md` | **改任何程式之前。** 工程規格、系統不變量、資料定義、三條紅線 |
| `ops/DEPENDENCIES.md` | 碰相依、CI、執行環境之前 |
| `ops/POSTMORTEMS.md` | 需要知道「為什麼會有那條規則」的時候才翻。append-only,很長 |

### 三條絕對不能做的事(寫在 `ops/CLAUDE.md`,這裡再寫一次)

1. **不准放寬健檢閾值讓檢查通過。**
2. **不准修改 `ops/tests/fixtures/` 的黃金基準讓測試過。**
3. **不准把失效的東西加進 `known_dead_proxies` / `accepted` 讓紅燈變綠。**

這三條是「把溫度計砸掉來退燒」。要動黃金基準只有一種情況:**你刻意改了策略**,
而且要在 commit 訊息裡寫清楚為什麼數字該變。

### 其他紀律

- **合併留給人。** 沒有 auto-merge,任何自動流程都不准推 `main`。
- **每個結論標證據等級**:`實測` / `靜態檢查` / `推論`。
- 改 `index.html` 任何內容 → `sw.js` 的 `CACHE = "cwgauge-vN"` 版本號 +1。

---

## 6. 目前的狀態(2026-11-09)

- `main` 最新:PR #29 合併(sw.js install 失敗不再被吞掉)
- PWA 快取版本:`cwgauge-v33`
- 五支 workflow 全部跑在 `ubuntu-26.04`,`actions/*` 都在 Node 24 那一代
- 分支只剩 `main` 與 `ops-data`

### 交接時記下的已知問題:已修(PR #29)

`sw.js` 的 `install` 原本把 `cache.addAll()` 的失敗 `catch(() => {})` 吞掉,
於是部署那幾秒第一次載入的使用者會帶著空的快取被啟用,下次版本號 +1 之前
離線都打不開。現在 install 會 reject,瀏覽器丟掉那個新 worker、下次開頁面重試;
`CACHE` 升到 v33 讓已中招的使用者重裝一次。經過與測試見
`ops/POSTMORTEMS.md` 的 2026-11-09 那條。

### 新機器實際接手時踩到的(2026-11-09,Windows 11)

照第 2 節的三行指令,在一台全新的 Windows 上實際跑過一次,**`實測` 619 → 621
passed、1 skipped**(skip 的是 `signals.json`,不進版控,CI 上同樣 skip)。
三行指令本身沒問題,卡住的都是指令**之前**的東西:

- **沒有 Git。** `winget install Git.Git` 與安裝程式都可能失敗(下載 504、
  系統安裝要 UAC)。免安裝的 PortableGit 解壓到 `%LOCALAPPDATA%\Programs\Git`,
  把 `cmd\` 與 `bin\` 加進使用者 PATH 即可 —— `bin\` 才有測試要的 `bash`。
- **`python` 是 Microsoft Store 的空殼。** 已經裝了 Python 但沒加進 PATH 時,
  `python` 會指向 `WindowsApps\python.exe` 並叫你去 Store。用 `py -0p` 找出
  真正的路徑,把它排在使用者 PATH 的 `WindowsApps` **前面**。
- **PortableGit 的預設 credential helper 是 `helper-selector`**,第一次 push 會卡在
  「Cannot prompt」。設 `git config --global credential.helper manager`,
  然後在互動式終端機裡 push 一次登入。
