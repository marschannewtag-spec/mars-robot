"""L3 年度健檢 —— 一年跑一次,產出 reports/YYYY-health.md。

**只陳述數字與偏離,不建議調參、不建議加減功能、不判斷市場。**

這個邊界是刻意的。報告會告訴你「現行參數是否仍在穩健區」,但如果它說
「已漂出穩健區」,那個「要不要調整」的決定仍然是你的 —— 一個會自己調參的
系統就是 overfit 機器。

用法:
    python src/annual_review.py                # 產出到 reports/
    python src/annual_review.py --stdout       # 印到畫面
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import backtest  # noqa: E402
import engine  # noqa: E402
import fetch_data  # noqa: E402

OPS_ROOT = Path(__file__).resolve().parents[1]
REPORTS = OPS_ROOT / "reports"
GOLDEN = OPS_ROOT / "tests" / "fixtures" / "golden_metrics.json"
HEALTH_LOG = OPS_ROOT / "data" / "health_log.csv"

# 參數敏感度網格 12 × 12 = 144 格。
# ⚠ 這組定義是本專案自訂的,尚未經使用者確認 —— 見報告中的註記。
#   MA_GRID   : 趨勢腿均線長度(現行 12)
#   MULT_GRID : VIX 階梯門檻的整體乘數(現行 1.0,即不縮放)
MA_GRID = list(range(6, 18))
MULT_GRID = [round(0.6 + 0.1 * i, 1) for i in range(12)]
CURRENT_MA = 12
CURRENT_MULT = 1.0


@dataclass
class Section:
    title: str
    body: str


# --------------------------------------------------------------------------
# 共用
# --------------------------------------------------------------------------


def build_signals(ma_len: int = CURRENT_MA, mult: float = CURRENT_MULT) -> pd.DataFrame:
    """依指定參數重算訊號。mult 縮放 VIX 階梯門檻,ma_len 換趨勢腿均線長度。"""
    sp_m = engine.sp_monthly(fetch_data.load("sp_daily"))
    vix_m = engine.build_vix_monthly(fetch_data.load("vix"))

    df = sp_m.copy().reset_index(drop=True)
    df["sma"] = df["price"].rolling(ma_len).mean()
    df["mom"] = df["price"] / df["sma"] - 1.0

    vix_df = vix_m.copy().reset_index(drop=True)
    vix_df["risk"] = engine.vix_risk_series(vix_df).values

    merged = df.merge(vix_df[["ym", "risk"]], on="ym", how="inner").dropna(subset=["mom"])
    merged = merged.reset_index(drop=True)
    merged["mom_cash"] = merged["mom"].map(engine.mom_cash)
    # 門檻乘上 mult 等價於把分數除以 mult
    merged["vix_cash"] = (merged["risk"] / mult).map(engine.vix_cash)
    merged["cash"] = np.maximum(merged["mom_cash"], merged["vix_cash"])
    return merged


def total_return_perf(sig: pd.DataFrame, start: str, end: str):
    """總報酬 + 現金 0% + shift(1),與黃金基準同一把尺。"""
    sh = fetch_data.load("shiller")
    sh["ym"] = pd.to_datetime(sh["Date"]).dt.strftime("%Y-%m")
    w = backtest.slice_window(sig.merge(sh[["ym", "Dividend"]], on="ym", how="left"),
                              start, end).reset_index(drop=True)
    if len(w) < 24:
        return None, None
    px = w["price"].astype(float)
    div = w["Dividend"].astype(float).fillna(0.0)
    mkt = (px + div / 12.0) / px.shift(1) - 1.0

    def curve(cw: pd.Series) -> pd.Series:
        cw = cw.astype(float).fillna(0.0).clip(0.0, 1.0)
        return (1.0 + ((1.0 - cw) * mkt).fillna(0.0)).cumprod()

    return (backtest.summarize(curve(w["cash"].shift(1)), w["ym"]),
            backtest.summarize(curve(pd.Series(0.0, index=w.index)), w["ym"]))


def pct(x: float) -> str:
    return f"{x * 100:.2f}%"


# --------------------------------------------------------------------------
# 各節
# --------------------------------------------------------------------------


def section_drift(sig: pd.DataFrame) -> Section:
    """基準漂移:重跑全期,對照 CLAUDE.md 記載的黃金基準。"""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    rows = ["對照 `tests/fixtures/golden_metrics.json`。**訊號層任何差異都是 bug**;",
            "績效層的小幅變動可能來自資料修訂。", ""]

    g_win = golden["訊號窗口"]
    rows.append(f"- 訊號窗口:黃金 `{g_win['start']}..{g_win['end']}`({g_win['months']} 月)"
                f" · 現在 `{sig['ym'].iloc[0]}..{sig['ym'].iloc[-1]}`({len(sig)} 月)")

    frozen = sig[sig["ym"] <= g_win["end"]]
    key_bad = []
    idx = sig.set_index("ym")
    for ym, expected in golden["關鍵時點"].items():
        got = float(idx.loc[ym, "cash"]) if ym in idx.index else float("nan")
        mark = "✅" if abs(got - expected) < 1e-9 else "❌"
        if mark == "❌":
            key_bad.append(ym)
        rows.append(f"- {mark} 關鍵時點 `{ym}`:黃金 {expected:.0%} · 現在 {got:.0%}")

    changes = int((frozen["cash"].diff().abs() > 1e-9).sum())
    rows.append(f"- 水位變動次數(凍結窗口內):黃金 {golden['水位變動次數']} · 現在 {changes}")

    start, end = golden["績效"]["窗口"].split("..")
    st, bh = total_return_perf(sig, start, end)
    if st:
        g = golden["績效"]["策略"]
        rows += ["", f"績效({golden['績效']['假設']},{golden['績效']['窗口']}):", "",
                 "| 指標 | 黃金基準 | 現在 | 差 |", "|---|---|---|---|"]
        for k, label in [("cagr", "CAGR"), ("max_drawdown", "MaxDD"), ("calmar", "Calmar")]:
            now = getattr(st, k)
            rows.append(f"| {label} | {g[k]:.4f} | {now:.4f} | {now - g[k]:+.4f} |")

    if key_bad:
        rows += ["", f"> ⚠ **關鍵時點漂移:{key_bad}**。訊號層不該有任何差異 —— "
                 "先確認資料源是否改了定義,再確認 `engine.py` 是否被改動。"]
    return Section("基準漂移", "\n".join(rows))


def section_recent(sig: pd.DataFrame) -> Section:
    """過去 12 個月的實績。"""
    last12 = sig.tail(12)
    rows = ["| 月份 | 現金 | 趨勢腿 | VIX腿 | 乖離 | 風險分數 |", "|---|---|---|---|---|---|"]
    for _, r in last12.iterrows():
        rows.append(f"| {r['ym']} | **{r['cash']:.0%}** | {r['mom_cash']:.0%} | "
                    f"{r['vix_cash']:.0%} | {r['mom'] * 100:+.2f}% | {r['risk']:.3f} |")
    changes = int((last12["cash"].diff().abs() > 1e-9).sum())
    rows += ["", f"- 水位變動次數:**{changes}** 次(全期平均每 12 個月 "
             f"{(sig['cash'].diff().abs() > 1e-9).sum() / len(sig) * 12:.1f} 次)",
             f"- 平均現金水位:**{last12['cash'].mean():.1%}**(全期 {sig['cash'].mean():.1%})",
             f"- 完全投入月數:**{int((last12['cash'] == 0).sum())}/12**"]
    return Section("過去 12 個月實績", "\n".join(rows))


def section_extremes(sig: pd.DataFrame) -> Section:
    """極端值:當前讀數在歷史分布的哪個位置。"""
    last = sig.iloc[-1]
    rows = ["當前讀數在 1990 年以來分布中的百分位。落在前/後 5% 值得看一眼 ——",
            "**這只是描述位置,不是預測**。", "",
            "| 指標 | 現值 | 百分位 | 註記 |", "|---|---|---|---|"]
    for col, label, hi_is_extreme in [("risk", "VIX 風險分數", True),
                                      ("mom", "S&P 對均線乖離", False)]:
        v = last[col]
        pctile = float((sig[col] < v).mean() * 100)
        note = ""
        if hi_is_extreme and pctile >= 95:
            note = "⚠ 歷史前 5%"
        elif not hi_is_extreme and pctile <= 5:
            note = "⚠ 歷史後 5%"
        elif not hi_is_extreme and pctile >= 95:
            note = "歷史前 5%(乖離偏高)"
        rows.append(f"| {label} | {v:.4f} | {pctile:.1f} | {note} |")
    return Section("極端值檢查", "\n".join(rows))


def section_data_health() -> Section:
    """資料源穩定度:過去一年每日健檢的成功率。"""
    if not HEALTH_LOG.exists():
        return Section("資料源穩定度",
                       "尚無 `data/health_log.csv` —— 每日健檢還沒累積足夠紀錄。\n"
                       "這一節要等每日流程跑滿一段時間才有內容。")
    log = pd.read_csv(HEALTH_LOG)
    log["date"] = pd.to_datetime(log["date"])
    cutoff = log["date"].max() - pd.Timedelta(days=365)
    recent = log[log["date"] >= cutoff]
    rows = [f"- 紀錄區間:{recent['date'].min().date()} .. {recent['date'].max().date()}"
            f"({len(recent)} 天)",
            f"- 全綠天數:**{int(recent['ok'].sum())}/{len(recent)}** "
            f"({recent['ok'].mean() * 100:.1f}%)"]
    fails = recent[recent["ok"] == 0]["failed"].dropna()
    if len(fails):
        counter: dict[str, int] = {}
        for row in fails:
            for name in str(row).split("|"):
                if name:
                    counter[name] = counter.get(name, 0) + 1
        rows += ["", "最常失敗的檢查項:", ""]
        for name, n in sorted(counter.items(), key=lambda x: -x[1])[:8]:
            rows.append(f"- `{name}` — {n} 天")
    else:
        rows.append("- 過去一年沒有任何失敗紀錄")
    return Section("資料源穩定度", "\n".join(rows))


def section_sensitivity() -> Section:
    """參數敏感度:現行參數在 144 格網格中的位置。"""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    start, end = golden["績效"]["窗口"].split("..")

    grid = []
    for ma in MA_GRID:
        for mult in MULT_GRID:
            try:
                sig = build_signals(ma, mult)
                st, _ = total_return_perf(sig, start, end)
                if st and np.isfinite(st.calmar):
                    grid.append({"ma": ma, "mult": mult, "calmar": st.calmar,
                                 "cagr": st.cagr, "mdd": st.max_drawdown})
            except Exception:  # noqa: BLE001, S110 — 個別格失敗不該中斷整份報告
                continue

    if not grid:
        return Section("參數敏感度", "網格計算失敗。")

    g = pd.DataFrame(grid)
    cur = g[(g["ma"] == CURRENT_MA) & (g["mult"] == CURRENT_MULT)]
    cur_calmar = float(cur["calmar"].iloc[0]) if len(cur) else float("nan")
    pctile = float((g["calmar"] < cur_calmar).mean() * 100)

    # 鄰域穩健性:現行參數周圍 8 格的 Calmar 離散程度。
    # 高原(鄰域都不錯)比尖峰(只有這一格好)可信得多。
    neigh = g[(g["ma"].between(CURRENT_MA - 1, CURRENT_MA + 1))
              & (g["mult"].between(CURRENT_MULT - 0.1, CURRENT_MULT + 0.1))]
    best = g.loc[g["calmar"].idxmax()]

    rows = [
        f"網格:MA ∈ {MA_GRID[0]}–{MA_GRID[-1]} × VIX 門檻乘數 ∈ "
        f"{MULT_GRID[0]}–{MULT_GRID[-1]},共 {len(g)} 格有效。",
        f"評分窗口 `{start}..{end}`,指標為 Calmar(總報酬 + 現金 0% + shift(1))。",
        "",
        f"- 現行參數 `MA={CURRENT_MA} / mult={CURRENT_MULT}`:Calmar **{cur_calmar:.3f}**,"
        f"位於全網格 **第 {pctile:.0f} 百分位**",
        f"- 鄰域(±1 格)8 鄰居:Calmar 平均 {neigh['calmar'].mean():.3f}、"
        f"標準差 {neigh['calmar'].std():.3f}、最小 {neigh['calmar'].min():.3f}",
        f"- 全網格最佳:`MA={int(best['ma'])} / mult={best['mult']}` → Calmar {best['calmar']:.3f}",
        "",
        "> 判讀方式:現行參數若落在**高原**(鄰域離散度小、百分位不低),代表它不是",
        "> 靠運氣選中的孤立尖峰。若鄰域離散度大、或百分位明顯下滑,代表參數面變陡了。",
        "> **本報告不建議是否調整** —— 那是你的決定。會自己調參的系統就是 overfit 機器。",
        "",
        "⚠ 這組網格定義(MA 6–17 × 乘數 0.6–1.7)是本專案自訂的,",
        "尚未與原始的 `MA12 / lvl1.2` 參數化方式對應確認。若你手上有原始定義,",
        "請更新 `annual_review.py` 的 `MA_GRID` / `MULT_GRID`。",
    ]
    return Section("參數敏感度(144 格網格)", "\n".join(rows))


def section_oos(sig: pd.DataFrame) -> Section:
    """樣本外延伸到最新資料。"""
    rows = ["樣本內為 1990–2010(參數選擇期),其餘為樣本外。",
            "報酬假設與黃金基準相同(總報酬 + 現金 0% + shift(1))。", "",
            "| 窗口 | 策略 CAGR | 策略 MaxDD | 策略 Calmar | BH Calmar | 策略勝出 |",
            "|---|---|---|---|---|---|"]
    sh = fetch_data.load("shiller")
    sh["ym"] = pd.to_datetime(sh["Date"]).dt.strftime("%Y-%m")
    div_end = sh[pd.to_numeric(sh["Dividend"], errors="coerce").fillna(0) > 0]["ym"].max()

    for label, s, e in [("樣本內 1990–2010", "1990-01", "2010-12"),
                        ("樣本外 2011–迄今", "2011-01", div_end),
                        ("全期", "1990-01", div_end)]:
        st, bh = total_return_perf(sig, s, e)
        if not st:
            continue
        win = "✅" if st.calmar > bh.calmar else "❌"
        rows.append(f"| {label} | {pct(st.cagr)} | {pct(st.max_drawdown)} | "
                    f"{st.calmar:.3f} | {bh.calmar:.3f} | {win} |")
    rows += ["", f"> 窗口上限為 `{div_end}` —— Shiller 的股息欄自 2023-07 起未發布,",
             "> 再往後就算不出總報酬。這是資料限制,不是策略表現的變化。"]
    return Section("樣本外延伸", "\n".join(rows))


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def build_report() -> str:
    sig = build_signals()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sections = [
        section_drift(sig),
        section_recent(sig),
        section_extremes(sig),
        section_data_health(),
        section_sensitivity(),
        section_oos(sig),
    ]
    out = [
        f"# 現金水位儀 · 年度健檢 {datetime.now(timezone.utc).year}",
        "",
        f"產出時間:{stamp} · 資料涵蓋 `{sig['ym'].iloc[0]}` .. `{sig['ym'].iloc[-1]}`",
        "",
        "> 本報告**只陳述數字與偏離**,不建議調參、不建議加減功能、不判斷市場。",
        "> 若某節指出參數已漂出穩健區,那個「要不要調整」的決定屬於你。",
        "",
        "---",
        "",
    ]
    for i, s in enumerate(sections, 1):
        out += [f"## {i}. {s.title}", "", s.body, "", "---", ""]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="年度健檢報告")
    ap.add_argument("--stdout", action="store_true", help="印到畫面而非寫檔")
    args = ap.parse_args()

    report = build_report()
    if args.stdout:
        print(report)
        return 0

    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / f"{datetime.now(timezone.utc).year}-health.md"
    path.write_text(report, encoding="utf-8", newline="\n")
    print(f"已產出 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
