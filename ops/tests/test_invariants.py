"""系統不變量 —— 任何改動都不得違反,違反即 bug。

這些測試不依賴任何績效假設或資料 vintage,只驗證邏輯本身的性質。
它們是最不會誤報、也最不該被放寬容差的一層。
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

import backtest
import engine

# --------------------------------------------------------------------------
# 值域
# --------------------------------------------------------------------------


def test_cash_within_unit_interval(signals: pd.DataFrame) -> None:
    assert signals["cash"].between(0.0, 1.0).all()
    assert signals["mom_cash"].between(0.0, 1.0).all()
    assert signals["vix_cash"].between(0.0, 1.0).all()


def test_cash_is_union_of_two_legs(signals: pd.DataFrame) -> None:
    """聯集:現金必須恰好等於兩腿的較大者,不多不少。"""
    expected = np.maximum(signals["mom_cash"], signals["vix_cash"])
    assert (signals["cash"] - expected).abs().max() == 0.0


def test_cash_only_takes_ladder_values(signals: pd.DataFrame) -> None:
    """輸出只能落在階梯值上 —— 出現中間值代表某處做了不該有的內插或平滑。"""
    allowed = {0.0, 0.24, 0.30, 0.42, 0.60, 0.66, 0.90, 1.00}
    got = set(np.round(signals["cash"].unique(), 10))
    assert got <= allowed, f"出現非階梯值:{sorted(got - allowed)}"


# --------------------------------------------------------------------------
# 階梯門檻(邊界值逐一驗)
# --------------------------------------------------------------------------
# 動能腿用嚴格小於、VIX 腿用嚴格大於 —— 方向不同是刻意的,
# 所以邊界點本身屬於哪一階必須被釘死,不能靠讀程式碼猜。

@pytest.mark.parametrize(
    "mom,expected",
    [
        (0.50, 0.00), (0.00, 0.00),            # 門檻上恰好 0 -> 不拉現金
        (-1e-12, 0.30), (-0.03, 0.30),         # -0.03 本身仍屬 0.30 那階
        (-0.030001, 0.60), (-0.08, 0.60),
        (-0.080001, 0.90), (-0.13, 0.90),
        (-0.130001, 1.00), (-0.99, 1.00),
    ],
)
def test_mom_cash_ladder_boundaries(mom: float, expected: float) -> None:
    assert engine.mom_cash(mom) == expected


@pytest.mark.parametrize(
    "risk,expected",
    [
        (-5.0, 0.00), (0.0, 0.00), (0.5, 0.00),   # 0.5 本身不觸發
        (0.500001, 0.24), (1.0, 0.24),
        (1.000001, 0.42), (1.8, 0.42),
        (1.800001, 0.66), (2.6, 0.66),
        (2.600001, 0.90), (3.4, 0.90),
        (3.400001, 1.00), (99.0, 1.00),
    ],
)
def test_vix_cash_ladder_boundaries(risk: float, expected: float) -> None:
    assert engine.vix_cash(risk) == expected


def test_ladders_are_monotonic() -> None:
    """風險越高,建議現金不得下降。"""
    xs = np.linspace(-0.5, 0.5, 2001)
    ys = [engine.mom_cash(x) for x in xs]
    assert all(a >= b for a, b in zip(ys, ys[1:])), "動能腿:乖離越低現金應越高"

    rs = np.linspace(-1.0, 5.0, 2001)
    vs = [engine.vix_cash(r) for r in rs]
    assert all(a <= b for a, b in zip(vs, vs[1:])), "VIX 腿:分數越高現金應越高"


def test_nan_mom_yields_zero_cash() -> None:
    """NaN 對齊 JS 的 `NaN < 0 === false`,回 0。

    這行為本身是個陷阱,所以要用測試釘住 —— 一旦有人「順手修好」讓它回 NaN,
    JS 與 Python 就會不一致。實務上 signal_frame() 會丟掉暖機期,不會走到這裡。
    """
    assert engine.mom_cash(float("nan")) == 0.0
    assert engine.vix_cash(float("nan")) == 0.0


# --------------------------------------------------------------------------
# 風險分數的構成
# --------------------------------------------------------------------------


def test_risk_score_is_non_negative(signals: pd.DataFrame) -> None:
    """三個組成項都先取 max(·,0),所以總分不可能為負。"""
    assert (signals["risk"] >= 0.0).all()


def test_risk_uses_sample_stdev_not_population(vix_daily: pd.DataFrame) -> None:
    """z-score 必須用樣本標準差(ddof=1)。

    母體標準差(ddof=0)會讓 z 系統性偏大,VIX 腿在門檻附近多拉現金。
    Pine 的 ta.stdev() 正是母體版,所以 Pine 那邊要手算 —— 這個測試守住 Python 側。
    """
    vm = engine.build_vix_monthly(vix_daily)
    close = vm["close"].astype(float)
    risk = engine.vix_risk_series(vm)

    i = 200  # 任取一個暖機期之後的月份
    window = close.iloc[i - 35 : i + 1]
    lvl = max((close.iloc[i] - 16.0) / 9.0, 0.0)
    z_sample = (close.iloc[i] - window.mean()) / window.std(ddof=1)
    spike = (vm["max"].iloc[i] - close.iloc[i - 5 : i + 1].mean()) / 12.0
    expected = (
        0.55 * max(lvl, 0.0) + 0.30 * max(z_sample, 0.0) + 0.15 * max(spike, 0.0)
    )
    assert math.isclose(risk.iloc[i], expected, rel_tol=0, abs_tol=1e-12)


def test_warmup_terms_are_zeroed_not_nan(vix_daily: pd.DataFrame) -> None:
    """暖機期的 z 與 spike 補 0(對齊 JS),而不是讓 NaN 汙染整個分數。"""
    vm = engine.build_vix_monthly(vix_daily)
    risk = engine.vix_risk_series(vm)
    assert risk.notna().all()
    # 前 5 個月連 spike 都還沒暖機,分數應只剩水位項
    close = vm["close"].astype(float)
    for i in range(5):
        assert math.isclose(
            risk.iloc[i], 0.55 * max((close.iloc[i] - 16.0) / 9.0, 0.0), abs_tol=1e-12
        )


# --------------------------------------------------------------------------
# 無前視
# --------------------------------------------------------------------------


def test_position_lags_signal_by_one_month(signals: pd.DataFrame) -> None:
    """position(t) 必須等於 signal(t-1)。這是唯一允許出現 shift 的地方。"""
    pos = backtest.to_positions(signals["cash"])
    assert pd.isna(pos.iloc[0]), "第一期不該有部位 —— 那時還沒有訊號"
    assert (pos.iloc[1:].values == signals["cash"].iloc[:-1].values).all()


def test_engine_has_no_lookahead(sp_daily: pd.DataFrame, vix_daily: pd.DataFrame) -> None:
    """截斷未來資料不得改變過去的訊號。

    這是前視偏誤最直接的檢驗:如果引擎偷看了未來,把資料尾巴砍掉之後,
    先前那些月份的訊號就會變。
    """
    full = engine.signal_frame(
        engine.sp_monthly(sp_daily), engine.build_vix_monthly(vix_daily)
    ).set_index("ym")

    cut = "2015-12"
    sp_cut = sp_daily[sp_daily["date"] <= "2015-12-31"]
    vix_cut = vix_daily[vix_daily["DATE"] <= "2015-12-31"]
    truncated = engine.signal_frame(
        engine.sp_monthly(sp_cut), engine.build_vix_monthly(vix_cut)
    ).set_index("ym")

    common = truncated.index.intersection(full.index)
    assert len(common) > 200, "截斷後樣本太少,測試沒有意義"
    diff = (full.loc[common, "cash"] - truncated.loc[common, "cash"]).abs()
    assert diff.max() == 0.0, (
        f"截斷未來資料改變了過去的訊號 —— 存在前視偏誤。"
        f"受影響月份:{common[diff > 0].tolist()[:10]}"
    )


def test_backtest_first_month_has_no_return(signals: pd.DataFrame) -> None:
    """第一個月是起點,不該產生報酬。"""
    _, _, detail = backtest.run(signals)
    assert math.isclose(detail["equity_strategy"].iloc[0], 1.0, abs_tol=1e-12)
    assert math.isclose(detail["equity_buyhold"].iloc[0], 1.0, abs_tol=1e-12)
