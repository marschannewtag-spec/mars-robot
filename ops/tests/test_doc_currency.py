"""文件不得替舊狀態背書(2026-10-25)。

為什麼需要這一組
----------------
`CLAUDE.md` 自己的「推論」一節寫著:

    改了程式就要回頭看文件有沒有在替舊狀態背書。

而這份文件**自己違反了三次**,全部在 2026-10-25 的盤點中才被發現:

| 宣稱 | 實際 | 錯了多久 |
|---|---|---|
| `health_check.py` 是 227 行 | 1277 行 | 約 2 個月 |
| `sw.js` 只有 56 行 | 100 行 | 約 2 個月 |
| 測試數 442、「只剩一個 skip」 | 537、0 skip | 5 天 |

三次都不是惡意,都是「改了程式、忘了回頭看文件」。靠自律顯然不行 ——
所以改成**約定 + 測試**。

約定
----
- 現況的檔案大小寫成 `` `<repo 相對路徑> 目前 N 行` ``,這一組會逐一核對
- 歷史狀態的句子**必須帶日期**(「2026-08-13 當時 227 行」),不會被核對 ——
  歷史不該被現況推翻
- 當下的測試數 / 覆蓋率**不要寫進散文**,指向量測指令

⚠ 容差是 ±10%,不是精確比對。精確比對會讓每一次無關的小修改都弄紅文件,
而那種測試三個月內一定會被刪掉。**10% 擋得住「227 vs 1277」這種會誤導人的
量級錯誤,擋不住的那幾行也不會害到任何人。**
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import OPS_ROOT, REPO_ROOT

DOCS = ["ops/CLAUDE.md", "ops/POSTMORTEMS.md", "ops/SETUP.md",
        "ops/DEPENDENCIES.md", "README.md"]

# `<路徑> 目前 N 行`
CLAIM = re.compile(r"`([\w./-]+\.\w+)\s+目前\s+([0-9,]+)\s*行`")
TOLERANCE = 0.10


def _docs() -> list[tuple[str, str]]:
    out = []
    for rel in DOCS:
        p = REPO_ROOT / rel
        if p.is_file():
            out.append((rel, p.read_text(encoding="utf-8")))
    return out


def test_the_marker_is_actually_used_somewhere() -> None:
    """前置條件:至少要有一個宣稱,否則下面那條是恆真的。

    一個掃不到東西的掃描器,和沒有掃描器,在輸出上長得一模一樣 ——
    兩者都印「通過」。這是本 repo 的既有慣例,不是多餘的謹慎。
    """
    total = sum(len(CLAIM.findall(text)) for _, text in _docs())
    assert total >= 1, (
        "沒有任何 `<路徑> 目前 N 行` 宣稱 —— 要嘛約定沒人在用、"
        "要嘛正則寫錯了。兩種都該有人看一眼,不該安靜地通過。")


def test_every_current_size_claim_is_still_true() -> None:
    """每一個「目前 N 行」都必須與那個檔案現在的行數相符(±10%)。"""
    wrong = []
    for rel, text in _docs():
        for path_s, n_s in CLAIM.findall(text):
            n = int(n_s.replace(",", ""))
            target = REPO_ROOT / path_s
            if not target.is_file():
                wrong.append(f"{rel}: 宣稱 `{path_s}` 但那個檔案不存在")
                continue
            actual = len(target.read_text(encoding="utf-8").splitlines())
            if abs(actual - n) > max(1, n * TOLERANCE):
                wrong.append(
                    f"{rel}: 宣稱 `{path_s} 目前 {n} 行`,實際 {actual} 行"
                    f"(差 {abs(actual - n)} 行,容差 ±{int(n * TOLERANCE)})")
    assert not wrong, (
        "文件在替舊狀態背書:\n  " + "\n  ".join(wrong) + "\n\n"
        "修法:把數字改成現在的值。若那句話講的是**歷史**,"
        "請改寫成帶日期的句子(「2026-08-13 當時 N 行」)並拿掉這個標記 ——"
        "歷史不該被現況推翻。")


@pytest.mark.parametrize("rel,needle", [
    ("ops/CLAUDE.md", "POSTMORTEMS.md"),
    ("ops/POSTMORTEMS.md", "CLAUDE.md"),
])
def test_the_two_documents_still_point_at_each_other(rel: str, needle: str) -> None:
    """拆檔之後,唯一讓人找得到事故記錄的東西就是那個連結。

    (與 `test_md_payload_check.py` 那一條重複是刻意的:那邊測的是搬運工具
    本身,這邊測的是文件現況。兩個檔案各自被刪掉時,另一個仍然會說話。)
    """
    assert needle in (REPO_ROOT / rel).read_text(encoding="utf-8"), \
        f"{rel} 不再連到 {needle}"


def test_the_doc_does_not_claim_a_test_count() -> None:
    """當下的測試數不得寫死在散文裡 —— 它保證會過期。

    「目前有 442 條測試」在五天內就變成假話。而**過期的數字比沒有數字更糟:
    它看起來像事實**,讀的人不會去查。歷史progression(129 → 140 → …)
    可以留,它本來就標著是歷史。
    """
    text = (OPS_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    bad = re.findall(r"(?:目前|現在|共)\s*\*{0,2}[0-9]{3,4}\*{0,2}\s*條測試", text)
    assert not bad, (
        f"CLAUDE.md 寫死了當下的測試數:{bad}\n"
        "改成指向量測指令。歷史 progression 要標清楚是歷史。")
