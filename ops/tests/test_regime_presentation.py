"""體制層的畫面呈現(2026-10-20 決策 T3 之二)。

決策內容:**不動主訊號參數**(MA12、VIX 階梯都不改),改讓體制層在畫面上
更看得見 —— 但**不得暗示這些早期警訊已經改變了現金水位**。

為什麼這件事需要測試
--------------------
使用者在回調中覺得「慢半拍」,而下面六條警訊可能已經亮了。兩個數字分在畫面
的兩端時,很容易把「警訊亮了」讀成「水位要動了」。這一組測試釘的就是那條線:

- 兩個讀數要**同時在視野裡**
- 要**明講彼此不影響**
- 而且 `renderRegime()` **仍然不准碰**主訊號那一行 ——
  方向是核心 → 體制層,不能反過來

第三條特別容易被「順手」破壞:讓 renderRegime 自己去讀核心節點、把兩件事
寫在一起,看起來更內聚,實際上是把那道硬邊界拆了。
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from conftest import OPS_ROOT, REPO_ROOT, require_js_bridge

INDEX = REPO_ROOT / "index.html"
RENDER_BRIDGE = OPS_ROOT / "tests" / "render_bridge.mjs"
REGIME_BRIDGE = OPS_ROOT / "tests" / "regime_bridge.mjs"


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text(encoding="utf-8")


def _node(bridge: Path, *args: str) -> dict:
    require_js_bridge(
        "體制面板的呈現 —— 兩個讀數有沒有同時出現、有沒有明講彼此不影響、"
        "已觸發的門檻有沒有露在外面")
    r = subprocess.run(["node", str(bridge), str(INDEX), *args],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=OPS_ROOT, timeout=120)
    assert r.returncode == 0, f"橋接器失敗:\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout)


# ==========================================================================
# 方向:核心 -> 體制層,不能反過來
# ==========================================================================

def test_the_main_signal_line_lives_inside_the_regime_panel(html: str) -> None:
    """兩個讀數要同時在視野裡,否則這整個改動沒有意義。"""
    panel = html[html.index('<div class="panel" id="regimepanel">"'.rstrip('"')):]
    panel = panel[:panel.index("</div>\n\n<details>")] if "</div>\n\n<details>" in panel \
        else panel[:4000]
    assert 'id="mainstate"' in panel, (
        "#mainstate 不在體制面板裡 —— 它存在的理由就是讓兩個讀數同時被看到")


def test_the_core_writes_it_and_the_regime_layer_does_not(html: str) -> None:
    """硬邊界的方向。

    `renderRegime()` 去讀核心節點看起來更內聚,實際上是把
    `test_pwa_regime_layer_does_not_touch_the_cash_number` 擋的那件事
    換一個寫法做掉。核心把算好的事實放進體制面板是單向的,沒有這個問題。
    """
    regime = html[html.index("function renderRegime"):html.index("function checkRegime")]
    assert '$("mainstate")' not in regime, (
        "renderRegime 寫了 #mainstate —— 那一行是核心的事實,"
        "體制層碰它就等於有能力覆蓋主訊號的陳述")

    core = html[html.index("function render(data"):html.index("function renderRegime")]
    assert '$("mainstate")' in core, "核心沒有寫 #mainstate,那一行會永遠是空的"


def test_rendering_the_regime_panel_does_not_disturb_the_main_signal_line() -> None:
    """`實測`:真的跑一次 renderRegime,那一行必須原封不動(此處為空)。"""
    sig = _signals()
    out = _with_signals(sig)
    assert out["mainstate"] == "", (
        f"renderRegime 動到了主訊號那一行:{out['mainstate']!r}")
    assert out["panel_on"], "面板沒有打開,後面幾條就沒有在測東西"


# ==========================================================================
# 那一行說了什麼
# ==========================================================================

@pytest.mark.parametrize("scenario,must,must_not", [
    ("main_idle", ["0%", "尚未觸發", "不會"], []),
    ("main_fired", ["24%", "趨勢腿", "VIX 腿", "不會"], ["尚未觸發"]),
])
def test_the_line_reports_the_actual_state(scenario: str, must: list[str],
                                           must_not: list[str]) -> None:
    """兩腿都沒觸發 vs 已觸發,必須說出不同的話。

    說同一句話的那一行不是在報告狀態,只是一句裝飾 —— 而裝飾會讓人以為
    自己看過了。
    """
    out = _node(RENDER_BRIDGE, scenario)
    text = out["mainstate"]
    for s in must:
        assert s in text, f"[{scenario}] 少了 {s!r}:{text}"
    for s in must_not:
        assert s not in text, f"[{scenario}] 不該出現 {s!r}:{text}"


def test_the_line_never_implies_the_warnings_moved_the_number() -> None:
    """決策寫死的那一條:不得暗示早期警訊改變了現金水位。"""
    for scenario in ("main_idle", "main_fired"):
        text = _node(RENDER_BRIDGE, scenario)["mainstate"]
        assert "不會" in text and "改變" in text, (
            f"[{scenario}] 沒有明講兩者不互相影響:{text}")


def test_the_two_readings_cannot_drift_apart() -> None:
    """那一行的百分比必須等於主讀數。

    它是同一個數字的第二份呈現 —— 兩份就會漂移,而漂移的那一邊沒有人看著。
    這裡直接比對同一次 render 的兩個節點。
    """
    out = _node(RENDER_BRIDGE, "main_fired")
    big = re.search(r"(\d+)", out["bigpct"])
    line = re.search(r"主訊號目前 (\d+)%", out["mainstate"])
    assert big and line, f"抓不到數字:{out['bigpct']!r} / {out['mainstate']!r}"
    assert big.group(1) == line.group(1), (
        f"主讀數 {big.group(1)}% 與體制面板上那一行的 {line.group(1)}% 不一致")


# ==========================================================================
# 六條警訊:已觸發的門檻要露在外面
# ==========================================================================

def _signals(**over) -> dict:
    """一份最小但合法的 signals.json。各測試只覆寫需要的部分。"""
    base = {
        "schema": 1,
        "generated_at": "2026-10-20T01:00:00+00:00",
        "dominance": {"leader": "美國", "scores": {"美國": 0.76, "歐洲": 0.16},
                      "matrix": {}, "margin": 4.8, "n_markets": 2, "window": "2y"},
        "warning": {
            "n_triggered": 1, "n_usable": 3, "band": "留意",
            "flags": [
                {"key": "greed", "label": "情緒極端貪婪", "triggered": True,
                 "detail": "綜合 81", "rule": "綜合分數 ≥ 75",
                 "source": "CNN Fear & Greed", "ok": True, "why": "歷史頭部多在此區"},
                {"key": "breadth", "label": "廣度背離", "triggered": False,
                 "detail": "綜合 81 · 廣度 55", "rule": "綜合 ≥ 60 且 廣度 ≤ 35",
                 "source": "CNN", "ok": True, "why": "內部惡化"},
                {"key": "credit", "label": "信用轉弱", "triggered": False,
                 "detail": "抓不到", "rule": "20 日變化 ≤ -10",
                 "source": "CNN", "ok": False, "why": "信用面領先"},
            ],
        },
    }
    base.update(over)
    return base


def _with_signals(sig: dict) -> dict:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "signals.json"
        f.write_text(json.dumps(sig, ensure_ascii=False), encoding="utf-8")
        return _node(REGIME_BRIDGE, str(f))


def _summaries(html: str) -> list[str]:
    """每一條 flag 的 <summary> 內容 —— 也就是**不用點開就看得到**的那部分。

    ⚠ 不能用 textContent 判斷「有沒有露出來」:jsdom(以及 DOM 本身)的
    textContent **不管折疊狀態**,`<details>` 裡收起來的 `.fbody` 一樣算進去。
    `實測`:第一版就是這樣寫的,結果「沒觸發的仍然收起來」那條必定失敗,
    而它失敗的原因與畫面無關。**判準要對準那個宣稱** —— 這裡的宣稱是
    「在 summary 裡」,那就去看 summary。
    """
    return re.findall(r"<summary>(.*?)</summary>", html, re.S)


def test_no_background_token_is_used_as_a_text_colour() -> None:
    """背景色 token 不得當成文字色 —— 那等於把字寫成隱形。

    ⚠ 2026-10-25 實測到的線上故障,與上面那條同一次改動、同樣壞了五天:

        #mainstate b{color:var(--ink)}

    在這套設計 token 裡 `--ink`(#0E1726)是**頁面背景色**,不是「墨水色」。
    結果那一行最重要的字(`主訊號目前 24%`)對比度 **1.13:1** —— 肉眼幾乎看不到。
    文字色只有三個:`--text` / `--muted` / `--faint`。

    名字會騙人:`ink` 聽起來像前景,實際是背景。**不要從 token 的名字推語意,
    去看它被宣告成什麼。**

    ⚠ 要先把 CSS 註解拿掉再比對 —— 上面那段說明裡就寫著 `color:var(--ink)`,
    而字串比對會把說明本身判成違規,逼人刪掉最該留的註解。
    (與 regime 那三條 AST 掃描同一個理由。)
    """
    html = INDEX.read_text(encoding="utf-8")
    style = html[html.index("<style>"):html.index("</style>")]
    clean = re.sub(r"/\*.*?\*/", "", style, flags=re.S)

    bad = []
    for token in ("--ink", "--surf", "--surf2", "--line"):
        for m in re.finditer(r"(?<![-\w])color:\s*var\(" + token + r"\)", clean):
            line = clean[:m.start()].count("\n") + 1
            bad.append(f"`{token}` 被當成文字色(<style> 第 {line} 行附近)")
    assert not bad, (
        "背景色 token 被當成文字色:\n  " + "\n  ".join(bad) + "\n\n"
        "文字色請用 --text / --muted / --faint。"
        "背景 token 當文字色的症狀是「看起來什麼都沒寫」,而測試不會紅。")


def test_every_summary_child_has_a_place_in_the_grid() -> None:
    """`.flag>summary` 是 grid —— **每一個子元素都必須有欄位可站**。

    ⚠ 2026-10-25 實測到的線上故障,壞了五天:

        .flag>summary{grid-template-columns:14px 1fr auto}   /* 三欄 */
        <summary><span.dot><span.fl><span.fd><span.frule>    /* 四個子元素 */

    第四個沒有明確放置,被自動排到**第二列第一欄**(寬 14px),
    而它身上帶著 `margin-left:18px` —— 18 > 14,內容寬度被壓成 **0**,
    文字變成一字一行。實測 `width=0, height=189px`,
    兩條已觸發的警訊各佔掉半個螢幕。

    **為什麼原本的測試沒抓到:** 它斷言 `.frule` 出現在 summary 的 HTML 裡,
    而那是**字串存在**,不是那個宣稱(「看得到」)。span 確實在,只是寬度 0。
    這是本 repo 第七次踩到「判準對準字串而非宣稱」。

    **為什麼不用瀏覽器測:** jsdom 不做排版,`getBoundingClientRect()` 一律回 0,
    量不出這個差別;而 L2 必須離線。所以改成查**機制**:
    **排在宣告欄數之後的子元素,必須自己宣告 `grid-column`。**
    這與「寬度 0」是同一件事的兩種說法,而這一種離線測得到。
    """
    html = INDEX.read_text(encoding="utf-8")

    # 1) CSS 宣告了幾欄(預設 + 窄螢幕覆寫,兩個都要算)
    decls = re.findall(r"\.flag>summary\{[^}]*?grid-template-columns:([^;}]+)", html)
    assert decls, "找不到 `.flag>summary` 的 grid-template-columns —— 前提變了,請重寫這條"
    col_counts = sorted({len(d.strip().split()) for d in decls})
    assert col_counts, "欄數解析不出來"

    # 2) renderRegime 實際產生的子元素,【依順序】
    out = _with_signals(_signals())
    summaries = _summaries(out["flaglist_html"])
    assert summaries, "沒有產生任何 flag,這條測試沒在量東西"
    orders = {tuple(re.findall(r'<span class="([\w-]+)"', s)) for s in summaries}
    assert orders, "summary 裡抓不到任何 span"

    # 3) 哪些 class 有宣告 grid-column(任何一處都算)
    placed = set(re.findall(r"\.([\w-]+)\{[^}]*grid-column:", html))

    problems = []
    for cols in col_counts:
        for order in orders:
            for idx, cls in enumerate(order):
                if idx >= cols and cls not in placed:
                    problems.append(
                        f"{cols} 欄的版型下,第 {idx + 1} 個子元素 `.{cls}` "
                        f"排在欄數之外卻沒有宣告 grid-column")
    assert not problems, (
        "summary 的 grid 放不下它的子元素:\n  " + "\n  ".join(sorted(set(problems)))
        + "\n\n沒有明確放置的子元素會被排到下一列的第一欄(只有 14px 寬),"
          "文字擠成一字一行 —— 而畫面以外沒有任何症狀,測試也不會紅。")


def test_a_triggered_flag_shows_its_threshold_without_clicking() -> None:
    """要點開才看得到的條件,等於沒有寫。

    這是決策裡「畫面可以更清楚地呈現體制層的等級、觸發條件和資料日期」
    那一句的可測版本。
    """
    out = _with_signals(_signals())
    triggered = _summaries(out["flaglist_html"])[0]
    assert "已達門檻" in triggered, f"已觸發的那條沒有把門檻露出來:{triggered}"
    assert "綜合分數 ≥ 75" in triggered


def test_a_quiet_flag_stays_folded() -> None:
    """沒觸發的仍然收起來 —— 六條全攤開會變成雜訊牆,然後整塊被忽略。

    這條是上一條的對照組。少了它,「每一條都把門檻攤開」會是讓上一條
    通過的最省事寫法,而那會讓這個面板變成沒有人讀的東西。
    """
    quiet = _summaries(_with_signals(_signals())["flaglist_html"])[1]
    assert "frule" not in quiet, f"沒觸發的那條也把門檻攤開了:{quiet}"
    assert "廣度 ≤ 35" not in quiet


def test_missing_data_does_not_look_like_a_quiet_flag() -> None:
    """**「沒得判斷」和「沒觸發」不可以長得一樣。**

    這是本 repo 的第一條規則。混在一起的話,資料源死掉會偽裝成一切平安 ——
    而那正是這整個專案在防的事。這裡三條各一種狀態,互相當對照組。
    """
    s = _summaries(_with_signals(_signals())["flaglist_html"])
    assert len(s) == 3, f"預期三條 flag,拿到 {len(s)}"
    assert "沒得判斷" in s[2], f"ok=false 的那條沒說出「沒得判斷」:{s[2]}"
    assert "沒得判斷" not in s[0], "已觸發的那條被說成「沒得判斷」"
    assert "frule" not in s[1], "沒觸發的那條不該有額外標示"


def test_the_threshold_text_is_escaped() -> None:
    """`rule` 來自 signals.json,是外部字串 —— 不得直接進 innerHTML。

    被入侵的資料源會變成 XSS 入口。這是專案四道防線的第 4 條,
    而我剛剛在 summary 裡新增了一個會吃它的位置。
    """
    sig = _signals()
    sig["warning"]["flags"][0]["rule"] = '<img src=x onerror="alert(1)">'
    out = _with_signals(sig)
    assert "<img" not in out["flaglist_html"], (
        f"門檻字串沒有被 esc():\n{out['flaglist_html'][:400]}")
    assert "&lt;img" in out["flaglist_html"], "應該被跳脫成實體"


def test_the_note_still_says_the_thresholds_are_not_backtested() -> None:
    """那段警語是寫死的、不得刪 —— 六個門檻真的沒有回測過。"""
    out = _with_signals(_signals())
    assert "沒有經過回測" in out["note"]
    assert "不改變上方的現金水位" in out["note"]
