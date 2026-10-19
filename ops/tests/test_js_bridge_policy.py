"""`require_js_bridge()` 這個守門員自己的測試(2026-10-20)。

為什麼需要獨立一條
------------------
`test_reporting.py` 掃的是「有沒有人還在用 skip 處理 node/jsdom 缺席」——
它掃的是**呼叫端**。但守門員自己如果哪天被改成 `pytest.skip`,
那條掃描完全看不到:每個呼叫端都還是 `require_js_bridge(...)`,
長得一模一樣,而整組測試會再次回到「392 passed, 32 skipped」。

也就是說:掃描釘住了「誰在用」,沒有釘住「它做什麼」。
這一條補的是後者,而且是**行為**測試,不是靜態掃描 ——
直接把 node 弄不見,看它拋的是 Failed 還是 Skipped。

⚠ 這個檔案刻意獨立。放進 `test_reporting.py` 會讓那邊的自我排除守衛
   (「本檔不得呼叫 require_js_bridge」)誤判 —— 而那個守衛是有用的,
   不該為了這一條把它拆掉。
"""

from __future__ import annotations

import shutil

import pytest

from conftest import JS_BRIDGE_SETUP, require_js_bridge

# 用一段夠長的說明,讓 test_every_bridge_gate_says_what_is_lost 不會把
# 這個「示範用」的呼叫當成寫太短的真實關口。
_WHAT = ("這是 require_js_bridge 自己的測試,不是真的關口 —— "
         "用來確認它在缺席時拋的是失敗而不是跳過。")


def test_it_fails_rather_than_skips_when_node_is_missing(monkeypatch) -> None:
    """node 不在 → 必須是 **Failed**,不是 Skipped。

    這兩者在 exit code 上的差別就是整件事的全部:
    `實測`,jsdom 不在時 ——
        改之前  392 passed, 32 skipped   exit 0  ← CI 綠燈
        改之後   22 failed,  11 errors   exit 1
    """
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(BaseException) as ei:
        require_js_bridge(_WHAT)
    assert ei.typename == "Failed", (
        f"守門員用了 {ei.typename} —— 必須是 Failed。"
        "Skipped 和 pass 在 exit code 上是同一件事,"
        "而這正是這一輪在修的東西。")


def test_the_message_says_what_is_lost_and_how_to_fix_it(monkeypatch) -> None:
    """錯誤訊息必須同時有「失去什麼」和「怎麼修」。

    只講怎麼修的話,讀的人知道要跑哪一行,卻不知道不跑會失去什麼 ——
    於是最自然的反應是把它改回 skip。那是這條規則最可能的死法。
    """
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(BaseException) as ei:
        require_js_bridge(_WHAT)
    msg = str(ei.value)
    assert _WHAT[:20] in msg, f"沒有把「失去什麼」放進訊息:{msg!r}"
    assert JS_BRIDGE_SETUP.split()[0] in msg, f"沒有講怎麼修:{msg!r}"
    assert "npm" in msg


def test_it_passes_silently_when_the_bridge_is_there() -> None:
    """控制組:橋接器在的時候,它必須什麼都不做。

    少了這一條,「永遠失敗」會是讓上面兩條全綠的最省事寫法 ——
    而那會讓整組測試在所有機器上都紅,然後被整個拿掉。
    """
    require_js_bridge(_WHAT)      # 不拋就是通過


def test_jsdom_is_required_by_default(monkeypatch, tmp_path) -> None:
    """預設要檢查 jsdom —— 只有明確寫 `needs_jsdom=False` 才放過。

    預設若變成不檢查,那六個真的需要 jsdom 的關口就會通過,然後在
    node 子行程裡以「Cannot find module 'jsdom'」收場 —— 訊息變回
    看不出失去什麼的那種,而這一輪修的就是訊息。
    """
    import conftest

    monkeypatch.setattr(conftest, "OPS_ROOT", tmp_path)   # 底下沒有 node_modules
    with pytest.raises(BaseException) as ei:
        require_js_bridge(_WHAT)
    assert ei.typename == "Failed", ei.typename
    assert "jsdom" in str(ei.value)

    # 明確說不需要的,就不該被 jsdom 擋下來
    require_js_bridge(_WHAT, needs_jsdom=False)


# --------------------------------------------------------------------------
# `require_shell()` —— 同一條規則,工具不同(2026-10-20)
#
# `test_annual_ops_data.py` 與 `test_l1_failure_harness.py` 原本用
# `skipif(shutil.which("bash") is None)`,理由寫「CI 的 ubuntu runner 一定有」。
#
# 那句話是對的,而那**正好是讓 skip 變危險的原因**:在一定有的地方跳過不會
# 發生,所以沒有人會注意到那個 skip 還在 —— 直到它真的在某個環境生效,
# 而那時它會安靜地把整組測試變成綠的。
#
# 這兩個檔案守的是「把 workflow 的那段 shell 從 YAML 抽出來真的執行」,
# 也就是 L1 失敗路徑與 L3 的 ops-data 兩步**唯一**的證據。
# --------------------------------------------------------------------------

def test_require_shell_fails_rather_than_skips(monkeypatch) -> None:
    from conftest import require_shell

    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(BaseException) as ei:
        require_shell(_WHAT, "bash", "git")
    assert ei.typename == "Failed", (
        f"守門員用了 {ei.typename} —— 必須是 Failed。"
        "skip 和 pass 在 exit code 上是同一件事。")
    msg = str(ei.value)
    assert "bash" in msg and "git" in msg, f"沒講出缺哪個工具:{msg!r}"
    assert _WHAT[:20] in msg, f"沒講出失去什麼:{msg!r}"


def test_require_shell_only_complains_about_what_is_actually_missing(
    monkeypatch
) -> None:
    """只缺一個就只講那一個 —— 訊息說錯話的檢查會被當成雜訊關掉。"""
    from conftest import require_shell

    monkeypatch.setattr(shutil, "which", lambda name: None if name == "git" else "/x")
    with pytest.raises(BaseException) as ei:
        require_shell(_WHAT, "bash", "git")
    msg = str(ei.value)
    assert "git" in msg, msg
    assert "找不到 bash" not in msg, f"bash 明明在,訊息卻說它不在:{msg!r}"


def test_require_shell_passes_when_the_tools_are_there() -> None:
    """控制組:工具都在就什麼都不做。

    少了這一條,「永遠失敗」會讓上面兩條全綠,而整組測試在所有機器上都紅。
    """
    from conftest import require_shell

    require_shell(_WHAT, "bash", "git")
