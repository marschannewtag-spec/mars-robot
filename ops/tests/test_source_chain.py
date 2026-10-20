"""PWA 的來源退回鏈 —— `fetchSP()` 到底會選哪一層。

## 為什麼這個檔案要存在

`fetchSP()` 是整個 PWA 裡唯一決定「使用者看到的數字來自哪裡」的地方。
它從來沒有被任何測試走過(因為它碰網路),而這條鏈已經壞過兩次:

| 何時 | 壞法 | 使用者看到的 |
|---|---|---|
| 2026-08..10 | allorigins 死了兩個月,PWA 不知道 | 每次冷啟動白等 14 秒,然後 Shiller 月均價 |
| 2026-10-19 | 我加的 `if(!worker && !key)` 把設了 key 的人排除在鏡像之外 | key 失效時直接掉到 Shiller 月均價 |

兩次都是同一個形狀:**一條寫了但走不到的退回路徑**。
而它不會讓畫面壞掉 —— 只會讓數字默默換成另一個定義(Shiller 的 SP500 欄
是月均價不是月收盤,在 17.3% 的月份給出不同的現金建議)。

所以這裡斷言的不只是「最後選了誰」,還有 **`calls`:哪些層真的被試過**。
只看結果的話,「跳過鏡像直接成功命中代理」和「照順序試到代理」是分不開的。

## 這些測試不碰網路

`tests/fetchsp_bridge.mjs` 在 jsdom 裡載入**線上真的在跑的 index.html**,
把 `window.fetch` 換成一個由情境檔控制的假函式。所以它驗的是真的那支
`fetchSP`,不是另抄一份。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import require_js_bridge  # noqa: E402

TESTS_DIR = Path(__file__).resolve().parent
BRIDGE = TESTS_DIR / "fetchsp_bridge.mjs"


ALL_DEAD = {"worker": False, "key": False, "mirror": False,
            "proxy": False, "shiller": False}


def run_chain(index_html: Path, tmp_path: Path, *, worker=None, key=None,
              alive: dict | None = None) -> dict:
    """跑一次 fetchSP,回傳 {src, months, altDef, calls, error}。

    `worker` / `key` 是「使用者有沒有設定」;`alive` 是「那一層活不活著」。
    兩者刻意分開成不同參數 —— 第一版把 alive 攤成 **kwargs,於是
    `worker=...`(有沒有設定)和 `worker=True`(活不活著)直接撞名,
    三條測試在 TypeError 上失敗。設定與死活是兩個獨立的軸。
    """
    require_js_bridge(
        "五層來源退回鏈(自有 Worker → TwelveData → 本專案鏡像 → Yahoo 經代理"
        " → Shiller)。少了它,『某一層默默死掉而畫面照樣有數字』這個本專案"
        "實際發生過的故障就不再被驗證。")
    scenario = {"worker": worker, "key": key,
                "alive": {**ALL_DEAD, **(alive or {})}}
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(scenario), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(BRIDGE), str(index_html), str(path)],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    if proc.returncode != 0:
        pytest.fail("橋接器失敗(exit %d):\n%s" % (proc.returncode, proc.stderr))
    return json.loads(proc.stdout)


def tried(res: dict, needle: str) -> bool:
    return any(needle in c for c in res["calls"])


# --------------------------------------------------------------------------
# 預設路徑:什麼都沒設定
# --------------------------------------------------------------------------

def test_with_nothing_configured_the_mirror_serves(index_html: Path, tmp_path: Path):
    """沒設 Worker、沒設 key → 走本專案鏡像。

    這是絕大多數使用者的路徑。2026-10-19 之前它走的是已經全死的代理鏈。
    """
    r = run_chain(index_html, tmp_path,
                  alive={"mirror": True, "proxy": True, "shiller": True})
    assert r["src"] == "每日 · 本專案鏡像"
    assert r["months"] >= 13
    assert not r["altDef"]
    assert not tried(r, "corsproxy.io"), "鏡像成功時不該再去碰代理"


def test_the_mirror_is_tried_before_the_proxy(index_html: Path, tmp_path: Path):
    """順序本身也要驗:鏡像的請求必須早於代理的請求。

    只斷言「最後選了鏡像」的話,一個先試代理再試鏡像的實作也會通過 ——
    而那會讓每次冷啟動先白等代理逾時。
    """
    r = run_chain(index_html, tmp_path,
                  alive={"mirror": True, "proxy": True, "shiller": True})
    order = [c for c in r["calls"]
             if "/pwa/sp_monthly.json" in c or "corsproxy.io" in c]
    assert order and "/pwa/sp_monthly.json" in order[0]


# --------------------------------------------------------------------------
# 這兩條釘的就是 2026-10-19 那個 bug
# --------------------------------------------------------------------------

def test_a_failing_api_key_falls_back_to_the_mirror_not_to_shiller(
    index_html: Path, tmp_path: Path
):
    """設了 key 但 key 失效 → **鏡像**,不是 Shiller。

    這是我自己寫進去的 bug:`if(!worker && !key)` 把設了 key 的使用者
    排除在鏡像之外,於是 key 過期那天他們會掉到已經全死的代理鏈、
    再掉到 Shiller 月均價 —— 而畫面上只會多一條警示列,不會說
    「你的 key 失效了」。

    「API key 錯誤或額度用盡」是這份程式碼別處就預期得到的情況
    (見 load() 的錯誤訊息),所以這不是假想的路徑。
    """
    r = run_chain(index_html, tmp_path, key="expired-key",
                  alive={"mirror": True, "proxy": True, "shiller": True})
    assert tried(r, "twelvedata.com"), "使用者設了 key,至少要試一次"
    assert r["src"] == "每日 · 本專案鏡像", (
        "key 失效後應該退到鏡像,實際退到:%s" % r["src"])
    assert not r["altDef"], "不該落到 Shiller 的月均價定義"


def test_a_failing_worker_falls_back_to_the_mirror(index_html: Path, tmp_path: Path):
    """設了 Worker 但 Worker 掛了 → **鏡像**,不是 Shiller。同一個 bug 的另一半。"""
    r = run_chain(index_html, tmp_path, worker="https://w.example/",
                  alive={"mirror": True, "proxy": True, "shiller": True})
    assert tried(r, "w.example"), "使用者設了 Worker,至少要試一次"
    assert r["src"] == "每日 · 本專案鏡像"
    assert not r["altDef"]


# --------------------------------------------------------------------------
# 使用者明確設定的來源仍然優先(控制組)
# --------------------------------------------------------------------------

def test_a_working_worker_wins(index_html: Path, tmp_path: Path):
    """Worker 正常時用 Worker —— 那是使用者明確的選擇。

    控制組:證明上面兩條的差異來自「那一層失敗了」,
    而不是「Worker/key 這兩層根本沒接上」。
    """
    r = run_chain(index_html, tmp_path, worker="https://w.example/",
                  alive={"worker": True, "mirror": True,
                         "proxy": True, "shiller": True})
    assert r["src"] == "每日 · 自有Worker"
    assert not tried(r, "/pwa/sp_monthly.json"), "Worker 成功時不該再去抓鏡像"


def test_a_working_api_key_wins(index_html: Path, tmp_path: Path):
    """key 正常時用 TwelveData —— 使用者要的是盤中即時。"""
    r = run_chain(index_html, tmp_path, key="good-key",
                  alive={"key": True, "mirror": True,
                         "proxy": True, "shiller": True})
    assert r["src"] == "即時 · SPY/TwelveData"
    assert not tried(r, "/pwa/sp_monthly.json")


# --------------------------------------------------------------------------
# 鏡像壞掉時,下面的層仍然接得住
# --------------------------------------------------------------------------

def test_a_broken_mirror_falls_through_to_the_proxy(index_html: Path, tmp_path: Path):
    """鏡像 404 → 代理(如果代理還活著)。

    鏡像是新的單點故障。這條證明舊的那條路沒有被拆掉,只是退到第四順位。
    """
    r = run_chain(index_html, tmp_path,
                  alive={"proxy": True, "shiller": True})
    assert tried(r, "/pwa/sp_monthly.json")
    assert r["src"] == "每日 · Yahoo^GSPC(免金鑰)"
    assert not r["altDef"]


def test_everything_dead_falls_to_shiller_and_flags_the_definition(
    index_html: Path, tmp_path: Path
):
    """全部掛掉 → Shiller,而且 **altDef 必須為真**。

    Shiller 的 SP500 欄是當月每日收盤的平均價,不是月收盤 ——
    與回測基準不同的定義,在 17.3% 的月份給出不同的現金建議。
    無聲切換到它是這個專案明文禁止的事,`altDef` 就是畫面上那條警示列的開關。
    """
    r = run_chain(index_html, tmp_path, alive={"shiller": True})
    assert r["src"] == "月頻 · Shiller(退回)"
    assert r["altDef"] is True, "退到月均價定義卻沒有標示,等於無聲換定義"


def test_when_even_shiller_is_dead_it_raises_rather_than_returning_garbage(
    index_html: Path, tmp_path: Path
):
    """連 Shiller 都抓不到 → 拋,讓 load() 去走離線快取那條路。

    回一個空陣列的話 compute() 會在 `sLast.mom` 當場炸掉,
    而畫面會停在「讀取資料中…」—— 使用者完全無從得知發生了什麼。
    """
    r = run_chain(index_html, tmp_path)
    assert r["src"] is None
    assert r["error"], "全部來源皆死時必須拋例外,不能靜靜回一個空結果"


# --------------------------------------------------------------------------
# 退回鏈不可以被前置條件整層跳過(釘住 bug 的形狀,不只是那一個實例)
# --------------------------------------------------------------------------

def test_the_mirror_layer_has_no_precondition(index_html: Path):
    """原始碼層級:鏡像那一層不得有 `if(!worker && !key)` 之類的前置條件。

    上面幾條驗的是行為;這一條驗的是**那個 bug 的形狀**。
    優先順序由順序表達就夠了 —— 任何加在鏡像層上的條件式,
    唯一的效果都是「把某些使用者排除在這層退回之外」。
    """
    src = index_html.read_text(encoding="utf-8")
    # ⚠ 先把註解整行拿掉。第一版沒拿,於是掃到了**解釋「為什麼不能有條件」
    #   的那段註解**本身,測試在正確的程式碼上失敗。
    #   掃原始碼的測試一定要先決定「註解算不算程式碼」—— 這裡不算。
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("//"))
    i = code.index("SP_MIRROR,opt")
    window = code[max(0, i - 400):i]
    assert "if(!worker" not in window and "if (!worker" not in window, (
        "鏡像那一層又被加上前置條件了 —— 它必須是無條件的退回點:\n%s" % window[-200:])
