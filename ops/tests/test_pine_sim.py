"""Pine 版三方一致性 —— 在沒有 TradingView 的情況下能驗到的部分。

`pine_sim.py` 從 `.pine` 原始檔解析常數與階梯,再用 Pine 的語義重現計算,
與 `engine.py` 逐月比對。

**這一層驗得到:** 常數、階梯門檻、公式、視窗長度、ddof、暖機期處理、
以及「圖表歷史比 VIX 長」時的 na 對齊。

**這一層驗不到:** Pine 是否編譯得過、`request.security` 的實際行為、
TradingView 的資料是否與這裡一致。那些只能靠 `scripts/check_pine_parity.py`
對真實匯出比對 —— 在那之前 Pine 的狀態是「邏輯已驗證、執行未驗證」。
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

import engine
import pine_sim

TOL = 1e-9


def _aligned_series(sp_daily: pd.DataFrame, vix_daily: pd.DataFrame):
    """模擬月線圖上的 bar 序列。

    圖表的 bar 是 S&P 的月份(1988-12 起),VIX 由 request.security 帶進來,
    在 VIX 尚未有資料的月份是 na。這正是真實圖表的樣子 ——
    如果 Pine 的滾動視窗處理 na 的方式與 engine 不同,這裡就會露餡。
    """
    sp_m = engine.sp_monthly(sp_daily)
    vix_m = engine.build_vix_monthly(vix_daily).set_index("ym")

    bars = sp_m["ym"].tolist()
    sp_close = sp_m["price"].astype(float).tolist()
    vix_close = [float(vix_m["close"][ym]) if ym in vix_m.index else pine_sim.NA
                 for ym in bars]
    vix_high = [float(vix_m["max"][ym]) if ym in vix_m.index else pine_sim.NA
                for ym in bars]
    return bars, sp_close, vix_close, vix_high


@pytest.fixture(scope="module")
def pine_result(sp_daily: pd.DataFrame, vix_daily: pd.DataFrame) -> pd.DataFrame:
    bars, sp_close, vix_close, vix_high = _aligned_series(sp_daily, vix_daily)
    rows = pine_sim.simulate(sp_close, vix_close, vix_high)
    df = pd.DataFrame(rows)
    df.insert(0, "ym", bars)
    return df[df["has_signal"]].set_index("ym")


def test_chart_starts_before_vix_data(sp_daily, vix_daily) -> None:
    """前提檢查:這個測試必須真的涵蓋「圖表比 VIX 長」的情境,否則沒驗到重點。"""
    bars, _, vix_close, _ = _aligned_series(sp_daily, vix_daily)
    na_head = sum(1 for v in vix_close[:24] if math.isnan(v))
    assert na_head > 0, "測試資料裡圖表沒有早於 VIX 的月份 —— na 對齊沒被驗到"


def test_pine_constants_match_engine() -> None:
    """`.pine` 裡的常數必須與 engine.py 的一致。"""
    k = pine_sim.parse_constants(pine_sim._read_pine())
    assert int(k["MOM_LEN"]) == engine.MOM_WARMUP_MONTHS
    assert (k["VIX_BASE"], k["VIX_DIV"], k["SPIKE_DIV"]) == (16.0, 9.0, 12.0)
    assert (k["W_LVL"], k["W_Z"], k["W_SPIKE"]) == (0.55, 0.30, 0.15)
    assert (int(k["Z_LEN"]), int(k["SPIKE_LEN"])) == (36, 6)


def test_pine_ladders_match_engine() -> None:
    """階梯門檻逐點比對 —— 直接拿 .pine 解析出的階梯去跑 engine 的邊界案例。"""
    src = pine_sim._read_pine()
    mom_steps = pine_sim.parse_ladder(src, "momCash")
    vix_steps = pine_sim.parse_ladder(src, "vixCash")

    for v in [0.5, 0.0, -1e-12, -0.03, -0.030001, -0.08, -0.080001,
              -0.13, -0.130001, -0.99]:
        assert pine_sim.apply_ladder(v, mom_steps) == engine.mom_cash(v), f"mom={v}"

    for v in [-5.0, 0.0, 0.5, 0.500001, 1.0, 1.000001, 1.8, 1.800001,
              2.6, 2.600001, 3.4, 3.400001, 99.0]:
        assert pine_sim.apply_ladder(v, vix_steps) == engine.vix_cash(v), f"risk={v}"


def test_pine_covers_same_months(pine_result: pd.DataFrame, signals: pd.DataFrame) -> None:
    py = signals.set_index("ym")
    assert list(pine_result.index) == list(py.index), (
        f"覆蓋月份不同 —— Pine 多出 "
        f"{sorted(set(pine_result.index) - set(py.index))[:5]},少了 "
        f"{sorted(set(py.index) - set(pine_result.index))[:5]}"
    )


@pytest.mark.parametrize("col", ["mom", "risk", "mom_cash", "vix_cash", "cash"])
def test_pine_series_match_engine(pine_result: pd.DataFrame, signals: pd.DataFrame,
                                  col: str) -> None:
    py = signals.set_index("ym")
    common = pine_result.index.intersection(py.index)
    diff = (pine_result.loc[common, col] - py.loc[common, col]).abs()
    bad = diff[diff > TOL]
    assert bad.empty, (
        f"{col} 有 {len(bad)} 個月不一致(最大差 {diff.max():.6g}):\n"
        + "\n".join(
            f"  {ym}  pine={pine_result.loc[ym, col]!r}  engine={py.loc[ym, col]!r}"
            for ym in bad.index[:10]
        )
    )


def test_pine_warmup_matches_engine(pine_result: pd.DataFrame,
                                    signals: pd.DataFrame) -> None:
    """暖機期是最容易兩邊各寫各的地方 —— 前 40 個月逐月比對。"""
    py = signals.set_index("ym")
    common = list(pine_result.index.intersection(py.index))[:40]
    for ym in common:
        assert pine_result.loc[ym, "risk"] == pytest.approx(py.loc[ym, "risk"], abs=TOL), (
            f"{ym} 暖機期風險分數不一致"
        )
