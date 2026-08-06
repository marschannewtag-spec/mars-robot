"""黃金基準回歸 —— 數字變動即代表有東西壞了。

兩層設計:

第一層 · 訊號錨(硬)
    凍結窗口的逐月現金序列,**精確比對**。訊號只需要價格與 VIX,不需要股息、
    不需要利率假設、沒有任何自由參數 —— 所以它完全確定、永遠可重現。
    這一層才是真正防止邏輯漂移的東西。

第二層 · 績效錨(軟)
    CAGR / MaxDD / Calmar,依賴明確寫出的報酬假設,給容差。資訊參考用,
    不當硬性守門 —— 免得報酬假設的爭論污染了回歸測試。

⚠ 錨定的是【凍結快照】而非即時資料。用即時資料當錨的話,每個月數字都會變,
  測試要嘛天天紅燈、要嘛容差開到大得失去意義。快照更新是一個需要人工審視的動作。
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import backtest
import engine

FIXTURES = Path(__file__).resolve().parent / "fixtures"
# 績效指標的容差:0.5 個百分點。訊號層不給容差。
PERF_TOL = 0.005


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads((FIXTURES / "golden_metrics.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def golden_signals() -> pd.DataFrame:
    return pd.read_csv(FIXTURES / "golden_signals.csv").set_index("ym")


# --------------------------------------------------------------------------
# 第一層:訊號錨
# --------------------------------------------------------------------------


def test_signal_window_unchanged(signals: pd.DataFrame, golden: dict) -> None:
    w = golden["訊號窗口"]
    assert signals["ym"].iloc[0] == w["start"]
    assert signals["ym"].iloc[-1] == w["end"]
    assert len(signals) == w["months"]


def test_cash_series_exact_match(signals: pd.DataFrame, golden_signals: pd.DataFrame) -> None:
    """逐月現金序列必須精確吻合 —— 這是最重要的一條測試。"""
    got = signals.set_index("ym")
    assert list(got.index) == list(golden_signals.index)
    diff = (got["cash"] - golden_signals["cash"]).abs()
    bad = diff[diff > 0]
    assert bad.empty, (
        f"現金序列漂移了 {len(bad)} 個月:\n"
        + "\n".join(
            f"  {ym}  現在={got.loc[ym, 'cash']!r}  黃金={golden_signals.loc[ym, 'cash']!r}"
            for ym in bad.index[:15]
        )
    )


@pytest.mark.parametrize("col", ["mom", "risk", "mom_cash", "vix_cash"])
def test_intermediate_series_match(
    signals: pd.DataFrame, golden_signals: pd.DataFrame, col: str
) -> None:
    """中間值也要錨住 —— 否則兩個抵銷的錯誤可能讓最終現金看起來沒變。"""
    got = signals.set_index("ym")
    diff = (got[col] - golden_signals[col]).abs()
    assert diff.max() <= 1e-9, f"{col} 最大差 {diff.max():.6g}"


def test_key_timepoints(signals: pd.DataFrame, golden: dict) -> None:
    """人工確認過的關鍵時點 —— 這些是這套策略存在的理由。"""
    idx = signals.set_index("ym")
    for ym, expected in golden["關鍵時點"].items():
        assert idx.loc[ym, "cash"] == pytest.approx(expected, abs=1e-9), (
            f"{ym} 應為 {expected:.0%},實際 {idx.loc[ym, 'cash']:.0%}"
        )


def test_level_distribution(signals: pd.DataFrame, golden: dict) -> None:
    counts = signals["cash"].round(2).value_counts()
    for level, expected in golden["水位分布"].items():
        assert int(counts.get(float(level), 0)) == expected, (
            f"水位 {float(level):.0%} 的月份數:預期 {expected},實際 "
            f"{int(counts.get(float(level), 0))}"
        )


def test_turnover(signals: pd.DataFrame, golden: dict) -> None:
    changes = int((signals["cash"].diff().abs() > 1e-9).sum())
    assert changes == golden["水位變動次數"]


# --------------------------------------------------------------------------
# 第二層:績效錨
# --------------------------------------------------------------------------


def _total_return_perf(df: pd.DataFrame):
    px = df["price"].astype(float)
    div = df["Dividend"].astype(float).fillna(0.0)
    mkt = (px + div / 12.0) / px.shift(1) - 1.0

    def curve(cash_w: pd.Series) -> pd.Series:
        w = cash_w.astype(float).fillna(0.0).clip(0.0, 1.0)
        return (1.0 + ((1.0 - w) * mkt).fillna(0.0)).cumprod()

    return (
        backtest.summarize(curve(df["cash"].shift(1)), df["ym"]),
        backtest.summarize(curve(pd.Series(0.0, index=df.index)), df["ym"]),
    )


@pytest.fixture(scope="module")
def perf(signals: pd.DataFrame, shiller: pd.DataFrame, golden: dict):
    sh = shiller.copy()
    sh["ym"] = pd.to_datetime(sh["Date"]).dt.strftime("%Y-%m")
    merged = signals.merge(sh[["ym", "Dividend"]], on="ym", how="left")
    start, end = golden["績效"]["窗口"].split("..")
    w = backtest.slice_window(merged, start, end).reset_index(drop=True)
    return _total_return_perf(w)


@pytest.mark.parametrize("metric", ["cagr", "max_drawdown", "calmar"])
def test_strategy_performance(perf, golden: dict, metric: str) -> None:
    got = getattr(perf[0], metric)
    assert got == pytest.approx(golden["績效"]["策略"][metric], abs=PERF_TOL)


@pytest.mark.parametrize("metric", ["cagr", "max_drawdown", "calmar"])
def test_buyhold_performance(perf, golden: dict, metric: str) -> None:
    got = getattr(perf[1], metric)
    assert got == pytest.approx(golden["績效"]["買進持有"][metric], abs=PERF_TOL)


def test_strategy_reduces_drawdown(perf) -> None:
    """這個工具存在的理由:回撤必須比買進持有小。

    刻意不測「報酬必須比較高」—— 它本來就是用報酬換回撤,
    寫成那樣會是個假的、遲早要被放寬的測試。
    """
    strategy, buyhold = perf
    assert strategy.max_drawdown > buyhold.max_drawdown
