"""現金水位儀 — 訊號引擎(真理來源 / single source of truth)

純函式模組:不碰網路、不讀寫檔案、不印東西、不含任何績效計算。
PWA `index.html` 的 JS 與 `CashWeightGauge.pine` 都必須複製這裡的邏輯,
由 `tests/test_parity.py` 逐月比對,任一處漂移即測試失敗。

重要邊界
--------
本模組只產生「訊號」:某月月底,建議的現金比重是多少。
訊號與部位不是同一件事 —— `position(t) = signal(t-1)`(訊號於月收盤確認、
次月才執行)。那個 shift(1) 只存在於 `backtest.py`,刻意不放在這裡,
因為 PWA 顯示的是當月的即時建議、不是回測部位,兩者若混在一起,
parity 測試會因為設計本身差一個月而永遠紅燈。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# 暖機期:動能腿需要 12 個月才能算 SMA12,在那之前沒有有效訊號
MOM_WARMUP_MONTHS = 12

# --------------------------------------------------------------------------
# 階梯函式(純量)
# --------------------------------------------------------------------------
# 注意:這兩條階梯的門檻方向不同 —— 動能腿用嚴格小於、VIX 腿用嚴格大於。
# 這是刻意對齊 JS 原始實作的語意,邊界值由 test_invariants.py 逐一驗證。


def mom_cash(mom: float) -> float:
    """趨勢動能腿:S&P 對 12 月均線的乖離率 -> 建議現金比重。

    mom >= 0        -> 0.00
    -0.03 <= mom<0  -> 0.30
    -0.08 <= mom    -> 0.60
    -0.13 <= mom    -> 0.90
    mom < -0.13     -> 1.00

    NaN 回傳 0.0,與 JS 的 `NaN < 0 === false` 行為一致。
    實務上 `signal_frame()` 會丟掉暖機期,不會讓 NaN 走到這裡。
    """
    c = 0.0
    if mom < 0:
        c = 0.30
    if mom < -0.03:
        c = 0.60
    if mom < -0.08:
        c = 0.90
    if mom < -0.13:
        c = 1.00
    return c


def vix_cash(risk: float) -> float:
    """VIX 領先腿:風險分數 -> 建議現金比重。

    risk <= 0.5 -> 0.00      risk > 1.8 -> 0.66
    risk > 0.5  -> 0.24      risk > 2.6 -> 0.90
    risk > 1.0  -> 0.42      risk > 3.4 -> 1.00
    """
    c = 0.0
    if risk > 0.5:
        c = 0.24
    if risk > 1.0:
        c = 0.42
    if risk > 1.8:
        c = 0.66
    if risk > 2.6:
        c = 0.90
    if risk > 3.4:
        c = 1.00
    return c


def union_cash(mom_c: float, vix_c: float) -> float:
    """聯集:兩腿取較高者。值域恆在 [0, 1]。"""
    return max(mom_c, vix_c)


# --------------------------------------------------------------------------
# 月頻聚合
# --------------------------------------------------------------------------


def build_vix_monthly(vix_daily: pd.DataFrame) -> pd.DataFrame:
    """VIX 日頻 -> 月頻。

    參數
    ----
    vix_daily : 需含 `DATE`(可轉 datetime)與 `CLOSE`、`HIGH` 欄。

    回傳欄位
    --------
    ym    : 'YYYY-MM'
    close : 該月最後一個交易日的收盤(月收)
    mean  : 該月每日收盤的平均
    max   : 該月的日內最高(用於飆升項)
    n     : 該月的交易日數,< 15 表示該月尚未走完
    """
    df = vix_daily.copy()
    df["DATE"] = pd.to_datetime(df["DATE"])
    df["CLOSE"] = pd.to_numeric(df["CLOSE"], errors="coerce")
    df["HIGH"] = pd.to_numeric(df.get("HIGH", df["CLOSE"]), errors="coerce")
    df = df.dropna(subset=["DATE", "CLOSE"]).sort_values("DATE")

    df["ym"] = df["DATE"].dt.strftime("%Y-%m")
    g = df.groupby("ym", sort=True)
    out = pd.DataFrame(
        {
            "close": g["CLOSE"].last(),
            "mean": g["CLOSE"].mean(),
            # JS 用該月的日內最高;若無 HIGH 欄則退回用收盤的最高
            "max": g["HIGH"].max().fillna(g["CLOSE"].max()),
            "n": g["CLOSE"].size(),
        }
    ).reset_index()
    return out


def sp_monthly(sp_daily: pd.DataFrame) -> pd.DataFrame:
    """S&P 日K -> 月頻【月收盤】序列(ym, price)。這是正式的價格定義。

    參數
    ----
    sp_daily : 需含 `date`(可轉 datetime)與 `close` 欄。

    月收盤 = 該月最後一個交易日的收盤價。這是 12 月均線趨勢濾網的標準構造,
    也是唯一你真的能成交的價格。
    """
    df = sp_daily.copy()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["date"] = pd.to_datetime(df["date"], utc=True, format="mixed")
    df = df[df["close"] > 0].dropna(subset=["date"]).sort_values("date")
    df["ym"] = df["date"].dt.strftime("%Y-%m")
    out = df.groupby("ym", sort=True)["close"].last().reset_index()
    return out.rename(columns={"close": "price"})


def sp_monthly_shiller_avg(shiller: pd.DataFrame) -> pd.DataFrame:
    """【舊定義,保留供對照】Shiller 的 SP500 欄 -> 月頻序列。

    ⚠ Shiller 的 SP500 是「當月每日收盤的平均價」,不是月收盤 —— 已實測確認
    (與 Yahoo 月均價差異精確為 0.00)。兩種定義在 17.3% 的月份會給出不同的
    現金建議,乖離率最大差 10.19 個百分點。

    2026-08 定案改用月收盤。本函式只保留給 annual_review.py 做定義對照,
    以及取用 Shiller 的股息欄。不要拿它產生訊號。
    """
    df = shiller.copy()
    df["price"] = pd.to_numeric(df["SP500"], errors="coerce")
    df["ym"] = pd.to_datetime(df["Date"]).dt.strftime("%Y-%m")
    df = df[df["price"] > 0].dropna(subset=["ym"])
    return df[["ym", "price"]].sort_values("ym").reset_index(drop=True)


# --------------------------------------------------------------------------
# 風險分數與動能
# --------------------------------------------------------------------------


def vix_risk_series(vix_m: pd.DataFrame) -> pd.Series:
    """VIX 月頻 -> 風險分數序列。

        risk = 0.55*max(lvl,0) + 0.30*max(z36,0) + 0.15*max(spike,0)

    lvl   = (close - 16) / 9
    z36   = 36 個月滾動 z-score,樣本標準差(ddof=1);前 35 個月為 0
    spike = (該月日內最高 - 近 6 個月月收均值) / 12;前 5 個月為 0

    暖機期補 0 而非 NaN,是為了對齊 JS。注意 spike 必須「整式補 0」,
    不能只把滾動均值補 0 —— 否則會變成 (max - 0)/12,是完全不同的數。
    """
    close = vix_m["close"].astype(float)
    high = vix_m["max"].astype(float)

    lvl = (close - 16.0) / 9.0

    roll36_mean = close.rolling(36).mean()
    roll36_std = close.rolling(36).std(ddof=1)
    z = (close - roll36_mean) / roll36_std
    z = z.where(roll36_std.notna() & (roll36_std > 0), 0.0)

    roll6_mean = close.rolling(6).mean()
    spike = (high - roll6_mean) / 12.0
    spike = spike.where(roll6_mean.notna(), 0.0)

    risk = (
        0.55 * lvl.clip(lower=0.0)
        + 0.30 * z.clip(lower=0.0)
        + 0.15 * spike.clip(lower=0.0)
    )
    return risk.rename("risk")


def momentum_frame(sp_m: pd.DataFrame) -> pd.DataFrame:
    """S&P 月頻 -> 動能表(ym, price, sma12, mom)。

    sma12 含當月本身(近 12 個月月收均值),mom = price/sma12 - 1。
    前 11 個月為 NaN。
    """
    df = sp_m.copy().reset_index(drop=True)
    df["sma12"] = df["price"].rolling(MOM_WARMUP_MONTHS).mean()
    df["mom"] = df["price"] / df["sma12"] - 1.0
    return df


# --------------------------------------------------------------------------
# 訊號
# --------------------------------------------------------------------------


def signal_frame(sp_m: pd.DataFrame, vix_m: pd.DataFrame) -> pd.DataFrame:
    """合併兩腿,產出逐月訊號表。

    只保留兩個資料源都有的月份,且丟掉動能腿的暖機期(前 11 個月)。

    回傳欄位:ym, price, mom, risk, mom_cash, vix_cash, cash
    其中 `cash` 即建議現金比重,值域 [0, 1]。
    """
    mom_df = momentum_frame(sp_m)
    vix_df = vix_m.copy().reset_index(drop=True)
    vix_df["risk"] = vix_risk_series(vix_df).values

    df = mom_df.merge(vix_df[["ym", "risk", "n"]], on="ym", how="inner")
    df = df.dropna(subset=["mom"]).reset_index(drop=True)

    df["mom_cash"] = df["mom"].map(mom_cash)
    df["vix_cash"] = df["risk"].map(vix_cash)
    df["cash"] = np.maximum(df["mom_cash"], df["vix_cash"])
    return df[["ym", "price", "mom", "risk", "mom_cash", "vix_cash", "cash"]]
