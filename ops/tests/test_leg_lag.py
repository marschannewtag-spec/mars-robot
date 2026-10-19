"""VIX 腿落後一整個月時,畫面上必須說出來。

## 為什麼這條值得一個測試檔

VIX 腿是**領先**腿。它存在的唯一理由是搶在均線跌破之前反應 —— 處理「崩盤前」。
它卡在上個月的意思是:**本月若發生 VIX 飆升,畫面上看不到**,而那正好是這個工具
最該發揮作用的時刻,它卻會顯示 0%。

2026-10-14 實測:GitHub `finance-vix` 自 2026-09-22 停更 22 天,線上 PWA 顯示
`乖離 7.8% · 2026-10` 與 `風險分數 0.04 · 2026-09` 並排 —— 月份不同,
而**沒有任何東西說這件事不正常**。L1 健檢有抓到(`資料新鮮度 · vix` 連紅 14 天),
但那個訊號困在一個已經 52 則留言的 Issue 裡,PWA 自己不知道。

## 為什麼不 grep 原始碼

「有沒有寫 `vLast.ym < sLast.ym`」這種檢查,任何一次分支改寫都會讓它失效卻仍然
通過 —— 和 `sw.js` 那次完全同一個理由。所以這裡在 jsdom 裡**呼叫真的 `render()`**,
只問:餵它一份落後的資料,`#lagnote` 有沒有亮、說的是不是對的事。

## 刻意不報警的三種情況

- **同月** —— 正常狀態。沒有這條對照組,一個「永遠亮著」的警示列也會全綠。
- **VIX 超前** —— 那是刻意的設計(VIX 腿允許用進行中的當月),不是故障。
- **月份字串壞掉** —— 寧可漏報也不要算出一個假的月數。亂叫的指示燈幾天後就會被無視。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import require_js_bridge  # noqa: E402

TESTS_DIR = Path(__file__).resolve().parent
BRIDGE = TESTS_DIR / "render_bridge.mjs"



def _run(index_html: Path, scenario: str, now: str | None = None) -> dict:
    """`now` 固定橋接器裡的時鐘。預設 2026-10-14(實測到這組故障的那天)。"""
    require_js_bridge(
        "兩腿落後的警示列 —— 相對落後(VIX 腿落後趨勢腿)與絕對落後"
        "(兩腿一起落後日曆)。少了它,『VIX 飆升期間畫面顯示 0%』這條"
        "實測過的故障就沒有人盯著了。")
    proc = subprocess.run(
        ["node", str(BRIDGE), str(index_html), scenario, *( [now] if now else [] )],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    if proc.returncode != 0:
        pytest.fail(f"渲染橋接器失敗(情境 {scenario}):\n{proc.stderr}")
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def aligned(index_html: Path) -> dict:
    return _run(index_html, "aligned")


# ── 該報警的 ──────────────────────────────────────────────────────────────

def test_vix_one_month_behind_is_surfaced(index_html: Path) -> None:
    got = _run(index_html, "vix_one_behind")
    assert got["lag_on"], (
        "VIX 腿落後一個月,畫面上卻完全沒有提示 —— 領先腿卡在舊月份時,"
        "本月的 VIX 飆升不會反映在數字上,而使用者看不出來"
    )
    assert "2026-09" in got["lag_text"] and "2026-10" in got["lag_text"], (
        f"警示列沒有把兩個月份都說出來,使用者無法自己判斷差多少:{got['lag_text']!r}"
    )
    assert "落後趨勢腿 1 個月" in got["lag_text"], f"沒有說落後幾個月:{got['lag_text']!r}"


def test_two_months_behind_says_two(index_html: Path) -> None:
    """月數要是算出來的,不是寫死「落後 1 個月」。

    ⚠ 判準必須是**完整片語**。第一版寫成 `"2" in lag_text` —— 而文字裡有 `2026-08`,
    年份就滿足了它,於是「把月數寫死成 1」這個突變逃掉了。
    這條註解留著,因為下一個人很可能會想把它簡化回去。
    """
    got = _run(index_html, "vix_two_behind")
    assert got["lag_on"]
    assert got["months_between"] == 2
    assert "落後趨勢腿 2 個月" in got["lag_text"], (
        f"落後兩個月卻沒說 2 —— 月數可能是寫死的:{got['lag_text']!r}"
    )
    assert "落後趨勢腿 1 個月" not in got["lag_text"], (
        f"同時出現了「落後 1 個月」,月數是寫死的:{got['lag_text']!r}"
    )


# ── 不該報警的(對照組)────────────────────────────────────────────────────

def test_aligned_months_do_not_warn(aligned: dict) -> None:
    """沒有這條,一個『永遠亮著』的警示列也會讓上面兩條全綠。"""
    assert not aligned["lag_on"], (
        f"兩條腿同月卻亮了落後警示 —— 這是狼來了:{aligned['lag_text']!r}"
    )
    assert aligned["lag_text"] == ""


def test_vix_ahead_is_not_a_fault(index_html: Path) -> None:
    """VIX 腿超前是刻意的設計(允許用進行中的當月),不是故障。"""
    got = _run(index_html, "vix_ahead")
    assert not got["lag_on"], (
        "VIX 腿比趨勢腿新就報警,會把這個工具的核心設計(VIX 腿搶快)"
        f"當成故障,每個月初都亮一次:{got['lag_text']!r}"
    )


def test_malformed_month_does_not_fabricate_a_lag(index_html: Path) -> None:
    """月份字串解析不出來時寧可漏報 —— 不要算出一個假的月數去亂叫。"""
    got = _run(index_html, "malformed_ym")
    assert got["months_between"] == 0
    assert not got["lag_on"], (
        f"壞掉的月份字串被算成了落後,這會變成假警報:{got['lag_text']!r}"
    )


# ── 硬邊界:這條警示不得改變核心讀數 ──────────────────────────────────────

def test_lag_warning_does_not_change_the_cash_number(index_html: Path) -> None:
    """落後警示是**平行的第二個訊息**,不是現金水位的輸入。

    和體制層同一條硬邊界:`cash = max(mom_cash, vix_cash)` 有 440 個月的凍結
    基準釘著。一個「資料舊了就自己把現金拉高」的版本會毀掉黃金基準,
    而基準正是判斷有沒有改壞的唯一依據。
    """
    base = _run(index_html, "aligned")
    lagged = _run(index_html, "vix_one_behind")
    for key in ("bigpct", "verdict", "momcash", "vixcash"):
        assert base[key] == lagged[key], (
            f"落後警示改變了核心讀數 `{key}`:{base[key]!r} -> {lagged[key]!r} —— "
            "這一層只准報告事實,不准動現金水位"
        )


def test_lag_warning_does_not_light_the_definition_warning(index_html: Path) -> None:
    """定義警示列(退回 Shiller 月均價)是另一件事,不該被落後警示帶著亮。"""
    got = _run(index_html, "vix_one_behind")
    assert not got["defnote_on"], (
        "VIX 落後讓『價格定義不同』的警示列也亮了 —— 兩個不同的故障顯示成同一件事,"
        "使用者會以為價格定義變了"
    )


# ══════════════════════════════════════════════════════════════════════════
# 絕對落後:兩條腿一起落後【日曆】
#
# 這一段是第一版漏掉的那一半,而且是更危險的那一半。
# 2026-10-14 在本機實測改動時才發現:代理鏈全死退回 Shiller 之後,
# 趨勢腿也掉到 2026-09,於是兩條腿月份【一致】——
# 相對落後的判斷完全靜默,而整份讀數其實都是上個月的。
#
# 月份看起來一致反而更像正常,所以比相對落後更不容易被發現。
# ══════════════════════════════════════════════════════════════════════════


def test_both_legs_stale_is_surfaced(index_html: Path) -> None:
    got = _run(index_html, "both_stale")
    assert got["lag_on"], (
        "兩條腿都停在上個月,畫面上卻完全沒有提示 —— 月份一致看起來像正常,"
        "但 VIX 的『月內飆升』項吃的是當月日內最高,沒有本月資料就是全盲"
    )
    assert "落後日曆 1 個月" in got["lag_text"], f"沒說是落後日曆:{got['lag_text']!r}"
    assert "2026-09" in got["lag_text"] and "2026-10" in got["lag_text"]


def test_early_in_the_month_is_not_a_fault(index_html: Path) -> None:
    """月初寬限期內不報警 —— 1 號遇上週末時當月可能還沒有任何交易日。

    這是對照組:沒有它,一個「每個月 1 號都亮一次」的警示列也會讓上面那條全綠,
    而那正是會訓練使用者無視通知的設計。
    """
    got = _run(index_html, "both_stale", now="2026-10-03T12:00:00Z")
    assert not got["lag_on"], (
        f"當月 3 號就報『落後日曆』—— 每個月初都會亮一次:{got['lag_text']!r}"
    )


def test_the_grace_period_does_end(index_html: Path) -> None:
    """寬限期必須真的會結束,否則上一條等於把這個檢查永久關掉。"""
    assert _run(index_html, "both_stale", now="2026-10-06T12:00:00Z")["lag_on"]


def test_calendar_lag_counts_months_not_a_fixed_string(index_html: Path) -> None:
    """落後兩個月要說 2 —— 月數是算出來的,不是寫死的。"""
    got = _run(index_html, "both_stale", now="2026-11-20T12:00:00Z")
    assert "落後日曆 2 個月" in got["lag_text"], got["lag_text"]


def test_both_kinds_of_lag_are_reported_together(index_html: Path) -> None:
    """VIX 落後趨勢腿【而且】兩者都落後日曆時,兩件事都要說。

    只說一件會讓使用者以為修好一邊就沒事了。
    """
    got = _run(index_html, "vix_one_behind", now="2026-11-20T12:00:00Z")
    assert "落後趨勢腿" in got["lag_text"], got["lag_text"]
    assert "落後日曆" in got["lag_text"], got["lag_text"]
