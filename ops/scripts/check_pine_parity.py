"""Pine 一致性比對 —— 三方中唯一無法自動化的那一份。

TradingView 是封閉環境,CI 跑不了 Pine。所以流程是人工的,但只有一次:

1. 在 TradingView 開 **SP:SPX** 的 **月線(1M)** 圖表
2. 套用 `ops/pine/CashWeightGauge.pine`
3. 右上角資訊表確認顯示「月線」而非紅色警告
4. 選單 → 匯出圖表資料 → CSV
5. 執行:

       python ops/scripts/check_pine_parity.py <匯出的.csv>

差異 > 1e-9 即視為漂移。腳本會列出不一致的月份與兩邊的值。

這支腳本補的是**執行層**。邏輯層已由 `tests/pine_sim.py` 自動驗證
(從 .pine 原始檔解析常數與階梯,用 Pine 語義重現後與 engine.py 逐月比對,
CI 每次都跑)。所以這裡真正要確認的是那些離線驗不到的事:

  · .pine 編譯得過嗎(語法、型別、Pine 版本差異)
  · request.security 的實際行為是否如假設
  · TradingView 的 VIX/SPX 資料是否與 Yahoo/GitHub 來源一致

⚠ 在跑過這支腳本之前,Pine 的狀態是「邏輯已驗證、執行未驗證」。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import engine  # noqa: E402
import fetch_data  # noqa: E402
from console import force_utf8  # noqa: E402

TOL = 1e-9
# TradingView 匯出的欄名就是 plot 的標題
CASH_COL_HINTS = ["建議現金比重", "cash", "現金"]


def find_column(df: pd.DataFrame, hints: list[str], what: str) -> str:
    for h in hints:
        for c in df.columns:
            if h.lower() in str(c).lower():
                return c
    raise SystemExit(
        f"在匯出檔中找不到{what}欄。現有欄位:{list(df.columns)}\n"
        f"可用 --cash-column 手動指定。"
    )


def to_ym(series: pd.Series) -> pd.Series:
    """TradingView 的時間欄可能是 ISO 字串或 unix 秒。"""
    s = series.astype(str)
    if s.str.fullmatch(r"\d{9,12}").all():
        return pd.to_datetime(series.astype("int64"), unit="s", utc=True).dt.strftime("%Y-%m")
    return pd.to_datetime(s, utc=True, format="mixed").dt.strftime("%Y-%m")


def main() -> int:
    force_utf8()  # Windows 的 cp950 印不出 ⚠✓✗,不設會直接 UnicodeEncodeError
    ap = argparse.ArgumentParser(description="比對 TradingView 匯出的 Pine 輸出與 engine.py")
    ap.add_argument("csv", help="TradingView 匯出的 CSV")
    ap.add_argument("--cash-column", help="現金比重欄名(預設自動偵測)")
    ap.add_argument("--time-column", help="時間欄名(預設取第一欄)")
    args = ap.parse_args()

    exported = pd.read_csv(args.csv)
    time_col = args.time_column or exported.columns[0]
    cash_col = args.cash_column or find_column(exported, CASH_COL_HINTS, "現金比重")

    pine = pd.DataFrame({
        "ym": to_ym(exported[time_col]),
        # Pine 畫的是百分比(0–100),engine 是比例(0–1)
        "cash": pd.to_numeric(exported[cash_col], errors="coerce") / 100.0,
    }).dropna().drop_duplicates(subset="ym", keep="last").set_index("ym")

    py = engine.signal_frame(
        engine.sp_monthly(fetch_data.load("sp_daily")),
        engine.build_vix_monthly(fetch_data.load("vix")),
    ).set_index("ym")

    common = pine.index.intersection(py.index)
    print(f"Pine 匯出 {len(pine)} 個月,engine.py {len(py)} 個月,交集 {len(common)} 個月")
    if len(common) < 24:
        print("⚠ 交集不足 24 個月 —— 匯出區間太短,或圖表不是月線。比對意義不大。")
        return 2

    only_pine = sorted(set(pine.index) - set(py.index))[:5]
    only_py = sorted(set(py.index) - set(pine.index))[-5:]
    if only_pine:
        print(f"  只有 Pine 有的月份(前5):{only_pine}")
    if only_py:
        print(f"  只有 engine 有的月份(後5):{only_py}")

    diff = (pine.loc[common, "cash"] - py.loc[common, "cash"]).abs()
    bad = diff[diff > TOL]

    print(f"\n最大絕對差:{diff.max():.6g}(容差 {TOL})")
    if bad.empty:
        print(f"✓ 三方一致 —— Pine 與 engine.py 在 {len(common)} 個月上完全吻合。")
        return 0

    print(f"✗ {len(bad)} 個月不一致:\n")
    print(f"{'月份':<10}{'Pine':>9}{'engine.py':>12}")
    for ym in bad.index[:30]:
        print(f"{ym:<10}{pine.loc[ym, 'cash'] * 100:>8.1f}%{py.loc[ym, 'cash'] * 100:>11.1f}%")
    if len(bad) > 30:
        print(f"… 另外 {len(bad) - 30} 個月")

    # 把「資料時點差異」和「真正的邏輯漂移」分開 —— 兩者的處理方式完全不同,
    # 混在一起會讓人把前者誤判成 bug,浪費一整個下午。
    tail_two = set(sorted(common)[-2:])
    if set(bad.index) <= tail_two:
        print("\n▲ 所有差異都集中在最後兩個月。")
        print("  這**很可能不是 bug** —— TradingView 的當月是進行中的 bar,")
        print("  會隨盤中報價一直變,而 engine.py 用的是有固定截止點的快照。")
        print("  判斷方式:等當月收完再匯出一次;若歷史月份全部吻合,就是資料時點差異。")
        return 1

    early = [ym for ym in bad.index if ym < sorted(common)[36]]
    if early and len(early) == len(bad):
        print("\n▲ 差異全部落在最前面 36 個月(z-score 暖機期)。")
        print("  常見原因是圖表載入的歷史不夠長 —— TradingView 只匯出【已載入】的 bar,")
        print("  暖機期的滾動視窗因此吃不到足夠資料。往回捲到 1990 之前再匯出一次。")
        return 1

    print("\n差異散布在歷史各處,比較可能是真的漂移。常見原因:")
    print("  · 圖表不是月線 —— 所有滾動窗口長度會全錯(指標右上角會顯示紅色警告)")
    print("  · VIX 來源符號不同(CBOE:VIX vs TVC:VIX)歷史起點與數值可能不同")
    print("  · TradingView 的 SPX 序列與 Yahoo ^GSPC 有調整差異")
    print("  · .pine 被改過但 ops/src/engine.py 沒跟著改(或反過來)")
    print("\n下一步:先跑 `python -m pytest ops/tests/test_pine_sim.py -v`。")
    print("  那一層若也失敗 → 是 .pine 的邏輯真的與 engine.py 不一致。")
    print("  那一層若通過 → 邏輯是對的,問題出在 TradingView 的執行環境或資料源。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
