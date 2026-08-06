"""績效計算層 —— 唯一出現 shift(1) 的地方。

`engine.py` 只產生訊號(某月月底的建議現金比重)。本模組負責把訊號轉成
可執行的部位並計算績效:

    position(t) = signal(t-1)

也就是月收盤確認訊號、次月才執行,避免前視偏誤。`test_invariants.py`
會驗證這個 shift 確實存在。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

MONTHS_PER_YEAR = 12


@dataclass(frozen=True)
class Performance:
    """一段權益曲線的績效摘要。"""

    cagr: float
    max_drawdown: float  # 負數,例如 -0.171
    calmar: float
    months: int
    start_ym: str
    end_ym: str

    def as_dict(self) -> dict:
        return asdict(self)


def to_positions(signal: pd.Series) -> pd.Series:
    """訊號 -> 可執行部位。這就是那個 shift(1)。"""
    return signal.shift(1)


def equity_curve(
    prices: pd.Series,
    cash_weight: pd.Series,
    cash_annual_yield: float = 0.0,
) -> pd.Series:
    """由價格與現金比重算出權益曲線(起始 = 1.0)。

    參數
    ----
    prices           : 月頻價格序列(與 cash_weight 等長、同索引)
    cash_weight      : 已經 shift 過的部位(現金比重),NaN 視為 0(滿倉)
    cash_annual_yield: 現金部位的年化收益。預設 0 —— 這是保守假設,
                       實際持有短債會有收益,會低估策略報酬。年度健檢
                       報告會同時列出 0% 與短債假設兩種結果。

    注意:Shiller 的 SP500 欄是「價格指數」,不含股息。因此本函式算出的
    是價格報酬,會系統性低估買進持有的真實總報酬。兩者用同一把尺,
    比較仍然公平,但絕對數字不可對外當成總報酬引用。
    """
    px = prices.astype(float)
    w_cash = cash_weight.astype(float).fillna(0.0).clip(0.0, 1.0)
    w_equity = 1.0 - w_cash

    mkt_ret = px.pct_change()
    cash_ret = (1.0 + cash_annual_yield) ** (1.0 / MONTHS_PER_YEAR) - 1.0

    strat_ret = w_equity * mkt_ret + w_cash * cash_ret
    strat_ret = strat_ret.fillna(0.0)
    return (1.0 + strat_ret).cumprod()


def max_drawdown(equity: pd.Series) -> float:
    """最大回撤,回傳負數。"""
    return float((equity / equity.cummax() - 1.0).min())


def cagr(equity: pd.Series, months: int) -> float:
    """年化複合成長率。months 為實際持有月數。"""
    if months <= 0 or equity.iloc[-1] <= 0:
        return float("nan")
    return float(equity.iloc[-1] ** (MONTHS_PER_YEAR / months) - 1.0)


def summarize(equity: pd.Series, ym: pd.Series) -> Performance:
    """權益曲線 -> 績效摘要。"""
    months = len(equity) - 1  # 第一個月是起點,沒有報酬
    c = cagr(equity, months)
    mdd = max_drawdown(equity)
    calmar = float(c / abs(mdd)) if mdd < 0 else float("nan")
    return Performance(
        cagr=c,
        max_drawdown=mdd,
        calmar=calmar,
        months=months,
        start_ym=str(ym.iloc[0]),
        end_ym=str(ym.iloc[-1]),
    )


def run(
    signals: pd.DataFrame,
    cash_annual_yield: float = 0.0,
) -> tuple[Performance, Performance, pd.DataFrame]:
    """跑完整回測。

    參數
    ----
    signals : `engine.signal_frame()` 的輸出(需含 ym / price / cash)

    回傳
    ----
    (策略績效, 買進持有績效, 逐月明細)
    """
    df = signals.copy().reset_index(drop=True)
    df["position_cash"] = to_positions(df["cash"])

    df["equity_strategy"] = equity_curve(
        df["price"], df["position_cash"], cash_annual_yield
    )
    df["equity_buyhold"] = equity_curve(
        df["price"], pd.Series(0.0, index=df.index), cash_annual_yield
    )

    perf_strategy = summarize(df["equity_strategy"], df["ym"])
    perf_buyhold = summarize(df["equity_buyhold"], df["ym"])
    return perf_strategy, perf_buyhold, df


def slice_window(signals: pd.DataFrame, start_ym: str, end_ym: str) -> pd.DataFrame:
    """依 'YYYY-MM' 字串切窗口(字串比較對 ISO 年月是安全的)。"""
    m = (signals["ym"] >= start_ym) & (signals["ym"] <= end_ym)
    return signals.loc[m].reset_index(drop=True)
