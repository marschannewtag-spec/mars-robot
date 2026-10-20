"""`scripts/md_payload_check.py` 自己的測試(2026-10-20)。

為什麼這支工具需要測試
----------------------
它的工作是在壓縮/搬移 Markdown 時確認「有效載荷」一個都沒掉。
**一個抓不到東西的檢查器,和沒有檢查器在輸出上長得一模一樣** ——
兩者都印「✓ payload 完整」。

而它已經錯過一次:第一版的 `FENCE` 正則沒有錨在行首,而 `POSTMORTEMS.md`
的散文裡有**行中**的 ```bash(在講那條會把文件指令抓出來執行的測試時),
配對因此歪掉,把整段散文當成程式碼區塊,報出四個根本不存在的「遺失」。

⚠ 這裡刻意**不**把它接成 CI 閘門(「payload 不准變少」)。那會擋掉正當的
刪除 —— 2026-10-20 就刪掉了一段過時的 `.pine` tooltip 說明,那是對的。
它是**搬運時用的工具**,不是持續性的不變量。CI 要守的是「這個工具本身
還有效」,不是「文件永遠只能長不能短」。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import OPS_ROOT, REPO_ROOT

SCRIPT = OPS_ROOT / "scripts" / "md_payload_check.py"

SAMPLE = """\
# 標題

散文一段,裡面有 `inline_code` 和一個路徑 `src/engine.py`,還有數字 42。

## 第二節

```python
x = 1  # 這是程式碼區塊
```

講到 ```bash 區塊時會在句子中間出現圍欄,這一行是**散文**不是程式碼。

| 欄 | 值 |
|---|---|
| a | 1.5 |

```sh
echo second_block
```

連結 https://example.com/thing 。
"""


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPT), *args],
        cwd=OPS_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )


@pytest.fixture()
def pair(tmp_path: Path) -> tuple[Path, Path]:
    md = tmp_path / "doc.md"
    md.write_text(SAMPLE, encoding="utf-8")
    snap = tmp_path / "snap.json"
    r = _run("snapshot", str(md), str(snap))
    assert r.returncode == 0, r.stdout + r.stderr
    return md, snap


# --------------------------------------------------------------------------
# 控制組:沒動就不准叫
# --------------------------------------------------------------------------

def test_an_untouched_file_passes(pair) -> None:
    md, snap = pair
    r = _run("verify", str(md), str(snap))
    assert r.returncode == 0, r.stdout
    assert "payload 完整" in r.stdout


def test_pure_prose_compression_passes(pair) -> None:
    """只縮散文、payload 全留 → 必須通過。

    少了這一條,「什麼都報遺失」會是讓下面幾條全綠的最省事寫法,
    而那會讓這個工具在真正要用的時候完全不能用。
    """
    md, snap = pair
    t = md.read_text(encoding="utf-8")
    t = t.replace(
        "散文一段,裡面有 `inline_code` 和一個路徑 `src/engine.py`,還有數字 42。",
        "散文縮短:`inline_code`、`src/engine.py`、42。")
    t = t.replace(
        "講到 ```bash 區塊時會在句子中間出現圍欄,這一行是**散文**不是程式碼。",
        "講到 ```bash 時圍欄在句中。")
    md.write_text(t, encoding="utf-8")
    r = _run("verify", str(md), str(snap))
    assert r.returncode == 0, f"正當的散文壓縮被誤報:\n{r.stdout}"


# --------------------------------------------------------------------------
# 它真的抓得到東西嗎 —— 每一類各一條
# --------------------------------------------------------------------------

@pytest.mark.parametrize("drop,kind", [
    ("x = 1  # 這是程式碼區塊", "fences"),
    ("`inline_code`", "inline"),
    ("`src/engine.py`", "paths"),
    ("https://example.com/thing", "urls"),
    ("1.5", "numbers"),
    ("## 第二節", "headings"),
])
def test_it_catches_a_planted_loss(pair, drop: str, kind: str) -> None:
    """把一項 payload 拿掉 → 必須紅,而且要指名是哪一類。

    這是本 repo 的既有慣例:掃描器寫完要先餵它一個已知的違規。
    跑出「沒問題」時,先假設是掃描壞了,不是檔案乾淨。
    """
    md, snap = pair
    t = md.read_text(encoding="utf-8")
    assert drop in t, f"前置條件壞了,樣本裡沒有 {drop!r}"
    md.write_text(t.replace(drop, "(拿掉了)"), encoding="utf-8")

    r = _run("verify", str(md), str(snap))
    assert r.returncode == 1, f"拿掉 {drop!r} 卻沒有紅:\n{r.stdout}"
    assert f"[{kind}]" in r.stdout, \
        f"沒有指名是 {kind} 不見了:\n{r.stdout}"


# --------------------------------------------------------------------------
# 那個踩過的坑
# --------------------------------------------------------------------------

def test_a_fence_in_mid_sentence_does_not_break_pairing(tmp_path: Path) -> None:
    """散文裡行中出現 ``` 時,圍欄配對不得歪掉。

    `實測`(2026-10-20):第一版沒錨行首,對著一次純搬移報出四個不存在的
    「遺失」,而那四個其實是散文。判準要對準結構,不是對準字串。
    """
    md = tmp_path / "a.md"
    md.write_text(SAMPLE, encoding="utf-8")
    snap = tmp_path / "s.json"
    _run("snapshot", str(md), str(snap))

    data = json.loads(snap.read_text(encoding="utf-8"))
    assert len(data["fences"]) == 2, (
        f"圍欄數應為 2(python 與 sh 兩個區塊),實際 {len(data['fences'])} ——"
        f" 行中的 ```bash 被當成圍欄了:{data['fences']}")
    bodies = "\n".join(data["fences"])
    assert "x = 1" in bodies and "echo second_block" in bodies
    # 關鍵:散文不得被吞進任何一個區塊裡
    assert not any("這一行是" in f for f in data["fences"]), (
        f"散文被當成程式碼區塊了:{data['fences']}")


def test_splitting_a_file_in_two_loses_nothing(tmp_path: Path) -> None:
    """把一份切成兩份、再合起來驗,必須通過 —— 這就是它被寫出來的用途。"""
    md = tmp_path / "whole.md"
    md.write_text(SAMPLE, encoding="utf-8")
    snap = tmp_path / "s.json"
    _run("snapshot", str(md), str(snap))

    lines = SAMPLE.split("\n")
    cut = lines.index("## 第二節")
    (tmp_path / "a.md").write_text("\n".join(lines[:cut]), encoding="utf-8")
    (tmp_path / "b.md").write_text("\n".join(lines[cut:]), encoding="utf-8")
    combined = tmp_path / "combined.md"
    combined.write_text(
        (tmp_path / "a.md").read_text(encoding="utf-8") + "\n"
        + (tmp_path / "b.md").read_text(encoding="utf-8"),
        encoding="utf-8")

    r = _run("verify", str(combined), str(snap))
    assert r.returncode == 0, f"純切割被報成遺失:\n{r.stdout}"


# --------------------------------------------------------------------------
# 拆檔之後的結構不變量
# --------------------------------------------------------------------------

def test_the_two_documents_still_point_at_each_other() -> None:
    """CLAUDE.md 與 POSTMORTEMS.md 必須互相連結。

    把事故記錄搬出去之後,唯一讓人找得到它的東西就是那個連結。
    斷了的話,2176 行的檔案庫就等於不存在 —— 而症狀是安靜的。
    """
    claude = (OPS_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    pm_path = OPS_ROOT / "POSTMORTEMS.md"
    assert pm_path.exists(), "POSTMORTEMS.md 不見了"
    pm = pm_path.read_text(encoding="utf-8")

    assert "POSTMORTEMS.md" in claude, \
        "CLAUDE.md 沒有連到事故記錄 —— 那 2176 行就沒有人找得到了"
    assert "CLAUDE.md" in pm, \
        "POSTMORTEMS.md 沒有指回工程規格"


def test_the_engineering_spec_stays_small() -> None:
    """CLAUDE.md 不得再被事故記錄塞回去。

    它 2026-10-20 從 2863 行縮到 731 行,靠的是把 append-only 的部分搬走。
    沒有這條,下一次「順手記一下」就會開始把它推回去,而那是漸進的、
    沒有任何一次改動看起來過分。

    ⚠ 上限刻意寬(1200):這不是在禁止成長,是在讓「它又變回垃圾場」
      這件事有人說話。真的需要就調高並在 commit 訊息說明為什麼。
    """
    n = len((OPS_ROOT / "CLAUDE.md").read_text(encoding="utf-8").splitlines())
    assert n <= 1200, (
        f"CLAUDE.md 已經 {n} 行(2026-10-20 拆檔後是 731)。\n"
        "帶日期的事故條目請寫進 ops/POSTMORTEMS.md —— 這一份是工程規格,\n"
        "是改程式之前要讀的東西,被記錄稀釋就沒有人會讀完。")


def test_the_refresh_tool_is_documented_where_it_is_used() -> None:
    """重產 checksum 的方式要寫在 README,不是只寫在測試註解裡。

    那是使用者唯一會翻的檔案,而「黃金基準不會被悄悄換掉」是對外的承諾。
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "refresh_fixture_checksums.py" in readme
    assert "CHECKSUMS.txt" in readme
