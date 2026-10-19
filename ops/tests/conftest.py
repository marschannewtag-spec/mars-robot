"""pytest 共用設定。

所有測試一律吃 tests/fixtures/ 的凍結快照,**不碰網路**。
這是刻意的分層:L2(邏輯)必須離線可跑,否則資料源某天抽風會讓 CI 變紅燈,
你就分不清是「邏輯壞了」還是「Yahoo 今天不舒服」—— L1 的問題污染 L2 的訊號,
兩層就都失去意義了。資料源的死活由 L1 的 health_check.py 負責。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

OPS_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO_ROOT = OPS_ROOT.parent

sys.path.insert(0, str(OPS_ROOT / "src"))


@pytest.fixture(scope="session")
def vix_daily() -> pd.DataFrame:
    return pd.read_csv(FIXTURES / "vix_daily.csv")


@pytest.fixture(scope="session")
def sp_daily() -> pd.DataFrame:
    return pd.read_csv(FIXTURES / "sp500_daily.csv")


@pytest.fixture(scope="session")
def shiller() -> pd.DataFrame:
    return pd.read_csv(FIXTURES / "shiller_sp500.csv")


@pytest.fixture(scope="session")
def signals(sp_daily: pd.DataFrame, vix_daily: pd.DataFrame) -> pd.DataFrame:
    import engine

    return engine.signal_frame(
        engine.sp_monthly(sp_daily), engine.build_vix_monthly(vix_daily)
    )


@pytest.fixture(scope="session")
def index_html() -> Path:
    """線上真的在跑的那個 PWA 檔案。"""
    return REPO_ROOT / "index.html"


# --------------------------------------------------------------------------
# JavaScript 橋接器:缺席是【失敗】,不是跳過(2026-10-20)
#
# `實測`:把 `ops/node_modules/jsdom` 改個名字再跑整組 ——
#
#     392 passed, 32 skipped in 26.87s      ← exit 0,看起來完全健康
#
# 那 32 條是**唯一會執行 PWA 真實 JavaScript** 的測試:engine 與 PWA 的
# 逐月對帳、五層來源退回鏈、兩腿落後警示、發布鏡像的等價性。
# 也就是整個專案唯一能回答「使用者螢幕上那個數字對不對」的那一層。
#
# 原本它們用 `pytest.skip`。那違反這份 repo 最常被引用的那條規則:
# **「沒得判斷」和「通過」不可以長得一樣。** 一個沒有人讀的 skip,
# 和 pass 在 exit code 上是同一件事。
#
# 對照:Python 那邊的相依缺失從來就不是 skip ——
# `test_the_running_environment_matches_requirements` 直接硬失敗。
# `npm ci --prefix ops` 和 `pip install -r requirements-test.txt` 是同一
# 層級的一次性設定步驟,沒有理由一個紅一個綠。
#
# 代價是沒裝 node 的人會看到紅燈。所以訊息必須**可操作**:講出少了什麼
# 驗證、以及要跑哪一行。
# --------------------------------------------------------------------------

JS_BRIDGE_SETUP = "npm ci --prefix ops   (需要 Node.js 22+)"


def require_js_bridge(what: str, *, needs_jsdom: bool = True) -> None:
    """node(視情況還有 jsdom)不在就讓這條測試【失敗】。

    `what` 要寫出**這條測試在驗什麼**,因為那正是缺席時失去的東西。
    只寫「尚未 npm install jsdom」的話,讀的人知道怎麼修,
    卻不知道不修會失去什麼 —— 於是最自然的反應是把它改回 skip。

    `needs_jsdom=False` 給只需要 node 的橋接器 —— `sw_bridge.mjs` 只
    `import { readFileSync } from "node:fs"`,它自己造假的 ServiceWorker
    環境,不碰 jsdom。要求它也要有 jsdom 會讓訊息說錯話,
    而說錯話的檢查遲早會被當成雜訊關掉。
    """
    import shutil

    missing = []
    if shutil.which("node") is None:
        missing.append("node 不在 PATH 上")
    if needs_jsdom and not (OPS_ROOT / "node_modules" / "jsdom").is_dir():
        missing.append("ops/node_modules/jsdom 不存在")
    if not missing:
        return

    pytest.fail(
        f"JavaScript 橋接器不可用({' / '.join(missing)})。\n"
        f"  失去的驗證:{what}\n"
        f"  修法:{JS_BRIDGE_SETUP}\n"
        f"\n"
        f"  這裡刻意【不】跳過。實測把 jsdom 藏起來跑整組會得到\n"
        f"  「392 passed, 32 skipped」—— exit 0、CI 綠燈,而整層\n"
        f"  PWA JavaScript 驗證不存在。跳過和通過長得一樣就等於沒有把關。"
    )
