"""Pine 語義模擬器 —— 在沒有 TradingView 的情況下驗證 CashWeightGauge.pine。

**這不是重寫一份實作。** 常數、權重、階梯門檻全部從 `.pine` 原始檔**解析出來**,
所以測的是那個檔案本身;有人改了 Pine 裡的任何一個數字,這裡就會抓到。

實作的是 Pine 的語義而非 pandas 的語義:
  · `ta.sma(src, n)` 在視窗內含 na 時回傳 na(不是跳過 na)
  · `src[i]` 是往回第 i 根 bar
  · 樣本標準差手算(Pine 的 ta.stdev 是母體標準差)
  · `na < 0` 為 false,所以 momCash(na) == 0

能抓到什麼:常數錯、階梯門檻錯、公式錯、視窗長度錯、ddof 用錯、暖機期處理錯。
**抓不到什麼:** Pine 是否編譯得過、`request.security` 的實際行為、
TradingView 的資料與這裡用的是否一致。那些只能靠 `check_pine_parity.py`
對真實匯出做比對。
"""

from __future__ import annotations

import math
import re
from pathlib import Path

PINE_PATH = Path(__file__).resolve().parents[1] / "pine" / "CashWeightGauge.pine"

NA = float("nan")


def _is_na(x: float) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


# --------------------------------------------------------------------------
# 從 .pine 原始檔解析常數與階梯
# --------------------------------------------------------------------------


class PineParseError(RuntimeError):
    """解析不到預期的東西 —— 寧可大聲失敗,也不要默默用預設值比對。"""


def _read_pine() -> str:
    if not PINE_PATH.exists():
        raise PineParseError(f"找不到 {PINE_PATH}")
    return PINE_PATH.read_text(encoding="utf-8")


def parse_constants(src: str) -> dict[str, float]:
    """抓 `NAME = 數字` 形式的常數宣告。"""
    wanted = [
        "MOM_LEN", "Z_LEN", "SPIKE_LEN",
        "VIX_BASE", "VIX_DIV", "SPIKE_DIV",
        "W_LVL", "W_Z", "W_SPIKE",
    ]
    out: dict[str, float] = {}
    for name in wanted:
        m = re.search(rf"^\s*{name}\s*=\s*([0-9.]+)", src, re.MULTILINE)
        if not m:
            raise PineParseError(f"在 .pine 裡找不到常數 {name}")
        out[name] = float(m.group(1))
    return out


def parse_ladder(src: str, func: str) -> list[tuple[str, float, float]]:
    """抓階梯函式的 (比較方向, 門檻, 現金) 序列,依原始碼順序。

    形如:
        if m < -0.03
            c := 0.60
    """
    m = re.search(rf"^{func}\(float \w+\) =>\n(.*?)(?=\n\S|\n$)", src,
                  re.MULTILINE | re.DOTALL)
    if not m:
        raise PineParseError(f"在 .pine 裡找不到階梯函式 {func}")
    body = m.group(1)
    steps = re.findall(r"if\s+\w+\s*([<>])\s*(-?[0-9.]+)\s*\n\s*c\s*:=\s*([0-9.]+)", body)
    if not steps:
        raise PineParseError(f"{func} 裡解析不到任何階梯")
    return [(op, float(th), float(cash)) for op, th, cash in steps]


def apply_ladder(value: float, steps: list[tuple[str, float, float]]) -> float:
    """依 Pine 的串接 if 語義:由上而下,最後一個成立的勝出。na 全部不成立。"""
    c = 0.0
    if _is_na(value):
        return c
    for op, threshold, cash in steps:
        hit = value < threshold if op == "<" else value > threshold
        if hit:
            c = cash
    return c


# --------------------------------------------------------------------------
# Pine 內建函式的語義
# --------------------------------------------------------------------------


def ta_sma(series: list[float], length: int) -> list[float]:
    """視窗不足或含 na -> na。這是 Pine 的行為,與 pandas 的 min_periods 不同。"""
    out: list[float] = []
    for i in range(len(series)):
        if i + 1 < length:
            out.append(NA)
            continue
        window = series[i - length + 1 : i + 1]
        if any(_is_na(v) for v in window):
            out.append(NA)
        else:
            out.append(sum(window) / length)
    return out


def sample_stdev(series: list[float], length: int, sma: list[float]) -> list[float]:
    """對應 .pine 裡的 sampleStdev():除以 (n-1),不是 Pine 內建的母體版。"""
    out: list[float] = []
    for i in range(len(series)):
        m = sma[i]
        if _is_na(m):
            out.append(NA)
            continue
        ss = 0.0
        for k in range(length):
            ss += (series[i - k] - m) ** 2
        out.append(math.sqrt(ss / (length - 1)))
    return out


# --------------------------------------------------------------------------
# 完整重現 .pine 的計算
# --------------------------------------------------------------------------


def simulate(sp_close: list[float], vix_close: list[float],
             vix_high: list[float]) -> list[dict]:
    """三個序列必須已依 bar 對齊(月線圖上就是逐月對齊)。"""
    src = _read_pine()
    k = parse_constants(src)
    mom_steps = parse_ladder(src, "momCash")
    vix_steps = parse_ladder(src, "vixCash")

    z_len = int(k["Z_LEN"])
    spike_len = int(k["SPIKE_LEN"])
    mom_len = int(k["MOM_LEN"])

    m36 = ta_sma(vix_close, z_len)
    sd36 = sample_stdev(vix_close, z_len, m36)
    m6 = ta_sma(vix_close, spike_len)
    sma_mom = ta_sma(sp_close, mom_len)

    rows: list[dict] = []
    for i in range(len(sp_close)):
        vc, vh = vix_close[i], vix_high[i]

        if _is_na(vc):
            lvl = NA
        else:
            lvl = max((vc - k["VIX_BASE"]) / k["VIX_DIV"], 0.0)

        if (not _is_na(sd36[i])) and sd36[i] > 0.0:
            z = (vc - m36[i]) / sd36[i]
        else:
            z = 0.0

        spike = (vh - m6[i]) / k["SPIKE_DIV"] if not _is_na(m6[i]) else 0.0

        if _is_na(lvl):
            risk = NA
        else:
            risk = (k["W_LVL"] * max(lvl, 0.0)
                    + k["W_Z"] * max(z, 0.0)
                    + k["W_SPIKE"] * max(spike, 0.0))

        mom = NA if _is_na(sma_mom[i]) else sp_close[i] / sma_mom[i] - 1.0

        mc = apply_ladder(mom, mom_steps)
        vc_cash = apply_ladder(risk, vix_steps)
        has_signal = (not _is_na(sma_mom[i])) and (not _is_na(vix_close[i]))

        rows.append({
            "mom": mom, "risk": risk,
            "mom_cash": mc, "vix_cash": vc_cash,
            "cash": max(mc, vc_cash),
            "has_signal": has_signal,
        })
    return rows
