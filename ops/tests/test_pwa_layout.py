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


def _token(css: str, name: str) -> str:
    """`:root` 裡某個設計 token 的值(`#rrggbb` 或 `rgba(...)`)。

    ⚠ 2026-10-26 擴充成也認得 `rgba()`。原本只認 `#rrggbb`,所以把共用的
    軌道底色收成 `--track:rgba(...)` 這個 token 之後,下面的對比度守衛
    就**讀不到顏色了** —— 而讀不到的守衛會直接 error,不是靜靜通過,
    所以這次是當場發現的。收 token 與「守衛讀得到」兩件事要一起做。
    """
    m = re.search(re.escape(name) + r":\s*(#[0-9a-fA-F]{3,8}|rgba?\([^)]*\))", css)
    assert m, f"找不到 token {name}"
    return m.group(1)


def _resolve(css: str, value: str) -> str:
    """把 `var(--x)` 換成它的值。只展開一層以外再遞迴,找不到就原樣丟回去
    讓 `_rgba()` 去報「看不懂的顏色」—— 不要在這裡悄悄給一個預設色,
    那會讓守衛量到一個畫面上不存在的顏色。"""
    v = value.strip()
    for _ in range(4):
        m = re.fullmatch(r"var\(\s*(--[\w-]+)\s*\)", v)
        if not m:
            return v
        v = _token(css, m.group(1)).strip()
    return v


def _rgba(value: str) -> tuple[float, float, float, float]:
    """`#rrggbb` 或 `rgba(r,g,b,a)` -> (r, g, b, alpha)。"""
    v = value.strip()
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", v)
    if m:
        h = m.group(1)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    m = re.fullmatch(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*"
                     r"(?:,\s*([\d.]+)\s*)?\)", v)
    assert m, f"看不懂的顏色:{value!r}(這條測試只認得 #rrggbb 與 rgba())"
    a = float(m.group(4)) if m.group(4) is not None else 1.0
    return (float(m.group(1)), float(m.group(2)), float(m.group(3)), a)


def _over(css: str, fg: str, bg: tuple[float, float, float]) -> tuple[float, float, float]:
    """把 fg 疊在不透明的 bg 上,得到實際看到的顏色。fg 可以是 `var(--x)`。"""
    r, g, b, a = _rgba(_resolve(css, fg))
    return tuple(a * c + (1 - a) * d for c, d in zip((r, g, b), bg))


def _luminance(rgb: tuple[float, float, float]) -> float:
    """WCAG 相對亮度。"""
    def lin(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


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
    右邊空一塊,邊緣參差。

    判準跟著實際的格子數走,不是寫死一個結論 —— 以後加第四張 stat,
    四張配兩欄除得盡,就不再要求跨欄。

    ## 這條測試的狀態,誠實寫在這裡

    `實測`(2026-10-26 的版面):宣告的欄數是 **3**(≥561px)與 **1**(窄螢幕),
    而 stat 有 3 張 —— 兩個都除得盡,所以**今天它不會對任何東西開火**。
    它守的是「哪天有人把欄數改成 2 卻忘了跨欄規則」。

    這跟「恆真」不一樣,差別在**前提會不會自己消失**:

    | | |
    |---|---|
    | 恆真 | 判準讀不到東西,改什麼都不會紅(例如上一版 `3 % 1 == 0` 直接 continue,而那是**唯一**一條 strip 守衛) |
    | 條件未觸發 | 判準讀得到真實資料,只是目前的值剛好合格 |

    上面那個 `assert n_stats >= 2` 就是用來分辨這兩者的:**如果 stat 數讀不到,
    它會紅,而不是安靜地通過。**

    ⚠ 2026-10-26 這條被我換成過 `test_the_stat_strip_stays_one_column`
    (當時版面是全頁單欄)。版面改回「桌面三等分」之後那條會紅 ——
    紅得對,因為它釘的宣稱已經不是現在的宣稱了。換回來,不是放寬。
    """
    html = INDEX.read_text(encoding="utf-8")
    n_stats = len(re.findall(r'<div class="stat"', html))
    assert n_stats >= 2, f"只找到 {n_stats} 張 stat —— 這條測試沒在量東西"

    values = _decl(css, ".strip", "grid-template-columns")
    assert values, "找不到 `.strip` 的 grid-template-columns —— 這條沒在量東西"

    span = _decl(css, ".strip .stat:last-child", "grid-column")

    for value in values:
        cols = len(_tracks(value))
        if n_stats % cols == 0:
            continue
        assert span, (
            f"{n_stats} 張 stat 配 {cols} 欄會剩下 {n_stats % cols} 張孤兒,"
            "而 `.strip .stat:last-child` 沒有宣告 grid-column。"
            "最後一張會獨佔一格、右邊空一塊。")
        covered = _covered(span[0], cols)
        assert len(covered) == cols, (
            f"`.strip .stat:last-child{{grid-column:{span[0]}}}` 只跨了 "
            f"{len(covered)} 欄,但要填滿一整列需要 {cols} 欄")


def test_the_three_stats_are_laid_out_identically(css: str) -> None:
    """三個即時指標必須共用同一套規則,不得有只給其中一格的特例。

    規格要求「等寬、等高、標籤位置一致、數字對齊」。等寬等高由
    `repeat(3, minmax(0,1fr))` 的 grid 保證;**真正會悄悄走鐘的是
    「只改其中一格」** —— `實測`(2026-10-26 改之前):第三格的值帶著
    `style="font-size:13px"`,另外兩格沒有,所以同一列三個數字兩種字級。

    所以這條去量兩件事:
      1. `.strip` 裡的三個 `.stat` 不得有 inline `style`
      2. 不得有 `:nth-child` / `:last-child` 之類只選到其中一格的字級規則
    """
    html = INDEX.read_text(encoding="utf-8")
    block = re.search(r'<div class="strip">(.*?)\n</div>', html, re.S)
    assert block, "找不到 `.strip` 的標記"
    inner = block.group(1)

    n = len(re.findall(r'<div class="stat"', inner))
    assert n == 3, f"`.strip` 裡有 {n} 個 .stat,預期 3 個 —— 這條沒在量東西"

    inline = re.findall(r'<div class="(?:stat|k|x|y)"[^>]*(style="[^"]*")', inner)
    assert not inline, (
        f"即時指標裡還有 inline 樣式:{inline}。"
        "一格有、另外兩格沒有,就是同一列出現兩種規格。"
        "要改就改 `.stat` / `.stat .x`,三格一起改。")

    picks = re.findall(r"\.stat[^{]*:(?:nth-child|last-child|first-child)[^{]*\{",
                       css)
    assert not picks, (
        f"有只選到其中一格的 `.stat` 規則:{picks}。"
        "三個指標要長得一樣,個別挑一格改就是在製造不一致。")


# ==========================================================================
# 3. 刻度字必須站在它所標示的轉折點上(2026-10-26 新增)
# ==========================================================================

def test_the_scale_marks_the_thresholds_the_code_actually_uses(css: str) -> None:
    """色條上的刻度,必須等於 `renderTemp()` 裡真正的分級門檻。

    `實測`(2026-10-26,改之前)程式裡的分級是

        uni<0.42 -> 警戒 ·  uni<0.90 -> 高風險 ·  否則 極端

    而色條上寫的是「警戒 **42%**、高風險 **70%**、極端 **100%**」:

    | 刻度字 | 標在 | 真相 |
    |---|---|---|
    | 警戒 | 42% | 那是警戒**結束**的地方 |
    | 高風險 | 70% | 不是任何門檻,是區間中點 |
    | 極端 | 100% | 它其實從 **90%** 就開始 |

    三個都錯。而前一版的守衛要求「刻度字對齊**漸層轉折**」—— 漸層轉折
    本身就不是門檻,所以那條測試只是把錯誤對得更整齊。
    **判準對準了一個中間物,不是對準那個宣稱。**

    現在門檻只寫一次(`.tempwrap` 的 `--t1` / `--t2`),刻度線 `.ttick`
    與刻度字 `.tempscale span` 都讀同一個變數 —— 兩者**不可能再分岔**,
    所以這條測試只需要問一件事:**那兩個變數等不等於程式裡的門檻。**

    (把分岔的可能性消掉,不等於讓守衛閉嘴 —— 剩下的那個耦合仍然被檢查,
    而且是唯一還能錯的地方。)
    """
    html = INDEX.read_text(encoding="utf-8")

    # 1) 程式裡的門檻
    fn = re.search(r"function renderTemp\([^)]*\)\{(.*?)\n\}", html, re.S)
    assert fn, "找不到 renderTemp() —— 前提變了,請重寫這條"
    thresholds = [float(v) for v in re.findall(r"uni<([\d.]+)\?", fn.group(1))]
    assert len(thresholds) >= 2, (
        f"從 renderTemp() 只解析到 {thresholds} —— 這條測試沒在量東西")

    # 2) CSS 裡的門檻(單一來源)
    declared = re.findall(r"--t(\d)\s*:\s*(\d+)%", css)
    assert len(declared) == len(thresholds), (
        f"程式有 {len(thresholds)} 個門檻,CSS 宣告了 {len(declared)} 個")
    css_pct = [int(v) for _, v in sorted(declared)]
    want_pct = [round(x * 100) for x in thresholds]
    assert css_pct == want_pct, (
        f"色條的刻度在 {css_pct}%,但 renderTemp() 的分級門檻是 {want_pct}%。\n"
        "刻度標在不是門檻的地方,畫面上看起來完全正常 —— 只有照著它讀的人會讀錯。")

    # 3) 刻度線與刻度字必須讀同一個變數,不得各自寫死
    for sel in (r"\.ttick\.t1\{left:var\(--t1\)\}",
                r"\.ttick\.t2\{left:var\(--t2\)\}",
                r"\.tempscale span:nth-child\(1\)\{left:var\(--t1\)\}",
                r"\.tempscale span:nth-child\(2\)\{left:var\(--t2\)\}"):
        assert re.search(sel, css), (
            f"找不到 `{sel}` —— 刻度線與刻度字必須讀同一個 --t 變數。\n"
            "各自寫死百分比的話,改了一邊忘了另一邊不會有任何症狀。")

    # 4) 刻度【字】本身就是那個門檻數字
    block = re.search(r'<div class="tempscale">(.*?)</div>', html, re.S)
    assert block, "找不到 `.tempscale` 的標記"
    labels = re.findall(r"<span>([^<]*)</span>", block.group(1))
    assert len(labels) == len(thresholds), (
        f"刻度字有 {len(labels)} 個,門檻有 {len(thresholds)} 個。\n"
        "刻度上【只】放門檻 —— 端點 0 與 100 不帶門檻資訊,是裝飾,已拿掉。")
    assert [int(x) for x in labels] == want_pct, (
        f"刻度字寫的是 {labels},但門檻是 {want_pct} —— 位置對了,字卻是別的數。")


# ==========================================================================
# 4. 量尺的刻度線不得與量尺底色同色(2026-10-26 新增)
# ==========================================================================

def test_the_scale_ticks_are_visible_against_the_empty_track(css: str) -> None:
    """刻度線與軌道的對比,不得低於**軌道與它所在表面**的對比。

    `實測`(2026-10-25,當時量的是那支已經刪掉的直立量尺):兩邊的
    background 都是 `#0c1322`。後果是刻度**只有被填滿的那一段看得見** ——
    水位 24% 時整支量尺就是一個黑盒子。它從來沒有「壞掉」過,
    **它只是從來沒有出現過**:沒有 console 錯誤、沒有版面位移、沒有測試會紅。

    ⚠ 第一版的判準是「兩個字串不相等」。那擋得住 `#0c1322` vs `#0c1322`,
    擋不住 `rgba(232,238,247,.056)` vs `rgba(232,238,247,.055)` ——
    差 0.001、肉眼完全沒有線,而字串不相等。**判準對準的是字串,不是
    「看得見」**。

    ⚠ 2026-10-26 量尺被刪掉(它與色條畫的是同一個數字)。這條守衛**沒有
    跟著刪**,而是搬到活下來的那把尺上 —— `.ttick` 對 `.tempbar` 的軌道。
    **宣稱沒有變:刻度要看得見。** 被刪掉的是其中一個對象,不是那個宣稱。

    門檻刻意**不寫死數字**,而是拿軌道自己當尺:

        contrast(刻度線, 軌道)  >=  contrast(軌道, 它所在的表面)

    白話是「刻度至少要和軌道本身一樣看得出來」。改了調色盤不必回來調這條。

    這條**不管刻度是比軌道亮還是比軌道暗** —— 深色的線一樣看得見。
    亮度方向不是宣稱的一部分,對比才是。
    """
    track_decl = _decl(css, ".tempbar", "background")
    assert track_decl, "找不到 `.tempbar` 的 background"
    tick_decl = _decl(css, ".ttick", "background")
    assert tick_decl, "找不到 `.ttick` 的 background"

    # 色條住在 hero 裡,所以它的底是 --surf2(不是頁面底色)。
    surf = _rgba(_resolve(css, _token(css, "--surf2")))[:3]

    track = _over(css, track_decl[0], surf)
    tick = _over(css, tick_decl[0], track)          # 刻度畫在軌道【上面】

    tick_vs_track = _contrast(tick, track)
    track_vs_surf = _contrast(track, surf)

    assert tick_vs_track >= track_vs_surf, (
        f"刻度線與軌道的對比只有 {tick_vs_track:.2f}:1,"
        f"而軌道與它所在的表面是 {track_vs_surf:.2f}:1 —— "
        "刻度比軌道本身還難看出來。\n"
        f"  軌道 `{track_decl[0].strip()}` 疊在 --surf2 上 -> rgb{tuple(round(c) for c in track)}\n"
        f"  刻度 `{tick_decl[0].strip()}` 疊在軌道上 -> rgb{tuple(round(c) for c in tick)}\n"
        "空白區看不到刻度不會有任何症狀:沒有錯誤、沒有位移、沒有測試會紅。"
        "而水位低(回測全期 59.3% 的月份現金是 0%)正是最常見的狀態。")


def _ramp_stops(html: str) -> list[tuple[float, tuple[int, int, int]]]:
    """從 `riskColor()` 的原始碼解析色階。

    不寫死一份副本 —— 副本會在改色盤的那天安靜地變成「測試在量兩個月前的設計」。
    """
    m = re.search(r"const stops=\[(.*?)\];", html)
    assert m, "找不到 riskColor() 的 stops —— 前提變了,請重寫這兩條"
    out = []
    for pos, r, g, b in re.findall(
            r"\[\s*([\d.]+)\s*,\s*\[\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\]\s*\]",
            m.group(1)):
        out.append((float(pos), (int(r), int(g), int(b))))
    assert len(out) >= 2, f"只解析到 {len(out)} 個 stop —— 這條測試沒在量東西"
    return out


def _ramp(stops, c: float) -> tuple[int, int, int]:
    """重現 riskColor() 的線性內插。"""
    for i in range(len(stops) - 1):
        a, ca = stops[i]
        b, cb = stops[i + 1]
        if c <= b:
            k = (c - a) / ((b - a) or 1)
            return tuple(round(x + (y - x) * k) for x, y in zip(ca, cb))
    return stops[-1][1]


def _union_ladder() -> list[float]:
    """聯集現金水位【真的會出現】的那幾個值,從引擎推出來,不寫死。"""
    import engine

    vals = set()
    for i in range(-4000, 4001):
        vals.add(engine.mom_cash(i / 10000))
        vals.add(engine.vix_cash(i / 1000))
    out = sorted(v for v in vals if v == v)        # 去掉 NaN
    assert len(out) >= 5, f"只推出 {len(out)} 個階梯值 —— 這條測試沒在量東西"
    return out


def test_the_colour_ramp_matches_the_css_tokens(css: str) -> None:
    """`riskColor()` 的四個 stop,必須逐位元等於 `:root` 的四個 token。

    `實測`(2026-10-26):兩邊目前相同,**而且沒有任何東西在守它**。
    同一個「安全色」在這個檔案裡存了兩份 —— 一份給 CSS(文字、膠囊、長條),
    一份給 JS(大數字、水位條、歷史圖)。

    改了一邊忘了另一邊,症狀是**畫面上同時出現兩種「同一個」顏色**,
    而且不會有任何錯誤 —— 這是本 repo 反覆記錄的那個形狀:
    兩端對同一份東西各有一個定義,而沒有人對帳。
    """
    html = INDEX.read_text(encoding="utf-8")
    stops = _ramp_stops(html)
    names = ["--teal", "--amber", "--orange", "--rose"]
    assert len(stops) == len(names), (
        f"色階有 {len(stops)} 個 stop,但語意色只有 {len(names)} 個 —— "
        "兩邊的數量都變了的話,這條測試要重寫,不是放寬")

    wrong = []
    for (pos, rgb), name in zip(stops, names):
        want = _rgba(_token(css, name))[:3]
        if tuple(rgb) != tuple(round(v) for v in want):
            wrong.append(f"{pos:.2f} 的 {rgb} vs {name} 的 "
                         f"{tuple(round(v) for v in want)}")
    assert not wrong, (
        "riskColor() 的 stop 與 CSS token 不一致:\n  " + "\n  ".join(wrong) + "\n\n"
        "同一個顏色存了兩份,改一邊忘了另一邊 —— 畫面上會同時出現兩種"
        "「同一個」顏色,而且沒有任何錯誤訊息。兩份都要改。")


def test_the_colour_ramp_only_gets_darker(css: str) -> None:
    """水位越高,顏色必須越深(相對亮度單調不升)。

    ⚠ 這條是 2026-10-26 使用者講出來的規則:「數字小淺、數字大深」。
    量過之後發現它**當時不成立**:

    | 水位 | 顏色 | 相對亮度 |
    |---|---|---|
    | 0% | `#2DD4BF` | 0.514 |
    | 24% | amber | **0.579** ← 比 0% 還淺 |

    也就是整條色階在最底下那一段是反的 —— 而那一段正是最常出現的狀態
    (回測全期 59.3% 的月份現金是 0%)。
    修法是把 0% 的 teal 提亮到 `#5EEAD4`(0.660),不是改 amber ——
    amber 一壓暗,24% 與 30% 的層次就糊了。

    ## 判準釘在哪幾個值上

    只釘**聯集真的會輸出的那幾個階梯值**(由 `engine` 推出來,不寫死)。
    不做連續掃描,理由是量過的:`實測` 密集掃描 0..1 會出現 177 個
    「變淺」的點,但最大一次只有 **0.0018**,而相鄰階梯值之間最小的落差是
    **0.0255** —— 差 14 倍。那是 `round()` 的 ±1 抖動,畫面上不存在,
    因為畫面上只會出現那八個值。

    **把抖動也釘住會讓這條測試在守一個不存在的東西。**
    """
    html = INDEX.read_text(encoding="utf-8")
    stops = _ramp_stops(html)
    ladder = _union_ladder()

    rows = [(v, _ramp(stops, v), _luminance(_ramp(stops, v))) for v in ladder]
    bad = []
    for (v0, c0, l0), (v1, c1, l1) in zip(rows, rows[1:]):
        if l1 > l0:
            bad.append(f"{v0*100:.0f}% {c0} 亮度 {l0:.3f}  ->  "
                       f"{v1*100:.0f}% {c1} 亮度 {l1:.3f}  (變淺了)")

    assert not bad, (
        "色階不是一路變深:\n  " + "\n  ".join(bad) + "\n\n"
        "規則是「水位越高顏色越深」。顏色在這張圖上是第二條軸 —— "
        "它反過來的那一段,讀的人會以為風險比較低。")


# ==========================================================================
# 5. 中文不得被設成等寬字(2026-10-26 新增)
# ==========================================================================

CJK = re.compile(r"[\u4e00-\u9fff]")


def _element_text(html: str, start: int, raw: bool = False) -> str:
    """從某個開始標籤的位置,取出它整棵子樹的文字 —— **數標籤**,不要用正則。

    (與 `_media_block` 同一個理由:巢狀的東西正則數不了。`.templab` 的中文
    在子層的 `<span>` 裡,只看直接文字會漏掉 —— 而它正是出過問題的那一個。)
    """
    tag = re.match(r"<(\w+)", html[start:]).group(1)
    i = html.index(">", start) + 1
    depth, out = 1, []
    opener = re.compile(r"<(/?)" + tag + r"[\s>/]", re.I)
    while depth and i < len(html):
        m = opener.search(html, i)
        if not m:
            break
        out.append(html[i:m.start()])
        i = html.index(">", m.start()) + 1
        depth += -1 if m.group(1) else 1
    inner = "".join(out)
    return inner if raw else re.sub(r"<[^>]*>", " ", inner)


def _elements_with(html: str, token: str, kind: str, raw: bool = False) -> list[str]:
    """帶有某個 class 或 id 的元素,各自的子樹文字(`raw=True` 回原始 HTML)。"""
    attr = "class" if kind == "." else "id"
    pat = re.compile(r'<\w+[^>]*\b' + attr + r'="[^"]*(?<![\w-])'
                     + re.escape(token) + r'(?![\w-])[^"]*"')
    return [_element_text(html, m.start(), raw=raw) for m in pat.finditer(html)]


def _elements_for(html: str, selector: str) -> list[str]:
    """一條(單一)選擇器真正選到的那些元素的文字。

    ⚠ 要看的是**最後一段**。第一版只抓選擇器裡最後一個 `.class`/`#id`,
    於是 `.warnhead b` 被解讀成 `.warnhead`,量到的是整個
    `外部早期警訊 2 / 6`,而真正被選到的 `<b>` 裡只有 `2 / 6`(數字,
    本來就該是等寬)—— **偽陽性,量錯了對象**。`.fine code` 同理。

    所以:最後一段若是純標籤,就先定位它的祖先,再在祖先的子樹裡找該標籤。
    """
    segs = [s for s in re.split(r"[\s>+~]+", selector.strip()) if s]
    if not segs:
        return []
    last = segs[-1]
    m = re.findall(r"([.#])([\w-]+)", last)
    if m:
        kind, token = m[-1]
        return _elements_with(html, token, kind)

    # 最後一段是標籤(b / code / summary …):在祖先的子樹裡找它
    tag = re.match(r"[\w-]+", last)
    if not tag:
        return []
    anc = re.findall(r"([.#])([\w-]+)", " ".join(segs[:-1]))
    scopes = ([html] if not anc
              else _elements_with(html, anc[-1][1], anc[-1][0], raw=True))
    out = []
    for scope in scopes:
        for mm in re.finditer(r"<" + tag.group(0) + r"[\s>]", scope):
            out.append(_element_text(scope, mm.start()))
    return out


def test_no_chinese_label_is_set_in_the_monospace_face(css: str) -> None:
    """`--mono` 沒有中文字 —— 把中文設成等寬,等於一個標籤裡兩套字體。

    `實測`(2026-10-26,改之前)六個標籤踩到:`.readlab`(建議現金比重)、
    `.templab`(風險溫度)、`.ddlab`(目前距高點)、`.warnhead`(外部早期警訊)、
    `.card .t`(趨勢動能腿)、`.stat .k`(VIX 現值)。中文掉到系統字體、
    拉丁與數字留在等寬,兩邊字重與基線都不一樣 —— 而且還套了
    **只對拉丁大寫有意義的字距**(最多 1.98px)。中文沒有字距這個傳統。

    那就是「看起來像機器排的」的來源,而它不會讓任何東西變紅。

    判準不是白名單,是**真的去看那條規則選到的元素裡有沒有中文**。
    所以新增一個裝中文的等寬規則,這條會紅;而
    `.eyebrow`(純拉丁)、`.big` / `.ddbig`(數字)、`code`(代碼)不受影響。

    ## 這一條【測不到】什麼 —— 兩個已證實的洞

    **它只看得到 HTML 裡寫死的字。** 兩個實測過的例子(突變 M194 / M195
    都因此 survive,那是預期的,不是判準弱):

    | 元素 | 為什麼看不到 |
    |---|---|
    | `#sfresh` 的值(剛剛 / 3 小時前) | 由 `render()` 寫入,靜態檔裡是 `··` |
    | `.flag .fbody code` 的門檻原文 | `實測`:靜態 body 裡 `class="fbody"` 出現 **0 次**,`#flaglist` 是空的;文字來自 `signals.json` 的 `f.rule`,是**外部資料** |

    第二個尤其值得記:那段中文**根本不在這個檔案裡**,所以任何靜態分析都看不到。
    要抓它只能在真的瀏覽器裡量字體 —— 而 L2 必須完全離線,
    jsdom 也不做 `var()` 的串接解析。**所以這個洞是知道的、量過的、刻意留著的。**
    留在這裡是為了讓下一個人不必重新發現它。
    """
    html = INDEX.read_text(encoding="utf-8")
    body = html[html.index("</style>"):]

    rules = re.findall(r"([^{}]+)\{([^}]*var\(--mono\)[^}]*)\}", css)
    assert len(rules) >= 6, f"只找到 {len(rules)} 條等寬規則 —— 掃描可能壞了"

    bad = []
    for selector, body_decl in rules:
        for part in selector.split(","):
            for text in _elements_for(body, part):
                hit = CJK.findall(text)
                if hit:
                    sample = re.sub(r"\s+", " ", text).strip()[:24]
                    bad.append(f"`{part.strip()}` 選到的元素含中文:「{sample}」")
                    break

    assert not bad, (
        "下列規則把中文設成了等寬字:\n  " + "\n  ".join(sorted(set(bad))) + "\n\n"
        "`--mono` 沒有中文字,中文會掉到系統字體 —— 同一個標籤裡兩套字體、"
        "兩種字重與基線。等寬字留給數字與代碼,文字用 `--sans`。\n"
        "順帶:`letter-spacing` 是拉丁全大寫標題的語彙,中文沒有這個傳統,"
        "套上去只會讓字散掉。")


# ==========================================================================
# 6. 門檻文字必須落在有寬度的軌道上(突變 M171)
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
