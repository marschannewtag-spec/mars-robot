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

from conftest import OPS_ROOT

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


# --------------------------------------------------------------------------
# 執行層工具:check_pine_parity.py 的時間欄解析
#
# 2026-08-12 實測踩到:SP:SPX 的月線可回溯到 1871,匯出的第一列時間戳是
# **負的** unix 秒(-3121407238)。原本用 `\d{9,12}` 判斷是不是數字,負號
# 不符合 → 落到字串解析 → 整支腳本直接拋 ValueError。
#
# 這個 bug 只有在「照文件指示把歷史全部載入」時才會出現 ——
# 也就是【越照著做越會踩到】,而文件正是那樣教的。
# --------------------------------------------------------------------------


def _to_ym():
    import importlib.util

    path = OPS_ROOT / "scripts" / "check_pine_parity.py"
    spec = importlib.util.spec_from_file_location("check_pine_parity", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.to_ym


def test_to_ym_handles_pre_epoch_timestamps() -> None:
    """1871 年的月線 bar 是負的 epoch 秒,必須解析得出來。"""
    to_ym = _to_ym()
    out = to_ym(pd.Series([-3121407238, -3118728838, 1785763800]))
    assert list(out) == ["1871-02", "1871-03", "2026-08"]


def test_to_ym_handles_an_all_negative_export() -> None:
    """整段都在 1970 之前(只匯出早期區間)時,量級判斷必須取絕對值。

    少了 abs(),max() 會是負數、判斷失敗,整份 CSV 落到字串解析後爆掉。
    上一條測試抓不到這個 —— 它的序列裡有一個正數把 max() 撐過門檻了。
    """
    to_ym = _to_ym()
    out = to_ym(pd.Series([-3121407238, -3118728838, -3116050438]))
    assert list(out) == ["1871-02", "1871-03", "1871-04"]


def test_to_ym_still_handles_positive_epoch_and_iso() -> None:
    to_ym = _to_ym()
    assert list(to_ym(pd.Series([1785763800, 1783085400]))) == ["2026-08", "2026-07"]
    assert list(to_ym(pd.Series(["2026-08-03", "2026-07-01"]))) == ["2026-08", "2026-07"]


def test_to_ym_does_not_mistake_bare_years_for_timestamps() -> None:
    """量級門檻:`2026` 這種**純數字**年份不可以被當成 epoch 秒。

    少了門檻,to_numeric 會成功、notna().all() 為真,於是 2026 秒被解讀成
    1970-01 —— 全表變成同一個月,對帳結果一片綠燈卻毫無意義。
    刻意用不含連字號的輸入,否則 to_numeric 會回 NaN、根本走不到那個判斷。
    """
    to_ym = _to_ym()
    assert list(to_ym(pd.Series([2026, 2025]))) == ["2026-01", "2025-01"]


# --------------------------------------------------------------------------
# vixSym 是這份腳本裡唯一能改變訊號的輸入(2026-10-20)
#
# 原本的 tooltip 寫「歷史較長的替代來源:TVC:VIX」。那句話:
#
#   1. 沒有人量過 —— 這個 repo 無法查證 TradingView 上哪個代號歷史比較長
#   2. 方向是反的 —— CBOE 官方 VIX 日K 本來就從 1990-01-02 開始
#      (`實測` data/raw/vix_daily.csv 9296 列),與黃金基準的 440 個月同一個
#      起點。「比它更長」只可能來自另一種建構方式(VXO 或回推),
#      那是另一條序列,不是同一條的延長
#   3. 它在**鼓勵**使用者去改那個輸入,卻沒說改了會發生什麼:
#      飆升項吃的是月內最高,不同代號的 high 不保證一致 ——
#      換掉之後數字會變,而畫面上看不出來
#
# 這條測試把 tooltip 綁在**量得到的事實**上:起始日期必須真的是本機資料的
# 起始日期。哪天資料範圍變了而文案沒跟上,這裡就會紅。
# --------------------------------------------------------------------------

def test_the_vix_symbol_input_says_what_changing_it_costs() -> None:
    import re

    src = (OPS_ROOT / "pine" / "CashWeightGauge.pine").read_text(encoding="utf-8")
    m = re.search(r'vixSym\s*=\s*input\.symbol\("([^"]+)"[^)]*tooltip="([^"]*)"',
                  src, re.DOTALL)
    assert m, "找不到 vixSym 的 input.symbol(...tooltip=...)"
    default, tip = m.group(1), m.group(2)

    assert default == "CBOE:VIX", (
        f"預設來源變成 {default!r} —— 黃金基準是用 CBOE 官方日K 算的,"
        "換預設等於換掉整條 VIX 腿的資料")

    # ⚠ 判準不能只找「最高」two 個字 —— tooltip 裡還有一處「含日內最高」
    #    是在描述預設來源,拿掉風險那句話之後它仍然在,於是測試照樣綠。
    #    實測:突變 M91(刪掉整句風險說明)第一版**逃掉**了。
    #    判準要對準那個【宣稱】,不是對準某個字。
    assert "不保證一致" in tip, (
        "tooltip 沒說換代號的真正代價:飆升項吃的是【月內最高】,"
        f"而不同代號的 high 不保證一致 —— {tip!r}")
    assert "check_pine_parity" in tip, (
        f"tooltip 沒告訴使用者換完要怎麼驗證有沒有漂移:{tip!r}")
    assert "歷史較長" not in tip, (
        "tooltip 又在推薦一個「歷史較長」的替代來源 —— "
        f"那是沒有量過的宣稱,而且方向是反的:{tip!r}")


def test_the_tooltip_start_date_is_the_real_start_date(vix_daily) -> None:
    """tooltip 寫的起始日期,必須就是本機 VIX 資料真正的第一天。

    文案一旦和資料脫鉤就會慢慢變成謊言,而這份專案已經記過三次
    「我的描述和程式碼分岔」。綁在量得到的東西上,它就不會。
    """
    import re

    import pandas as pd

    src = (OPS_ROOT / "pine" / "CashWeightGauge.pine").read_text(encoding="utf-8")
    m = re.search(r'vixSym\s*=\s*input\.symbol\([^)]*tooltip="([^"]*)"',
                  src, re.DOTALL)
    assert m, "找不到 tooltip"
    claimed = re.search(r"(\d{4}-\d{2}-\d{2})", m.group(1))
    assert claimed, f"tooltip 沒有寫出起始日期:{m.group(1)!r}"

    # 用凍結 fixture,不是 data/raw —— 後者不進版控,CI 上根本不存在
    actual = str(pd.to_datetime(vix_daily[vix_daily.columns[0]]).min().date())
    assert claimed.group(1) == actual, (
        f"tooltip 說 VIX 從 {claimed.group(1)} 開始,"
        f"實際資料從 {actual} 開始")
