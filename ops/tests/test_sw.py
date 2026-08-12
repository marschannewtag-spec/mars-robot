"""Service Worker 的行為測試 —— 在 Node 裡真的把 sw.js 跑起來。

2026-08-12 線上巡檢時挖到:`sw.js` 的靜態檔分支是**快取優先**,卻對回應
的狀態碼照單全收。一次 404 被存進快取之後,就算檔案後來部署上去了,
SW 仍會一直回那個 404 —— 直到 `CACHE` 版本號 +1 為止。

這個 bug 的形狀和這個專案裡其他幾個一樣:**沒有症狀**。
使用者只會看到某個圖示不見了、某個檔案讀不到,重新整理也沒用,
而 DevTools 的 Network 分頁顯示 200(因為那是 SW 回的,不是網路回的)。

所以這裡不 grep 原始碼 —— 「有沒有寫 res.ok」這種檢查,任何一次分支
改寫都會讓它失效卻仍然通過。這裡問的是唯一重要的那件事:
**一個 404 打進來之後,快取裡有沒有多一筆。**
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import REPO_ROOT

TESTS_DIR = Path(__file__).resolve().parent
BRIDGE = TESTS_DIR / "sw_bridge.mjs"
SW = REPO_ROOT / "sw.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 才能執行 SW 橋接器"
)


@pytest.fixture(scope="module")
def sw_src() -> str:
    return SW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def run() -> dict:
    """把 sw.js 載進假的 ServiceWorker 環境,派送幾種 fetch 事件。"""
    proc = subprocess.run(
        ["node", str(BRIDGE), str(SW)],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    if proc.returncode != 0:
        pytest.fail(f"SW 橋接器失敗(exit {proc.returncode}):\n{proc.stderr}")
    return json.loads(proc.stdout)


# ── 前提檢查:這組情境真的分得出好壞嗎 ──────────────────────────────────


def test_a_successful_response_really_does_get_cached(run: dict) -> None:
    """先證明快取真的會被寫入,否則下面那幾條「沒被寫入」全部是恆真的。"""
    s = run["scenarios"]["static_200"]
    assert s["cachedNow"] == 1, f"200 的回應沒有進快取 —— 離線功能等於沒有:{s}"
    assert s["status"] == 200


# ── 失敗的回應不得進快取 ────────────────────────────────────────────────


@pytest.mark.parametrize("case", ["static_404", "static_500"])
def test_failed_static_response_is_not_cached(run: dict, case: str) -> None:
    """③ 是快取優先 —— 存錯一次就會一直錯到版本號 +1。"""
    s = run["scenarios"][case]
    assert s["cachedNow"] == 0, (
        f"{case}:失敗的回應被存進快取了。這個分支是快取優先,"
        f"之後就算檔案部署上去也會一直回舊的失敗回應,而且【完全沒有症狀】"
    )
    assert not s["cachedUrl"]


def test_partial_content_is_not_handed_to_the_cache(run: dict) -> None:
    """206 是 `res.ok` 為 true、卻**不能**存進 Cache 的那個洞。

    `Cache.put()` 收到 206 會拋 TypeError,而 sw.js 的 put 沒有接 catch ——
    那是一個沒人看得到的 unhandled rejection。所以判準必須是 `status === 200`,
    不能只寫 `res.ok`(那也正是最容易被「順手簡化」成的寫法)。
    """
    s = run["scenarios"]["static_206"]
    assert s["putErrors"] == [], (
        f"206 的回應被交給 Cache.put() 了 —— 會拋 TypeError 且無人接手:{s['putErrors']}\n"
        f"判準要寫 status === 200,不能只寫 res.ok"
    )
    assert s["unhandled"] == [], f"產生了無人接手的 rejection:{s['unhandled']}"
    assert s["cachedNow"] == 0


def test_failed_response_is_still_returned_to_the_page(run: dict) -> None:
    """不快取不等於吞掉 —— 頁面仍要拿到真實的狀態碼,否則會變成另一種說謊。"""
    for case, want in (("static_404", 404), ("static_500", 500)):
        s = run["scenarios"][case]
        assert s["threw"] is None, f"{case} 拋了例外:{s['threw']}"
        assert s["status"] == want, f"{case} 回給頁面的狀態碼是 {s['status']}"


def test_a_404_navigation_does_not_become_the_offline_homepage(run: dict) -> None:
    """② 導覽分支存了 404 的話,離線時的首頁就會是一張 404 頁。"""
    s = run["scenarios"]["navigate_404"]
    assert s["cachedNow"] == 0 and not s["cachedUrl"], (
        f"404 的導覽回應被存起來了 —— 離線首頁會變成 404 頁:{s}"
    )


def test_a_successful_navigation_is_still_cached(run: dict) -> None:
    """網路優先 + 離線退回快取,是這份 SW 的核心策略,不得被順手關掉。"""
    assert run["scenarios"]["navigate_200"]["cachedUrl"]


# ── 分流規則 ────────────────────────────────────────────────────────────


def test_cross_origin_data_is_never_intercepted(run: dict) -> None:
    """資料 API 一旦被 SW 碰到就可能拿到舊資料 —— 那會直接害現金水位算錯。"""
    assert not run["scenarios"]["cross_origin"]["intercepted"], (
        "SW 介入了跨網域請求。資料 API 必須完全放行,否則使用者可能看到過期的價格"
    )


def test_non_get_is_not_intercepted(run: dict) -> None:
    assert not run["scenarios"]["post"]["intercepted"]


# ── 靜態性質 ────────────────────────────────────────────────────────────


def test_every_shell_file_exists(sw_src: str) -> None:
    """`addAll` 是**原子**的:SHELL 裡少一個檔,整批就 reject。

    而 install 的 `.catch(() => {})` 會把它吞掉 —— 於是 SW 註冊成功、
    版本號也對,離線卻完全不能用,沒有任何錯誤訊息。
    """
    m = re.search(r"const SHELL\s*=\s*\[(.*?)\]", sw_src, re.DOTALL)
    assert m, "找不到 SHELL"
    missing = [p for p in re.findall(r'"\./([^"]*)"', m.group(1))
               if p and not (REPO_ROOT / p).exists()]
    assert not missing, (
        f"SHELL 列了不存在的檔案:{missing} —— addAll 是原子操作,"
        f"一個失敗就整批不進快取,而 install 的 catch 會把它靜靜吞掉"
    )


def test_cache_name_is_versioned(sw_src: str) -> None:
    """版本號是清掉舊快取的唯一機制(activate 只留下同名的那個)。"""
    m = re.search(r'const CACHE\s*=\s*"cwgauge-v(\d+)"', sw_src)
    assert m, "CACHE 不是 cwgauge-vN 格式 —— activate 的清理邏輯靠這個命名"
    assert int(m.group(1)) >= 13, "版本號不該往回退,舊快取會清不掉"
