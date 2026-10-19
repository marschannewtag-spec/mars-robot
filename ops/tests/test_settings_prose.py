"""設定區寫給使用者的那段話,和 `fetchSP()` 真的跑的那條鏈。

## 為什麼這個檔案要存在

2026-10-19 把「本專案鏡像」插進退回鏈的第三層之後,`fetchSP()` 從四層變成
五層,而 `tests/test_source_chain.py` 在 jsdom 裡把五層逐一驗過了。

但**設定區那段話沒有跟著改**。使用者唯一會去讀、用來決定「我需不需要
自己架 Worker 或去申請一把 API key」的地方,當時還寫著:

    ③ 每日 · Yahoo ^GSPC(**預設、免金鑰**)… ③ 目前實質上不會成功,
    會直接退到 ④。**要恢復每日新鮮度請填 ① 或 ②。**

三句話,三個都是反的:預設路徑不是代理而是鏡像;預設路徑會成功;
每日新鮮度不需要填任何東西。同一頁的「資料源」段落早就寫著
「免金鑰、免代理,反映到昨收」—— 所以那一頁自己跟自己矛盾,
而且是往「叫使用者去做工作」的方向矛盾。

這正是 CLAUDE.md 那條結論的反面:
**「代理這條路已經沒有未來,而正解一直在手上」**——
正解在手上了,畫面卻還在叫人去部署 Cloudflare Worker。

## 這些測試驗的是什麼

不是文案好不好,是**文案與程式碼有沒有分家**:

| 驗 | 怎麼驗 |
|---|---|
| 層數一致 | `fetchSP()` 裡的 `src:"…"` 標籤數 == 設定區列出的項目數 |
| 順序一致 | 文案裡的出現順序 == 程式碼嘗試的順序 |
| 「預設」指對人 | 標著「預設」的那一項,必須是什麼都不設定時真的會跑的那一層 |
| 編號引用沒錯位 | 後面那段註解提到「跨網域」時,引用的編號要是代理那一層 |
| 代理名稱一致 | `PROXIES` 裡的每個代理名都要出現在設定區 |

**順序與層數必須從程式碼抽出來比,不能在測試裡抄一份。** 抄一份的話
插入新層時測試和文案會一起沉默 —— 那就是這次漏掉的那個形狀。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# 程式碼裡的 `src:"…"` 標籤  ->  設定區那一項必須出現的字樣。
#
# 為什麼要這張表:程式碼的標籤是給「資料更新」欄顯示的短字串
# (`每日 · 自有Worker`),文案是給人讀的句子(`自有 Worker`),兩邊
# 本來就不會逐字相同。表的存在讓**新增一層時測試一定紅**——
# 下面第一條會斷言表的鍵集合與程式碼抽出來的集合完全相同。
TIER_ANCHOR = {
    "每日 · 自有Worker": "自有 Worker",
    "即時 · SPY/TwelveData": "TwelveData",
    "每日 · 本專案鏡像": "本專案鏡像",
    "每日 · Yahoo^GSPC(免金鑰)": "CORS 代理",
    "月頻 · Shiller(退回)": "Shiller",
}

# 什麼都不設定時真的會跑的那一層,由
# test_source_chain.py::test_with_nothing_configured_the_mirror_serves 實測。
DEFAULT_TIER = "每日 · 本專案鏡像"

_MARKERS = "①②③④⑤⑥⑦⑧⑨⑩"


@pytest.fixture(scope="module")
def html(index_html: Path) -> str:
    return index_html.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def code_tiers(html: str) -> list[str]:
    """`fetchSP()` 裡的 `src:"…"`,照程式碼順序。

    ⚠ 先把註解整行拿掉 —— 這個 repo 已經三次掃到自己寫的解釋性註解。
    """
    i = html.index("async function fetchSP(")
    j = html.index("\nfunction compute(", i)
    body = "\n".join(l for l in html[i:j].splitlines()
                     if not l.lstrip().startswith("//"))
    return re.findall(r'src:"([^"]+)"', body)


@pytest.fixture(scope="module")
def settings_block(html: str) -> str:
    """設定區那個 `<details>` 的內文。"""
    i = html.index("趨勢腿資料來源")
    j = html.index("</details>", i)
    return html[i:j]


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", fragment)).strip()


def _item_of(tier_items: list[str], tier: str) -> int:
    """`tier` 在設定區清單裡的位置。

    刻意不用 `next(...)` 的裸版本 —— 找不到時那會拋 `StopIteration`,
    報告上只看得到一行例外名稱,看不出少了哪一層。
    """
    anchor = TIER_ANCHOR[tier]
    hits = [n for n, item in enumerate(tier_items) if anchor in item]
    assert len(hits) == 1, (
        f"「{tier}」(找字樣 {anchor!r})在設定區清單裡命中 {len(hits)} 項,預期 1 項:\n  "
        + "\n  ".join(i[:80] for i in tier_items)
    )
    return hits[0]


@pytest.fixture(scope="module")
def tier_items(settings_block: str) -> list[str]:
    """設定區那段清單,一層一項(`<br>` 分隔)。"""
    paras = re.findall(r"<p\b[^>]*>(.*?)</p>", settings_block, re.S)
    # 以「①」【開頭】的那一段才是清單本身。只用「含有①」會多抓到後面那段
    # 註解(它用「填①或②」回指清單),而那一段沒有 `<br>` 分隔的項目結構。
    listed = [p for p in paras if p.lstrip().startswith(_MARKERS[0])]
    assert len(listed) == 1, (
        f"設定區裡以「{_MARKERS[0]}」開頭的段落有 {len(listed)} 個,預期恰好 1 個 —— "
        "清單被拆開或改寫了,下面的比對會失去意義"
    )
    return [_text(x) for x in re.split(r"<br\s*/?>", listed[0])]


# --------------------------------------------------------------------------
# 掃描式測試先證明兩邊都掃得到東西
# --------------------------------------------------------------------------

def test_both_sides_of_the_comparison_were_actually_found(
    code_tiers: list[str], tier_items: list[str]
) -> None:
    """兩邊任一邊掃成空的,底下每一條都會變成恆真。"""
    assert len(code_tiers) >= 4, f"從 fetchSP() 只抽到 {len(code_tiers)} 層:{code_tiers}"
    assert len(tier_items) >= 4, f"設定區只解析出 {len(tier_items)} 項:{tier_items}"
    for n, item in enumerate(tier_items):
        assert item.startswith(_MARKERS[n]), (
            f"第 {n + 1} 項沒有以「{_MARKERS[n]}」開頭:{item[:40]!r} —— "
            "編號不連續的話,後面引用編號的句子會指到錯的那一層"
        )


# --------------------------------------------------------------------------
# 層數、順序、預設
# --------------------------------------------------------------------------

def test_the_settings_panel_lists_every_tier_the_code_has(
    code_tiers: list[str], tier_items: list[str]
) -> None:
    """程式碼有幾層,設定區就要列幾項。

    2026-10-19 漏掉的就是這一條:鏡像插進第三層,文案還是四項。
    """
    assert set(code_tiers) == set(TIER_ANCHOR), (
        "fetchSP() 的層級變了,但這個測試的對照表沒跟著改。\n"
        f"  程式碼:{code_tiers}\n"
        f"  對照表:{sorted(TIER_ANCHOR)}\n"
        "改完對照表之後,記得設定區那段文案也要一起改 —— 那才是使用者讀的那份。"
    )
    assert len(tier_items) == len(code_tiers), (
        f"fetchSP() 有 {len(code_tiers)} 層,設定區列了 {len(tier_items)} 項。\n"
        f"  程式碼:{code_tiers}\n"
        f"  設定區:{[i[:24] for i in tier_items]}\n"
        "少列一層的後果不是「文件不完整」,是使用者按著一份錯的優先順序做決定。"
    )
    for tier, anchor in TIER_ANCHOR.items():
        hits = [n for n, item in enumerate(tier_items) if anchor in item]
        assert len(hits) == 1, (
            f"「{tier}」在設定區裡對應到 {len(hits)} 項(找字樣 {anchor!r}),預期恰好 1 項"
        )


def test_the_settings_panel_lists_them_in_the_order_the_code_tries_them(
    code_tiers: list[str], tier_items: list[str]
) -> None:
    """順序本身就是語意 —— 退回鏈的優先順序完全由程式碼的先後決定。"""
    where = [_item_of(tier_items, t) for t in code_tiers]
    assert where == sorted(where), (
        "設定區列出的順序和 fetchSP() 嘗試的順序不同:\n"
        f"  程式碼順序:{code_tiers}\n"
        f"  在文案裡的位置:{where}"
    )


def test_the_tier_marked_default_is_the_one_that_runs_with_nothing_configured(
    code_tiers: list[str], tier_items: list[str]
) -> None:
    """「預設」兩個字必須標在真的會跑的那一層上。

    標錯的代價是最高的:它直接影響使用者要不要去申請金鑰、架 Worker。
    標在一條已經全死的代理鏈上,等於叫每一個使用者去做本專案自己已經在做的事。
    """
    assert DEFAULT_TIER in code_tiers, (
        f"{DEFAULT_TIER!r} 不在 fetchSP() 的層級裡了:{code_tiers}"
    )
    marked = [n for n, item in enumerate(tier_items) if "預設" in item]
    assert len(marked) == 1, (
        f"設定區有 {len(marked)} 項標著「預設」,預期恰好 1 項:"
        f"{[tier_items[n][:40] for n in marked]}"
    )
    expected = _item_of(tier_items, DEFAULT_TIER)
    assert marked[0] == expected, (
        f"「預設」標在第 {marked[0] + 1} 項,但什麼都不設定時真的會跑的是第 "
        f"{expected + 1} 項({DEFAULT_TIER})。\n"
        f"  標著預設的:{tier_items[marked[0]][:80]}\n"
        f"  真的預設的:{tier_items[expected][:80]}"
    )


# --------------------------------------------------------------------------
# 編號引用 / 代理名稱
# --------------------------------------------------------------------------

def test_the_note_about_cross_origin_fetching_cites_the_right_tier(
    settings_block: str, tier_items: list[str]
) -> None:
    """清單後面那段話用編號回指某一層 —— 插入新層會讓那個編號指錯。

    這是「③為跨網域抓取」留下來的坑:鏡像插進第三層之後,③變成了
    `raw.githubusercontent.com`(同源式讀取、不需要代理),而那句話還在
    說它是跨網域的。編號是一種會隨著清單變動而失效的參照。
    """
    proxy_n = _item_of(tier_items, "每日 · Yahoo^GSPC(免金鑰)")
    clauses = [c for c in re.split(r"[。;;]", _text(settings_block)) if "跨網域" in c]
    assert clauses, "設定區不再有任何一句解釋「跨網域」—— 那段註解被刪了?"
    wrong = [c for c in clauses if _MARKERS[proxy_n] not in c]
    assert not wrong, (
        f"解釋「跨網域」的句子沒有引用代理那一層的編號「{_MARKERS[proxy_n]}」:\n  "
        + "\n  ".join(w[:120] for w in wrong)
    )
    for c in clauses:
        others = [m for m in _MARKERS[:len(tier_items)]
                  if m != _MARKERS[proxy_n] and m in c]
        assert not others, (
            f"解釋「跨網域」的句子同時引用了別層的編號 {others} —— 讀者會搞不清在說哪一層:\n"
            f"  {c[:160]}"
        )


def test_every_proxy_the_code_uses_is_named_in_the_settings_panel(
    html: str, settings_block: str
) -> None:
    """`PROXIES` 換掉時,設定區那個代理名稱不能留在原地。

    `test_proxy_chain.py` 已經釘住 `PROXIES` 與 `config.yml` 一致;
    這一條補的是第三份清單 —— 使用者讀的那份。
    """
    m = re.search(r"const PROXIES\s*=\s*\[(.*?)\];", html, re.S)
    assert m, "index.html 裡找不到 `const PROXIES=[...]`"
    names = re.findall(r'name\s*:\s*"([^"]+)"', m.group(1))
    assert names, "從 PROXIES 解析出 0 個代理名稱 —— 底下的比對會變成恆真"
    missing = [n for n in names if n not in settings_block]
    assert not missing, (
        f"這些代理在程式碼裡會被試,但設定區沒提到:{missing}\n"
        f"  PROXIES:{names}"
    )
