"""DOM 契約 —— JS 參照的每一個 id 都必須真的存在於 HTML 裡。

## 為什麼需要這個檔

`index.html` 有 870+ 行,而 **CSS 與 DOM 結構幾乎沒有任何測試**。
既有的測試都在驗「計算對不對」(`test_parity`)或「某幾個特定節點有沒有被碰」
(`test_signals_contract`),沒有一條在問最基本的那件事:

    `$("xxx")` 參照的那個 id,HTML 裡到底有沒有?

把某個 `id` 改名或刪掉,`$()` 會回 `null`,然後 `.textContent = ...` 當場拋
TypeError —— 而 `render()` 一拋,**後面所有讀數都不會被畫出來**,
畫面就停在「讀取資料中…」。這和「抓不到資料」看起來一模一樣,
但原因完全不同,而且重新整理也救不回來。

jsdom 的 `render()` 測試只走得到它自己那條路;`checkOpsHeartbeat()`、
`renderRegime()`、按鈕的 onclick 裡參照的 id 都不在那條路上。
所以這一條是**靜態掃描**:便宜、涵蓋全部,而且改名一定抓得到。

## 這條測試【不能】取代什麼

它只回答「id 存在嗎」。它不知道版面有沒有溢出、CSS 有沒有壞、
手機寬度下會不會被切掉 —— 那些要真的瀏覽器才看得到。
不要因為這條綠了就以為 UI 驗過了。
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def html(index_html: Path) -> str:
    return index_html.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def defined_ids(html: str) -> list[str]:
    """HTML 標籤上宣告的 id(只掃標籤內的 id=,不掃 JS 字串)。"""
    return re.findall(r'<[^>]*\bid\s*=\s*"([^"]+)"', html)


@pytest.fixture(scope="module")
def referenced_ids(html: str) -> set[str]:
    """JS 裡 `$("xxx")` 與 `getElementById("xxx")` 參照到的 id。"""
    out = set(re.findall(r'\$\(\s*"([^"]+)"\s*\)', html))
    out |= set(re.findall(r'getElementById\(\s*"([^"]+)"\s*\)', html))
    return out


def test_the_scan_found_something(defined_ids: list[str], referenced_ids: set[str]) -> None:
    """掃描式測試要先證明它掃得到東西,否則底下兩條可能只是恆真。

    (`force_utf8` 那條掃描就踩過這個 —— 掃描本身也要被驗證。)
    """
    assert len(defined_ids) > 20, f"只掃到 {len(defined_ids)} 個 id,掃描可能壞了"
    assert len(referenced_ids) > 20, f"只掃到 {len(referenced_ids)} 個參照,掃描可能壞了"


def test_every_referenced_id_exists(defined_ids: list[str], referenced_ids: set[str]) -> None:
    missing = sorted(referenced_ids - set(defined_ids))
    assert not missing, (
        f"JS 參照了不存在的 id:{missing}\n"
        "`$()` 會回 null,接著 .textContent/.classList 當場拋 TypeError —— "
        "而 render() 一拋,後面所有讀數都不會被畫出來,畫面停在「讀取資料中…」,"
        "看起來和「抓不到資料」一模一樣。"
    )


def test_no_duplicate_ids(defined_ids: list[str]) -> None:
    """重複 id 時 `getElementById` 只會拿到第一個 —— 另一個永遠更新不到。

    那不會報錯,只會有一塊畫面安靜地不更新。
    """
    dupes = {k: v for k, v in Counter(defined_ids).items() if v > 1}
    assert not dupes, (
        f"HTML 裡有重複的 id:{dupes}\n"
        "getElementById 只回第一個,後面那個永遠不會被更新 —— 無聲失效"
    )


def test_core_readout_ids_are_present(defined_ids: list[str]) -> None:
    """核心讀數的節點是硬需求,不只是「有被參照到」。

    這一條列舉而非掃描:上面兩條只保證「參照與宣告一致」——
    若有人把 `render()` 連同那些 id 一起刪掉,兩邊仍然一致、測試仍然全綠,
    而畫面上再也沒有現金水位。列舉的那幾個就是這個工具存在的理由。
    """
    required = {
        "bigpct",     # 建議現金比重,整個工具的輸出
        "verdict",    # 文字判讀
        "momcash", "vixcash",   # 兩條腿
        "chart",      # 歷史圖
        "defnote",    # 價格定義退回的警示列
        "lagnote",    # 資料落後的警示列
        "opsnote",    # 心跳指示器
        "regimepanel",  # 體制面板
        "err",        # 錯誤列
    }
    missing = sorted(required - set(defined_ids))
    assert not missing, f"核心讀數的節點不見了:{missing}"
