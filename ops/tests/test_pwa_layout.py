"""PWA 版型的幾何守衛(2026-10-25)。

為什麼需要這一組
----------------
2026-10-25 修掉四個版型缺陷,其中兩個有測試守著(grid 子元素沒位置、
背景 token 當文字色),**另外兩個沒有** —— 當時的理由是:

> jsdom 不做排版,`getBoundingClientRect()` 一律回 0;而 L2 必須離線。

那句話只對了一半。**排版引擎量不到,不代表算不出來。** 這幾條缺陷的幾何
全部由 CSS 裡幾個寫死的數字決定,把那些數字讀出來做算術就測得到,
不需要瀏覽器、不需要網路、不需要新相依。

模型的可信度不是推論,是對過帳的
--------------------------------
`.tval` 那一條的模型是「重疊量 = |top| − margin-top」。拿它對 2026-10-25
在真實瀏覽器量到的兩個數字:

    margin-top 16px -> 模型 21-16 = 5px 重疊   實測 5px   ✅
    margin-top 24px -> 模型 21-24 = -3px 間隙  實測 -3px  ✅

**兩次都精確吻合**,所以這個算術量的是真的那個機制,不是一個看起來合理的近似。

這一組【測不到】什麼
--------------------
字型換掉、字數變長而換行、瀏覽器預設值差異 —— 這些都會改變實際排版而
這裡看不到。它守的是「寫死的數字之間的關係」,不是「畫面長得對」。
**不要因為這一組是綠的就不看畫面。**
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import REPO_ROOT

INDEX = REPO_ROOT / "index.html"


@pytest.fixture(scope="module")
def css() -> str:
    """`<style>` 的內容,**註解已剝除**。

    不剝的話,說明文字裡寫的範例(例如「實測踩過:`color:var(--ink)`」)
    會被判成真的宣告 —— 那會逼人刪掉最該留的註解。
    """
    html = INDEX.read_text(encoding="utf-8")
    style = html[html.index("<style>"):html.index("</style>")]
    return re.sub(r"/\*.*?\*/", "", style, flags=re.S)


def _decl(css: str, selector: str, prop: str) -> list[str]:
    """某個選擇器的某個屬性,所有出現處(預設 + media query 覆寫)。"""
    out = []
    for block in re.findall(re.escape(selector) + r"\{([^}]*)\}", css):
        m = re.search(re.escape(prop) + r":\s*([^;]+)", block)
        if m:
            out.append(m.group(1).strip())
    return out


def _tracks(value: str) -> list[str]:
    """把 grid-template-columns 展開成逐軌清單(支援 repeat(n, x))。"""
    m = re.fullmatch(r"repeat\(\s*(\d+)\s*,\s*(.+?)\s*\)", value.strip())
    if m:
        return [m.group(2)] * int(m.group(1))
    return value.strip().split()


def _media_block(css: str, query: str) -> str:
    """切出某個 `@media` 的內容 —— **數大括號**,不要用正則。

    ⚠ 第一版寫成 `@media\\(...\\)\\{(.*)\\}\\s*$` 加 `re.S`,貪婪的 `.*`
    會一路吃到檔尾。後果:把規則搬到 media query **外面**,這條測試
    仍然說它在裡面(突變 M175 實測 survive)。
    **切區塊要數括號,正則數不了巢狀。**
    """
    i = css.index(query)
    j = css.index("{", i)
    depth, k = 0, j
    while k < len(css):
        if css[k] == "{":
            depth += 1
        elif css[k] == "}":
            depth -= 1
            if depth == 0:
                return css[j + 1:k]
        k += 1
    raise AssertionError(f"{query} 的大括號沒有配對完成")


def _covered(grid_column: str, n_tracks: int) -> list[int]:
    """`grid-column: a/b` 覆蓋哪幾條軌道(1-based)。"""
    parts = [p.strip() for p in grid_column.split("/")]
    start = int(parts[0])
    if len(parts) == 1:
        return [start]
    end = int(parts[1])
    if end < 0:                       # -1 = 最後一條格線
        end = n_tracks + 1 + (end + 1)
    return list(range(start, end))


# ==========================================================================
# 1. 刻度數字不得壓到標題列(突變 M174)
# ==========================================================================

def test_the_scale_value_has_room_above_the_bar(css: str) -> None:
    """`.tval` 定位在 `.tempbar` 上方 |top| 處,那段空間由 bar 的 margin-top 給。

    `實測`(2026-10-25,375×812):margin-top 16px 時 `.tval` 與 `.templab`
    重疊 **5px** —— 現金水位落在 20–30% 區間時,刻度上那個數字會蓋到「警戒」。

    模型:`重疊 = |tval.top| − tempbar.margin-top`。對過帳:
    16px → 預測 +5、實測 +5;24px → 預測 −3、實測 −3。**兩次都精確吻合。**
    """
    tops = _decl(css, ".tval", "top")
    assert tops, "找不到 `.tval` 的 top —— 前提變了,請重寫這條"
    top_px = abs(int(re.match(r"-?(\d+)", tops[0].replace("-", "-")).group(0)))

    margins = _decl(css, ".tempbar", "margin")
    assert margins, "找不到 `.tempbar` 的 margin"
    margin_top = int(re.match(r"(\d+)", margins[0]).group(1))

    assert margin_top >= top_px, (
        f"`.tval` 在 bar 上方 {top_px}px,但 `.tempbar` 只留了 "
        f"{margin_top}px —— 會壓到 `.templab` {top_px - margin_top}px。\n"
        "水位落在 20–30% 時那個數字正好在「警戒」上面,實測看得到。\n"
        "修法是加大 `.tempbar` 的上緣 margin,不是把 `.tval` 往下移"
        "(往下移會蓋到色條本身)。")


# ==========================================================================
# 2. stat 不得在窄螢幕變成孤兒(突變 M173)
# ==========================================================================

def test_the_stat_strip_never_leaves_an_orphan(css: str) -> None:
    """`.strip` 的格子數除不盡欄數時,最後一張必須橫跨整列。

    `實測`(2026-10-25):三張 stat 塞進兩欄,第三張獨佔半寬(164px)、
    右邊空一塊,邊緣參差。跨欄後 339px 滿版。

    這一條會隨「以後加第四張 stat」而自動重算 —— 四張配兩欄除得盡,
    就不再要求跨欄。判準跟著實際的格子數走,不是寫死一個結論。
    """
    html = INDEX.read_text(encoding="utf-8")
    n_stats = len(re.findall(r'<div class="stat"', html))
    assert n_stats >= 2, f"只找到 {n_stats} 張 stat —— 這條測試沒在量東西"

    span = _decl(css, ".strip .stat:last-child", "grid-column")

    for value in _decl(css, ".strip", "grid-template-columns"):
        cols = len(_tracks(value))
        if n_stats % cols == 0:
            continue
        assert span, (
            f"{n_stats} 張 stat 配 {cols} 欄會剩下 {n_stats % cols} 張孤兒,"
            "而 `.strip .stat:last-child` 沒有宣告 grid-column。\n"
            "最後一張會獨佔一格、右邊空一塊。")
        covered = _covered(span[0], cols)
        assert len(covered) == cols, (
            f"`.strip .stat:last-child{{grid-column:{span[0]}}}` 只跨了 "
            f"{len(covered)} 欄,但要填滿一整列需要 {cols} 欄")


def test_the_orphan_fix_is_scoped_to_narrow_screens(css: str) -> None:
    """跨欄規則只能在窄螢幕生效 —— 對照組。

    少了這條,「把跨欄規則搬到全域」會讓上面那條照樣綠,而桌面版
    (三欄、三張,本來就除得盡)的最後一張會橫跨整列,變成另一種走鐘。
    """
    inner = _media_block(css, "@media(max-width:430px)")
    assert ".strip .stat:last-child" in inner, (
        "跨欄規則不在 `@media(max-width:430px)` 裡 —— 桌面版是三欄三張,"
        "本來就不需要跨欄,全域生效會讓最後一張無故橫跨整列")


# ==========================================================================
# 3. 門檻文字必須落在有寬度的軌道上(突變 M171)
# ==========================================================================

def test_the_threshold_text_sits_on_a_flexible_track(css: str) -> None:
    """`.frule` 必須覆蓋到 `1fr` 那條軌道,不能只站在固定的窄軌上。

    `test_every_summary_child_has_a_place_in_the_grid` 只保證它**有**位置;
    這一條保證那個位置**有寬度**。兩者缺一不可:

        grid-column:1      -> 有位置,但那是 14px 的圓點欄,文字照樣擠成一行一字
        grid-column:2/-1   -> 覆蓋 1fr,正確

    判準是「有沒有覆蓋到可伸縮軌道」,不是比對字串 `2/-1` ——
    哪天欄位順序改了,這條仍然問對問題。
    """
    span = _decl(css, ".frule", "grid-column")
    assert span, (
        "`.frule` 沒有宣告 grid-column —— 見 "
        "test_every_summary_child_has_a_place_in_the_grid")

    layouts = _decl(css, ".flag>summary", "grid-template-columns")
    assert layouts, "找不到 `.flag>summary` 的 grid-template-columns"

    for value in layouts:
        tracks = _tracks(value)
        covered = _covered(span[0], len(tracks))
        flexible = [i for i in covered
                    if 1 <= i <= len(tracks) and "fr" in tracks[i - 1]]
        assert flexible, (
            f"版型 `{value.strip()}` 下,`.frule{{grid-column:{span[0]}}}` "
            f"覆蓋軌道 {covered},其中沒有任何一條是可伸縮的(`fr`)。\n"
            f"軌道內容:{tracks}\n"
            "固定寬度的窄軌放不下門檻文字 —— 它會擠成一行一個字,"
            "而這在離線測試裡沒有其他症狀。")
