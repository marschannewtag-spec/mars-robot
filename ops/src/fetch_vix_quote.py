"""盤中 VIX 報價 —— 把領先腿的資料年齡從「整個交易時段」壓到「一次排程」。

問題
----
VIX 腿是**領先腿**(`實測`:1990 起六次 ≥15% 回撤,先動的是 VIX 4 次、
兩腿同時 2 次、趨勢腿 0 次)。而它唯一的來源 `VIX_History.csv` 只收錄
**已經收盤**的交易日,每日 workflow 收盤後抓一次。

結果是整個交易時段裡,那條該最早動的腿看的是昨天。
2026-10-30 當天 VIX 盤中最高 23.93、收盤 19.42 —— 而風險分數的飆升項
吃的正是**月內日內最高**。該跳的那一天,它跳不起來。

`實測`(2026-11-02 18:18Z)那天的落差:日檔最後一列是 10/30,
而當下 VIX 已經是 19.81(日內最高 20.13、月份 2026-11)。
趨勢腿若接了即時來源會顯示 2026-11,VIX 腿顯示 2026-10 ——
畫面上那條「VIX 腿落後趨勢腿 1 個月」的警告,就是這個結構落差的症狀。

為什麼不在瀏覽器裡直接抓
------------------------
`實測` 2026-11-02,從 PWA 自己的 origin(github.io)逐一試九個:

    cdn.cboe.com 報價 JSON          沒有 ACAO -> TypeError: Failed to fetch
    query1.finance.yahoo.com ^VIX   同上
    corsproxy.io(鏈上僅存的代理)   HTTP 403 keyless_legacy_url
    allorigins / codetabs / thingproxy   9 秒全逾時或 fail
    stooq                           Failed to fetch(與既有紀錄一致)
    r.jina.ai                       200 —— 但那是第三方轉送站,
                                    allorigins 與 corsproxy 就是這樣死的

所以答案和 `publish_pwa.py` 當初一樣:**server 端抓,推到 ops-data,
瀏覽器讀 raw.githubusercontent.com**(它送 ACAO: *)。
這個 repo 自己就是那支 Worker,不需要使用者去部署什麼。

刻意只用標準函式庫
------------------
不 import pandas、不 import requests。它的 workflow 因此**不需要
`pip install`** —— 一條每天要跑好幾次的路徑,不該讓相依解析有機會弄壞它。

刻意不寫進歷史
--------------
輸出是一個獨立的小檔(~250 bytes),**完全不碰 `vix_daily.csv`**。兩個理由:

1. **歷史不可被盤中讀數改寫。** 黃金基準、parity、健檢全錨在那份日檔上。
2. 80 KB 的檔案每天重寫好幾次,`ops-data` 的歷史會被灌爆。

PWA 只在這個報價的日期**嚴格晚於**日檔最後一天時才接上去。
所以這個檔案凍結了也不會有事 —— 隔天日檔追上來,它就自動被忽略。
**它的失效模式就是回到今天的行為**,這是刻意挑的。

日期是驗出來的,不是假設的
--------------------------
報價 JSON 有兩個時間欄:`timestamp`(UTC)與 `last_trade_time`(美東,無時區標記)。
`實測` 2026-11-02:`timestamp` 18:18:33Z、`last_trade_time` 13:03:31,
差 5h15m = UTC-5(EST)+ 15 分鐘的延遲報價。

這裡**不用 zoneinfo**,而是直接檢查這個差值落在 3.5–6 小時:
- 那個區間同時涵蓋 EDT(UTC-4)與 EST(UTC-5),不需要時區資料庫,
  Windows 上也不會因為缺 tzdata 而行為不同;
- 更重要的是它**順便驗證了假設本身** —— 收盤後或週末,最後一筆成交會越來越舊,
  差值自然掉出區間,這一輪就不發布。假設哪天不成立(CBOE 改成送 UTC),
  差值會變成 0 而被擋下,不會靜靜地把日期記錯一天。

那正是 ponytail 稽核拿來當範例的那個形狀(用伺服器時鐘當「今天」,
柏林 00:30 的訂單被記到前一天)。這裡用兩個讀數互相對帳,而不是信其中一個。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# console.py 自己只 import sys —— 拉進來不違反「只用標準函式庫」那條,
# 而不拉進來會違反另一條:這支會印中文,而 Windows 的 stdout 是 cp950。
# 那是這個 repo 記錄過的第一個實測 bug(CI 全綠、使用者一跑就爆),
# 現在由 test_reporting.py 從守門員那一端釘住 —— 這一條就是它抓到的。
from console import force_utf8  # noqa: E402

OPS_ROOT = Path(__file__).resolve().parents[1]
PWA_DIR = OPS_ROOT / "data" / "pwa"
QUOTE_FILE = "vix_today.json"

QUOTE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json"
USER_AGENT = "cash-gauge-ops/1.0 (+https://github.com/marschannewtag-spec/mars-robot)"

# `timestamp`(UTC)與 `last_trade_time`(美東)之間的合理差值。
# EDT 是 4 小時、EST 是 5 小時,延遲報價再加約 15 分鐘。
# 下界鬆到 3.5 小時、上界放到 6 小時:寧可偶爾多發一次,也不要在
# 夏令時間切換那兩天無聲地整個停掉。掉出區間 = 不在交易時段,這一輪不發布。
MIN_ET_OFFSET = timedelta(hours=3, minutes=30)
MAX_ET_OFFSET = timedelta(hours=6)

# VIX 的合理值域。歷史盤中最高是 2020-03-16 的 89.53,所以 500 這個上界
# 永遠擋不到真的讀數,它擋的是 0、null、字串、以及欄位改名之後的垃圾。
# ⚠ 下界必須嚴格大於 0:報價在非交易時段曾經把 bid/ask 回成 0.0,
#   一個 0 接進去會讓 lvl=(0-16)/9 被 clip 成 0,風險分數整個塌掉 ——
#   而畫面上看起來只是「今天比較平靜」。
VIX_MIN, VIX_MAX = 1.0, 500.0


class QuoteRejected(RuntimeError):
    """報價不可信 —— 不是錯誤,是這一輪不發布。"""


def _get(url: str, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        if r.status != 200:
            raise QuoteRejected(f"HTTP {r.status}")
        return r.read()


def _num(d: dict, key: str) -> float:
    v = d.get(key)
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise QuoteRejected(f"{key} 不是數字:{v!r}")
    v = float(v)
    if not (VIX_MIN <= v <= VIX_MAX):
        raise QuoteRejected(f"{key}={v} 落在合理值域 [{VIX_MIN}, {VIX_MAX}] 之外")
    return v


def parse_quote(payload: dict) -> dict:
    """報價 JSON -> 發布用的那一列。不可信就 raise QuoteRejected。

    每一條檢查都對應一種**不會有症狀**的壞法,所以寧可不發布也不要發錯:
    符號換了、欄位改名、值變成 0、時間戳改了時區、或是現在根本不在交易時段。
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        raise QuoteRejected("沒有 data 物件")
    if data.get("symbol") != "^VIX":
        raise QuoteRejected(f"symbol 不是 ^VIX:{data.get('symbol')!r}")

    try:
        ts = datetime.strptime(payload["timestamp"], "%Y-%m-%d %H:%M:%S")
    except (KeyError, TypeError, ValueError) as e:
        raise QuoteRejected(f"timestamp 解析不了:{payload.get('timestamp')!r}") from e
    try:
        lt = datetime.strptime(data["last_trade_time"], "%Y-%m-%dT%H:%M:%S")
    except (KeyError, TypeError, ValueError) as e:
        raise QuoteRejected(
            f"last_trade_time 解析不了:{data.get('last_trade_time')!r}") from e

    offset = ts - lt
    if not (MIN_ET_OFFSET <= offset <= MAX_ET_OFFSET):
        raise QuoteRejected(
            f"UTC 與最後成交時間差 {offset} —— 不在交易時段,或時區假設不再成立")

    high, low, close = _num(data, "high"), _num(data, "low"), _num(data, "close")
    if not (low <= close <= high):
        raise QuoteRejected(f"low/close/high 不自洽:{low} / {close} / {high}")

    return {
        "date": lt.strftime("%Y-%m-%d"),
        "high": high,
        "low": low,
        "close": close,
        "quote_time_et": lt.isoformat(),
        "generated_at": ts.replace(tzinfo=timezone.utc).isoformat(timespec="seconds"),
        "source": QUOTE_URL,
        "note": "CBOE 延遲 15 分鐘報價。只有在日期嚴格晚於 vix_daily.csv "
                "最後一天時才會被 PWA 接上去;日檔追上來之後它自動被忽略。",
    }


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    ap = argparse.ArgumentParser(description="抓 CBOE 盤中 VIX 報價並發布一列")
    ap.add_argument("--out", default=str(PWA_DIR), help="輸出目錄")
    args = ap.parse_args(argv)

    try:
        payload = json.loads(_get(QUOTE_URL))
        row = parse_quote(payload)
    except (QuoteRejected, urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        # 不是失敗,是「這一輪沒有可發布的東西」。退出碼 0 ——
        # 讓它紅燈等於每個週末、每個收盤後都紅一次,而那種警報兩週內就會被無視。
        print(f"不發布:{e}")
        return 0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / QUOTE_FILE).write_text(
        json.dumps(row, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8", newline="\n")
    print(f"已發布 {row['date']} close={row['close']} high={row['high']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
