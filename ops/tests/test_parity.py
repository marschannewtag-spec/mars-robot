"""三方一致性 —— engine.py(真理來源)↔ PWA index.html 的 JS。

不是把 JS 抄一份過來比對,而是用 jsdom 載入【線上真的在跑的那個 index.html】,
呼叫它自己的函式。所以這個測試失敗就代表使用者現在看到的數字真的和真理來源不一致,
而不是「某份副本過期了」。

Pine 那一份無法在 CI 執行(TradingView 是封閉環境),改由
scripts/check_pine_parity.py 對匯出的 CSV 做手動比對 —— 見該檔說明。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest

import engine

TESTS_DIR = Path(__file__).resolve().parent
FIXTURES = TESTS_DIR / "fixtures"
BRIDGE = TESTS_DIR / "js_bridge.mjs"
TOL = 1e-9

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 才能執行 JS 橋接器"
)


@pytest.fixture(scope="module")
def js_result(index_html: Path) -> dict:
    """在 Node 裡跑一次 PWA 的計算,拿回逐月結果。"""
    if not (TESTS_DIR.parent / "node_modules" / "jsdom").exists():
        pytest.skip("尚未 npm install jsdom")
    proc = subprocess.run(
        [
            "node", str(BRIDGE), str(index_html),
            str(FIXTURES / "vix_daily.csv"),
            str(FIXTURES / "sp500_daily.csv"),
            str(FIXTURES / "shiller_sp500.csv"),
        ],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    if proc.returncode != 0:
        pytest.fail(f"JS 橋接器失敗(exit {proc.returncode}):\n{proc.stderr}")
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def paired(js_result: dict, signals: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    js = pd.DataFrame(js_result["series"]).set_index("ym")
    py = signals.set_index("ym")
    common = js.index.intersection(py.index)
    return js.loc[common], py.loc[common]


def _report(js: pd.Series, py: pd.Series, col: str) -> str:
    d = (js - py).abs()
    bad = d[d > TOL]
    lines = [f"{col} 有 {len(bad)} 個月超出容差 {TOL}(最大差 {d.max():.6g}):"]
    for ym in bad.index[:12]:
        lines.append(f"  {ym}  JS={js[ym]!r}  engine.py={py[ym]!r}")
    if len(bad) > 12:
        lines.append(f"  … 另外 {len(bad) - 12} 個月")
    return "\n".join(lines)


def test_same_months_covered(paired) -> None:
    js, py = paired
    assert len(js) > 400, "覆蓋月數太少,比對沒有意義"
    assert list(js.index) == list(py.index)


@pytest.mark.parametrize("col", ["price", "mom", "mom_cash"])
def test_trend_leg_matches(paired, col: str) -> None:
    """趨勢腿:價格聚合、乖離率、階梯輸出。"""
    js, py = paired
    d = (js[col] - py[col]).abs()
    assert d.max() <= TOL, _report(js[col], py[col], col)


def test_vix_monthly_aggregation_matches(js_result: dict, vix_daily: pd.DataFrame) -> None:
    """VIX 月頻聚合:月收、月內最高、交易日數。

    這裡是最容易漂移的地方 —— 「月內最高」到底是日內最高還是收盤最高,
    兩邊很容易各寫各的,而且差異只在少數月份才會翻轉現金水位,不容易被眼睛看到。
    """
    js = pd.DataFrame(js_result["vix_monthly"]).set_index("ym")
    py = engine.build_vix_monthly(vix_daily).set_index("ym")
    common = js.index.intersection(py.index)
    for col in ["close", "max", "n"]:
        d = (js.loc[common, col] - py.loc[common, col]).abs()
        assert d.max() <= TOL, _report(js.loc[common, col], py.loc[common, col], f"vix.{col}")


@pytest.mark.parametrize("col", ["risk", "vix_cash"])
def test_vix_leg_matches(paired, col: str) -> None:
    js, py = paired
    d = (js[col] - py[col]).abs()
    assert d.max() <= TOL, _report(js[col], py[col], col)


def test_union_cash_matches(paired) -> None:
    """最終輸出 —— 使用者真正看到的那個數字。"""
    js, py = paired
    d = (js["cash"] - py["cash"]).abs()
    assert d.max() <= TOL, _report(js["cash"], py["cash"], "cash")


def test_compute_assembly_matches(js_result: dict, signals: pd.DataFrame) -> None:
    """PWA 的 compute() 組裝層(不只各別公式)也要一致。"""
    last = js_result["compute_last"]
    py_last = signals.iloc[-1]
    assert last["ym"] == py_last["ym"]
    for k in ["mom", "risk", "mom_cash", "vix_cash", "cash"]:
        assert abs(last[k] - py_last[k]) <= TOL, (
            f"compute() 的 {k} 不一致:JS={last[k]!r} engine.py={py_last[k]!r}"
        )


def test_shiller_fallback_definition_matches(js_result: dict, shiller: pd.DataFrame) -> None:
    """退回來源(Shiller 月均價)兩邊也要一致。

    這條路徑的價格定義本來就與正式的月收盤不同(PWA 會顯示警示列),
    但既然保留了它,兩份實作對「月均價」的算法也必須一致。
    """
    js = pd.DataFrame(js_result["shiller_monthly"]).set_index("ym")["price"]
    py = engine.sp_monthly_shiller_avg(shiller).set_index("ym")["price"]
    common = js.index.intersection(py.index)
    assert len(common) > 400
    d = (js.loc[common] - py.loc[common]).abs()
    assert d.max() <= TOL, _report(js.loc[common], py.loc[common], "shiller.price")
