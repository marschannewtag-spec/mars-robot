# 相依治理決策記錄(2026-10-19)

這份文件記錄的是**已經做出的決策與它的證據**,不是一份選項清單。
每一條都標了證據等級:

| 標記 | 意思 |
|---|---|
| `實測` | 真的跑過一次,指令與輸出可重現 |
| `靜態檢查` | 讀檔案/解析 YAML 得到的,沒有實跑那條路徑 |
| `推論` | 從前兩類推導出來的,沒有被直接驗證 |

⚠ 這個區分不是形式。2026-10-19 的交接文件把一條**沒驗過的機制**
(「被 yank 的版本會讓 `pip install` 失敗」)用跟實測相同的語氣寫進前提,
而它是**反的** —— 審閱者照單全收的話,會因為一個錯的理由否決一整條路。
實測:`pip download "packaging==20.6"`(20.6 是 PyPI 上真的被 yank 的版本)
**裝得起來**;`pip download "packaging>=20.5,<20.7"` 則**跳過它**拿到 20.5。
PEP 592 的行為是「精確釘住照裝、只給警告;範圍會跳過」。

---

## 決策總覽

| 層 | 裝什麼 | 為什麼 |
|---|---|---|
| **L1 每日健檢** | `requirements.txt`(runtime 範圍) | 它的工作是每天把資料生出來;要的是「裝得起來」 |
| **L2 CI** | `requirements-ci.lock`(精確 pin + hash) | 它的工作是當一把不會自己變動的量尺 |
| **canary** | 乾淨解析最新相容版本 | 讓「升上去會不會出事」變成被觀測的,而不是被信任的 |
| **autofix** | runtime + test 範圍 | 唯一需要跑 pytest 的 job |

**不做的事:不自動合併、不直推 `main`、不放寬黃金基準。**

---

## D1 · L1 不使用會阻塞每日健檢的精確鎖定

L1 只安裝 `requirements.txt`(有界範圍),**不安裝 pytest / pytest-cov**。

### 證據:原本的故障路徑

`靜態檢查`(讀 `daily-health.yml`,沒有實跑一次真的安裝失敗):

```
pip install 失敗 → job 紅
                 → 「執行健檢」被跳過 → steps.check.outputs.code 是空字串
                 → 開 Issue 那一步的條件不含 always() → 被跳過,不開 Issue
                 → 心跳有 always() 會跑,但沒有新資料
                    → git diff --cached --quiet → 跳過 commit → 沒有推送
```

`實測`:L1 確實安裝同一份宣告檔 —— `daily-health.yml:58`、`ci.yml:51`、
`annual-review.yml:37`、autofix job。所以在拆開之前,**pytest 的一次解析失敗
有能力讓每日資料供應停擺**,而且 Issue 通道會完全安靜。

`推論`:連續多天的安裝失敗會讓 GitHub 的 60 天排程停用計時器開始跑
(心跳在沒有新資料時不會 commit)。這一點已經由「把依賴 manifest 納入心跳」
處理掉 —— 安裝失敗那天 manifest 會變動,所以心跳仍然有東西可以 commit。

### 為什麼不是「L1 也用 lock」

`推論`:這個 repo 的前提是**沒有人定期維護它**。精確鎖定放在每日關鍵路徑上,
會把「鎖定檔過期」這件事變成**資料供應故障**。而鎖定檔真正的腐爛機制
(`實測` + 規格閱讀)是:

1. 檔案被**刪除**(不是 yank —— yank 對精確釘住無效,見上面)
2. 固定版本在未來的 Python / 平台上**沒有可用的 wheel**
3. 只剩原始碼而**建置失敗**

這三件都會讓 `pip install` 直接失敗,而不是退回舊版。所以 L1 要的是範圍。

### 範圍的紀律

下界是一句**支援承諾**,只能寫進真的驗過的 major(`實測`,2026-10-19):

| 宣告 | 原本跨幾個 major | 收成 |
|---|---|---|
| `numpy>=1.26,<3` | 2(1.x / 2.x) | `>=2.1,<3` |
| `packaging>=23,<27` | 4(23/24/25/26) | `>=26,<27` |
| `pytest-cov>=5.0,<8` | 3(5/6/7) | `>=7,<8` |
| `pytest>=8.0,<10` | 2(8/9)—— **兩個都實測全綠** | 不動 |

⚠ **證據不能轉讓。** `pytest` 的 `<10` 有「測試在 8.x 與 9.1.1 都全綠」當依據;
把那個依據挪用到 `pytest-cov` 不成立,它們是不同的套件。

`實測`:下界幾乎不影響 CI 實際裝什麼(`pip install -r` 永遠解析到範圍內的
最新版),所以收緊下界的目的**不是**改變 resolver 結果,是讓宣告變成真的。
由 `tests/test_reporting.py::test_the_running_environment_matches_requirements`
硬失敗把關(反向驗證過:把 numpy 改成 `>=2.9,<3` → 立刻紅)。

---

## D2 · L2 使用固定、帶 hash 的測試鎖定

`ops/requirements-ci.lock` —— 19 個套件,全部 `==` 精確釘住 + sha256,
由 runtime 與 test 兩份宣告一起解析產生。

### 生成指令(釘死工具版本)

```bash
cd ops
python -m pip install "uv==0.9.9"
python -m uv pip compile requirements.txt requirements-test.txt \
  --generate-hashes --python-version 3.12 --python-platform linux \
  --output-file requirements-ci.lock
```

`實測`:重新產生一次,**本體逐位元相同**(只有標頭那一行會記錄輸出路徑,
所以重新產生時要用同一個相對路徑,否則會出現一行假 diff)。

`實測`:解析結果是 `pandas 2.3.3 / numpy 2.5.4 / pytest 9.1.1` ——
**正好就是先前拿凍結黃金基準測過零漂移的那個組合**
(440 個月、`abs=1e-9`、`test_golden` 16 passed / `test_invariants` 34 /
`test_parity` 10)。所以這份鎖定不是一個沒人驗過的新組合。

⚠ `靜態檢查`:那次測量是在 Windows / Python 3.13 上做的,而這份鎖定是
Linux / Python 3.12 的解析結果。**版本組合相同,平台不同。** 真正的
Linux/3.12 驗證來自這個分支的 L2 run。

⚠ 鎖定 Python 套件**不等於**鎖定運算環境:作業系統、Python ABI、CPU 架構、
BLAS 都還是浮動的,而同一個版本在不同平台可能拿到不同的二進位輪子。
這份鎖定買到的是「套件版本不會自己變」,不是「逐位元相同的運算環境」。

### 為什麼 L2 要跟 L1 不一樣

L2 的工作是回答「邏輯有沒有被改壞」。它必須是一把不會自己變動的尺 ——
否則某天 pip 裝到新版、基準漂了 `1e-9`,紅燈的真正原因是「裝到新版」,
而它會偽裝成「程式被改壞了」。那是最難歸因的一類紅燈。

---

## D3 · canary 是獨立的最新版驗證管線

`.github/workflows/dependency-canary.yml`,每週一次 + 可手動觸發。

`靜態檢查`:它**不是** health job 的 `needs`、不讀也不寫 health 的 output、
不碰 `ops-data` 分支。由 `tests/test_dependency_policy.py` 釘住。

- 乾淨環境解析最新相容版本(**刻意不開 pip cache** —— 快取會讓它量到
  「上週的最新版」)
- 跑**與 L2 同一個入口**(`ops/scripts/ci_checks.sh`)。兩邊各寫一套 pytest
  指令的話,canary 遲早會在測一組不同的檢查,而那是一個看起來在把關、
  實際上在量別的東西的閘門
- 全綠且鎖定檔有差異 → 開 PR(**不合併**)
- 失敗 → 開/更新 `dependency-canary` 標籤的 Issue,**按版本組合去重**

### 去重 key 的定義

`實測`(`tests/test_dep_manifest.py`):

```
combination_key   = sha256(sorted(name==version) + python + platform + resolver)
incomplete_key    = sha256(input file hashes + python + platform + 正規化錯誤類別)
```

⚠ commit SHA 與 run ID **刻意不在** key 裡面。放進去的話每次執行都會算出新的
key,於是「同一個壞掉的版本組合」每週都會被當成新發現 —— 那就是這個 repo
實測過的 52 則留言的狼來了。manifest 本身仍然記錄 commit 與 run ID。

---

## D4 · 安裝失敗本身是一個看得見的健康事件

`ops/src/dep_manifest.py` —— **只用標準庫**(`實測`:AST 掃描確認 0 個
第三方 import)。它必須在「pandas 裝不起來」的那一刻跑得動。

| 路徑 | 會留下什麼 |
|---|---|
| 安裝成功 | manifest,完整版本集合,`resolution_complete=true` |
| 安裝失敗 | manifest,**已落地的部分**,`resolution_complete=false`、錯誤類別、遮蔽後的 log 尾段 |
| 兩者 | artifact `dependency-manifest`(保留 30 天)+ 心跳推到 `ops-data` |

- **健康碼**:安裝失敗 → `90`(health_check.py 的 exit code 是失敗項目數,
  目前最多約 19 項,不會混淆)。由一個 `if: always()` 的步驟寫出來,
  而不是讓下游條件去讀一個空字串。
- **Issue**:安裝失敗那條路徑自己開/更新 `health-fail` Issue,內文帶
  `<!-- failset: 依賴安裝 -->`,所以恢復正常那天會被既有的「關閉 Issue」
  步驟自動關掉,不會變成孤兒。
- **原子寫入**:先寫 `.tmp` 再 `os.replace`。**不成功就不碰目的檔** ——
  shell 的重導向會在命令執行之前就把目的檔清空,這個 repo 被那件事咬過一次
  (`health_log.csv` 變成一張沒有表頭的空表)。
- **祕密遮蔽**:URL 帳密、token 前綴、query string 憑證、`Authorization`
  標頭,以及**實際環境變數的值**(名稱含 TOKEN/SECRET/PASSWORD/API_KEY/PAT
  且長度 ≥ 8)。後者才擋得住「某個 secret 剛好不符合任何已知樣式」。
- **schema 分離**:寫 `ops/data/deps/install.json`,**不寫**
  `ops/data/manifest.json`。後者是 PWA 的心跳在讀的那一份,把依賴診斷塞進去
  等於讓一個與使用者無關的欄位有機會改變 PWA 的心跳判斷。

---

## 已知的限制(沒有被這次改造解決)

`靜態檢查` / `推論`,誠實列出:

1. **沒有真的在 GitHub Actions 上跑過一次安裝失敗。** 失敗路徑的驗證是
   「`dep_manifest.py` 的真實執行 + 解析 YAML 的條件」,不是一次真實的
   紅燈演練。要實跑需要刻意推一個壞掉的宣告檔,代價是一次真的 Issue。
2. **鎖定檔不保證三年後裝得起來。** 檔案被刪除、未來平台沒有 wheel、
   只剩原始碼而建置失敗 —— 這三種都會讓 L2 直接壞掉。L2 壞掉不影響 L1
   的資料供應(這正是 D1 的目的),但會讓 PR 無法合併。
3. **Python 3.12 的官方支援到 2028-10。** 在那之後繼續釘 3.12 意味著
   解譯器本身不再拿到安全更新。這是環境生命週期問題,鎖定套件版本解決不了。
4. **canary 全綠不代表升級安全,只代表「這一次沒看到問題」。** 黃金基準
   440 個月 `1e-9` 是目前最強的閘門,但它量的是這個策略的輸出,
   不是套件的全部行為。
5. **「環境符合宣告」不等於「開發機與 CI 相同」。** 在 `pytest>=8.0,<10`
   之下,8.x 與 9.1.1 都會通過那條測試。那是宣告允許的結果,不是環境相同。
