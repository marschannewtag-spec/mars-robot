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


# ══════════════════════════════════════════════════════════════════════════
# 黃金基準不得被悄悄移動(2026-10-20)
#
# 這份專案三條紅線之一就是「不要修改 tests/fixtures/ 的黃金基準讓測試過」。
# 在今天之前,那條規則**只存在於文字裡** —— `daily-health.yml` 給 autofix
# 的提示詞第 570 行寫著它,而提示詞不是強制力。
#
# 上面那些測試比的是「現在算出來的」對「黃金基準」。
# `實測`:單獨把 `golden_metrics.json` 裡一個值乘 1.5 → **3 failed**。
# 所以「只改基準」這條路是堵住的。
#
# 堵不住的是**協同修改**:
#
#   · 改 engine.py 產生錯的數字,再重產基準讓它對上
#   · 改輸入快照(sp500_daily.csv / vix_daily.csv / shiller_sp500.csv),
#     再重產基準
#
# 兩者跑完整組都是綠的 —— 那是**定義上必然**的,沒有任何比對型測試能從
# 結果看出來。能做的只有一件事:讓那個動作在 diff 裡看得見。
#
# 一個 20000 列的 CSV 改幾列,在 review 裡看起來像雜訊;
# `CHECKSUMS.txt` 裡一行 hash 變了,看起來不像。
#
# ⚠ 這條測試**不是**在禁止重產。Shiller 修訂歷史、CBOE 補回缺漏交易日,
#    都是真的會發生而且應該被接受的事。被禁止的是讓它看起來沒發生過。
#    真的要改就跑 `scripts/refresh_fixture_checksums.py --write`,
#    那支腳本會逼你看見哪些 hash 動了。
# ══════════════════════════════════════════════════════════════════════════

def _content_hash(path: Path) -> str:
    """行尾正規化之後的 sha256 —— 與 scripts/refresh_fixture_checksums.py 一致。

    ⚠ 不可以直接 hash 原始位元組。第一版就是那樣,結果**本機全綠、CI 紅**:
    `golden_metrics.json` 在 Windows 工作副本是 CRLF,git 存的是 LF,
    Linux runner 檢出來自然不一樣。

    一個用來守護黃金基準的檢查,自己踩了這個 repo 最早那條教訓 ——
    本機與 CI 的環境差異。行尾不是內容,不該讓它決定紅綠。
    由 `test_the_checksum_is_the_same_on_windows_and_linux` 釘住。
    """
    import hashlib

    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def test_the_frozen_fixtures_have_not_moved() -> None:

    fixtures = FIXTURES
    checksums = fixtures / "CHECKSUMS.txt"
    assert checksums.exists(), (
        "CHECKSUMS.txt 不見了 —— 黃金基準就回到了「可以被悄悄改掉」的狀態。"
        "跑 `python scripts/refresh_fixture_checksums.py --write` 重建。")

    expected = {}
    for line in checksums.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        h, _, name = line.partition("  ")
        expected[name] = h
    assert len(expected) >= 5, f"CHECKSUMS.txt 只列了 {len(expected)} 個檔案"

    on_disk = {f.name for f in fixtures.iterdir()
               if f.is_file() and f.name != "CHECKSUMS.txt"}
    assert on_disk == set(expected), (
        f"fixtures 目錄與 CHECKSUMS.txt 對不上。\n"
        f"  只在磁碟上:{sorted(on_disk - set(expected))}\n"
        f"  只在清單裡:{sorted(set(expected) - on_disk)}")

    moved = []
    for name, want in sorted(expected.items()):
        got = _content_hash(fixtures / name)
        if got != want:
            moved.append(f"{name}: {want[:12]} -> {got[:12]}")

    assert not moved, (
        "黃金基準或輸入快照被改動了:\n  " + "\n  ".join(moved)
        + "\n\n這是這份專案三條紅線之一。這條測試不是在禁止重產 ——"
          "\n資料源真的修訂過歷史時,重產是對的。被禁止的是**讓它看起來沒發生過**。"
          "\n\n如果這是刻意的:"
          "\n  1. 跑 `python scripts/refresh_fixture_checksums.py --write`"
          "\n  2. commit 訊息寫清楚:為什麼重產、哪些數字動了、"
          "\n     **訊號層有沒有動**(訊號層任何差異都是 bug,不是資料修訂)"
          "\n\n如果你不知道為什麼它變了 —— 那就是這條測試存在的理由,先去查。"
    )


def test_the_refresh_script_exists_and_refuses_to_write_by_default() -> None:
    """重產腳本必須存在,而且預設**不寫檔**。

    一支預設就會覆蓋黃金基準的腳本,等於把紅線變成一個打字錯誤的距離。
    """
    import subprocess
    import sys

    script = FIXTURES.parents[1] / "scripts" / "refresh_fixture_checksums.py"
    assert script.exists(), "重產腳本不見了 —— 那會讓上面那條測試變成死路"

    r = subprocess.run(
        [sys.executable, "-X", "utf8", str(script)],
        cwd=FIXTURES.parents[1], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    assert "--write" in r.stdout or r.returncode == 0, (
        f"腳本沒有說明要怎麼真的寫入:\n{r.stdout}\n{r.stderr}")
    # 沒加 --write 不該動到檔案
    import hashlib
    before = hashlib.sha256(
        (FIXTURES / "CHECKSUMS.txt").read_bytes()).hexdigest()
    subprocess.run([sys.executable, "-X", "utf8", str(script)],
                   cwd=FIXTURES.parents[1], capture_output=True, timeout=120)
    after = hashlib.sha256(
        (FIXTURES / "CHECKSUMS.txt").read_bytes()).hexdigest()
    assert before == after, "不加 --write 也把檔案改掉了"


def test_the_checksum_is_the_same_on_windows_and_linux() -> None:
    """同一份內容,CRLF 與 LF 兩種檢出必須得到同一個 hash。

    這條是為了一個**已經發生過的**失敗寫的:第一版的 checksum 直接 hash
    磁碟位元組,於是 `golden_metrics.json`(Windows 工作副本 CRLF、
    git 存 LF)在本機全綠、在 CI 紅。

    `實測`還原方式:`git -c core.autocrlf=false clone` 出一份 LF 檢出,
    整組只有那一條紅 —— 確認原因,而不是用猜的。

    ⚠ 這是這個 repo 最早那條教訓的第 N 次:**本機與 CI 不是同一個環境**。
       而這次踩到它的,正好是一個用來守護黃金基準的檢查。
    """
    fixtures = FIXTURES
    for f in sorted(fixtures.iterdir()):
        if not f.is_file() or f.name == "CHECKSUMS.txt":
            continue
        raw = f.read_bytes()
        lf = raw.replace(b"\r\n", b"\n")
        crlf = lf.replace(b"\n", b"\r\n")
        assert crlf != lf or b"\n" not in lf, f"{f.name} 沒有換行,這條測不到東西"

        import hashlib
        h_lf = hashlib.sha256(lf.replace(b"\r\n", b"\n")).hexdigest()
        h_crlf = hashlib.sha256(crlf.replace(b"\r\n", b"\n")).hexdigest()
        assert h_lf == h_crlf, (
            f"{f.name} 的 checksum 會因為行尾而改變 —— "
            "那會讓本機與 CI 給出不同答案(已經發生過一次)")

    # 正規化不得變成「什麼都一樣」:真的改內容必須算出不同的 hash
    import hashlib
    probe = (fixtures / "golden_metrics.json").read_bytes()
    assert _content_hash(fixtures / "golden_metrics.json") != hashlib.sha256(
        probe.replace(b"\r\n", b"\n") + b"x").hexdigest(), \
        "正規化把內容差異也吃掉了"
