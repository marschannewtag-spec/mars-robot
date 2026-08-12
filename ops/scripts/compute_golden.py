"""一次性:算出真實的黃金基準,供人工確認後寫入 CLAUDE.md。

刻意不自動寫檔 —— 黃金基準是回歸測試的錨,必須由人看過並確認,
否則測試保護的只是「上次跑出來的數字」,而不是「正確的數字」。

價格定義:月收盤(Yahoo ^GSPC 日K 聚合)。2026-08 定案,取代原本的
Shiller 月均價 —— 兩者在 17.3% 的月份給出不同現金建議。
績效假設:總報酬(Shiller 股息)+ 現金收益 0% + shift(1)。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import backtest  # noqa: E402
import engine  # noqa: E402
import fetch_data  # noqa: E402
from console import force_utf8  # noqa: E402

KEY_MONTHS = ["2008-10", "2015-08", "2020-03", "2022-06"]
# 股息只發布到 2023-06,績效窗口就切在那裡 —— 用不完整的輸入算出來的錨會漂
PERF_END = "2023-06"


def pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def total_return_perf(df: pd.DataFrame) -> tuple:
    """總報酬 + 現金 0% + shift(1)。回傳 (策略, 買進持有)。"""
    px = df["price"].astype(float)
    div = df["Dividend"].astype(float).fillna(0.0)
    mkt = (px + div / 12.0) / px.shift(1) - 1.0

    def curve(cash_w: pd.Series) -> pd.Series:
        w = cash_w.astype(float).fillna(0.0).clip(0.0, 1.0)
        return (1.0 + ((1.0 - w) * mkt).fillna(0.0)).cumprod()

    eq_s = curve(df["cash"].shift(1))
    eq_b = curve(pd.Series(0.0, index=df.index))
    return (
        backtest.summarize(eq_s, df["ym"]),
        backtest.summarize(eq_b, df["ym"]),
    )


def main() -> None:
    force_utf8()
    sp_daily = fetch_data.load("sp_daily")
    vix = fetch_data.load("vix")
    shiller = fetch_data.load("shiller")

    sp_m = engine.sp_monthly(sp_daily)          # 月收盤
    vix_m = engine.build_vix_monthly(vix)
    sig = engine.signal_frame(sp_m, vix_m)

    sh = shiller.copy()
    sh["ym"] = pd.to_datetime(sh["Date"]).dt.strftime("%Y-%m")
    sig = sig.merge(sh[["ym", "Dividend"]], on="ym", how="left")

    print("=" * 78)
    print("第一層 · 訊號錨(零假設,精確比對)")
    print("=" * 78)
    print(f"窗口      : {sig['ym'].iloc[0]} .. {sig['ym'].iloc[-1]}  共 {len(sig)} 個月")
    print(f"價格定義  : 月收盤(Yahoo ^GSPC 日K 聚合)")
    print()
    print("關鍵時點:")
    idx = sig.set_index("ym")
    for ym in KEY_MONTHS:
        if ym not in idx.index:
            print(f"  {ym}  — 資料中沒有這個月")
            continue
        r = idx.loc[ym]
        print(f"  {ym}  現金={r['cash'] * 100:>5.1f}%   "
              f"趨勢腿={r['mom_cash'] * 100:>5.1f}% (乖離 {r['mom'] * 100:+7.2f}%)   "
              f"VIX腿={r['vix_cash'] * 100:>5.1f}% (分數 {r['risk']:.3f})")

    print()
    print("水位分布:")
    for lvl, cnt in sig["cash"].value_counts().sort_index().items():
        print(f"  {lvl * 100:>5.1f}% : {cnt:>4} 個月 ({cnt / len(sig) * 100:>5.1f}%)")
    changes = int((sig["cash"].diff().abs() > 1e-9).sum())
    full = idx[idx["cash"] >= 1.0]
    print(f"\n水位變動次數: {changes}")
    print(f"達 100% 現金: {len(full)} 個月 — {', '.join(full.index.tolist())}")

    print()
    print("=" * 78)
    print(f"第二層 · 績效錨(總報酬 + 現金 0% + shift(1),窗口切至 {PERF_END})")
    print("=" * 78)
    w = backtest.slice_window(sig, "1990-01", PERF_END)
    st, bh = total_return_perf(w.reset_index(drop=True))
    print(f"窗口      : {st.start_ym} .. {st.end_ym}  共 {st.months} 個月報酬")
    print(f"{'':12}{'CAGR':>10}{'MaxDD':>11}{'Calmar':>10}")
    print(f"{'策略':<12}{pct(st.cagr):>10}{pct(st.max_drawdown):>11}{st.calmar:>10.3f}")
    print(f"{'買進持有':<11}{pct(bh.cagr):>10}{pct(bh.max_drawdown):>11}{bh.calmar:>10.3f}")

    print()
    print("樣本內/外分割:")
    for label, s, e in [("IS 1990-2010", "1990-01", "2010-12"),
                        ("OOS 2011-2023", "2011-01", PERF_END)]:
        ww = backtest.slice_window(sig, s, e).reset_index(drop=True)
        a, b = total_return_perf(ww)
        print(f"  {label:<15} 策略 {pct(a.cagr):>8} / {pct(a.max_drawdown):>8} / "
              f"{a.calmar:>6.3f}    BH {pct(b.cagr):>8} / {pct(b.max_drawdown):>8} / "
              f"{b.calmar:>6.3f}")


if __name__ == "__main__":
    main()
