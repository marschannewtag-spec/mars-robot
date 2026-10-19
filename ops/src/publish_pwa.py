"""把每日抓到的資料轉成 **PWA 在瀏覽器裡直接讀得到** 的檔案。

為什麼需要這一層
----------------
PWA 跑在瀏覽器裡,而 Yahoo 與 CBOE 都不送 `Access-Control-Allow-Origin`
標頭 —— 瀏覽器抓不到。2026-08 之前的解法是串免費 CORS 代理,
2026-10-19 實測【全部失效】:

    corsproxy.io   HTTP 401(2026-08-28 起轉付費)   ~120ms
    allorigins     完整 7 秒逾時,4/4 次           (已於 2026-10-14 移除)

後果不是「畫面壞掉」,而是**趨勢腿每天默默退回 Shiller 月均價** ——
那個定義與回測基準(月收盤)不同,在 17.3% 的月份會給出不同的現金建議。
連紅 47 天沒人知道。

這一層把問題整個拿掉:每日 workflow 在 **server 端** 抓(沒有 CORS 問題),
把結果寫成 PWA 現有解析器就讀得懂的格式,跟著心跳推到 `ops-data` 分支。
`raw.githubusercontent.com` 送 `Access-Control-Allow-Origin: *`
(2026-10-19 實測),所以瀏覽器可以直接讀 —— **不需要代理、不需要金鑰、
不需要自架 Cloudflare Worker**。那支 Worker 原本是文件裡「最推薦的長久解」,
而它需要使用者去開帳號、部署、而且不小心就會變成一支開放代理。
現在這個 repo 自己就是那支 Worker。

格式刻意【不是】新格式
----------------------
`sp_monthly.json` 是 Yahoo chart JSON 的**子集**,讓 PWA 的 `spFromYahoo()`
原封不動就能解析;`vix_daily.csv` 的欄位是原本上游鏡像的子集,讓
`parseCSV()` + `buildVixMonthly()` 原封不動就能解析。

整個 PWA 只有「bytes 從哪裡來」變了,**算法一行都沒動** ——
所以黃金基準與 JS/Python 對帳測試都還釘得住同樣的東西。

為什麼 S&P 只發月底那一天
--------------------------
`spFromYahoo()` 做的事就是「按 UTC 月分組,取每組最後一筆收盤」。
所以只發每個月最後一個交易日,它算出來的 `[{ym, price}]` 與拿到全段
日K時**完全相同**,而檔案從 421 KB 掉到 ~17 KB。
這不是近似,是恆等式 —— 由 `test_month_end_only_is_identical_to_full_daily`
(Python 側)與 `tests/sp_mirror_bridge.mjs`(真的跑 PWA 那支 `spFromYahoo`)
兩邊各自釘住。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_data  # noqa: E402
from console import force_utf8  # noqa: E402

OPS_ROOT = Path(__file__).resolve().parents[1]
PWA_DIR = OPS_ROOT / "data" / "pwa"

SP_MONTHLY = "sp_monthly.json"
VIX_DAILY = "vix_daily.csv"

# VIX 日K 要發幾年。
#
# 需求是算得出來的,不是挑的:畫面上的歷史圖是**最後 96 個月**,而每個月的
# 風險分數需要**前 36 個月**的滾動 z 分數 —— 96 + 36 = 132 個月 = 11 年。
# 14 年留三年餘裕,讓滾動窗在月初、閏年、連假邊界都還是滿的。
#
# 代價:約 100 KB,每天重發一次。要再壓的話唯一的槓桿是「按年切檔」
# (`vix_history.csv` 一年才變一次 + `vix_recent.csv` 每天變),
# 代價是 PWA 要多一次 fetch 與一段串接邏輯。目前不值得。
PWA_VIX_YEARS = 14


def sp_monthly_payload(sp_daily: pd.DataFrame, now: datetime | None = None) -> dict:
    """日K -> Yahoo chart JSON 的子集(每月最後一個交易日)。

    `spFromYahoo()` 用 `new Date(ts*1000).toISOString().slice(0,7)` 分月,
    也就是 **UTC** 月份。`fetch_data._fetch_yahoo_daily()` 落地時已經把
    Yahoo 的 epoch 轉成 UTC 日期字串,所以這裡把日期當 UTC 午夜轉回 epoch,
    UTC 月份必然一致。
    ⚠ 不要改用本地時區 —— 在 UTC+11 以東,月底那根會被算進下個月。
    """
    df = sp_daily.copy()
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date")
    df["ym"] = df["date"].dt.strftime("%Y-%m")
    tail = df.groupby("ym", sort=True).tail(1)

    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    return {
        # 這兩個鍵是給人與健檢看的。`spFromYahoo()` 只讀 chart.*,多的鍵它不管。
        "generated_at": stamp,
        "note": "每月最後一個交易日的收盤。由 ops/src/publish_pwa.py 產生。",
        "chart": {
            "error": None,
            "result": [{
                "meta": {"symbol": "^GSPC", "granularity": "1mo-last-close"},
                "timestamp": [int(t.timestamp()) for t in tail["date"]],
                "indicators": {
                    "quote": [{"close": [float(c) for c in tail["close"]]}]
                },
            }],
        },
    }


def vix_payload(vix_daily: pd.DataFrame, years: int = PWA_VIX_YEARS,
                now: datetime | None = None) -> str:
    """VIX 日K -> `DATE,HIGH,CLOSE` 的近 N 年切片。

    `buildVixMonthly()` 只讀 DATE / CLOSE / HIGH 三欄 —— OPEN 與 LOW
    從來沒被用到,發出去只是讓每天的 commit 大一倍。
    """
    df = vix_daily.copy()
    df["DATE"] = pd.to_datetime(df["DATE"])
    for col in ("HIGH", "CLOSE"):
        df[col] = pd.to_numeric(df[col], errors="coerce").round(2)
    df = df.dropna(subset=["DATE", "CLOSE"]).sort_values("DATE")
    # HIGH 缺漏時退回用 CLOSE —— 與 engine.build_vix_monthly / buildVixMonthly 同一條規則
    df["HIGH"] = df["HIGH"].fillna(df["CLOSE"])

    cutoff = pd.Timestamp(now or datetime.now(timezone.utc)).tz_localize(None)
    cutoff = cutoff.normalize() - pd.DateOffset(years=years)
    df = df[df["DATE"] >= cutoff]

    out = df[["DATE", "HIGH", "CLOSE"]].copy()
    out["DATE"] = out["DATE"].dt.strftime("%Y-%m-%d")
    # lineterminator 寫死 LF。`to_csv()` 回字串時在 Windows 上用的是 CRLF,
    # 在 Linux runner 上是 LF —— 同一份輸入會因為「誰產生的」而得到不同的
    # bytes(實測相差 3551 bytes = 行數)。發布物應該是輸入的函數,
    # 不是產生它的那台機器的函數,否則 sha256 與 commit 差異都失去意義。
    return out.to_csv(index=False, lineterminator="\n")


def _publish_sp(out: Path, now: datetime | None) -> dict:
    sp = sp_monthly_payload(fetch_data.load("sp_daily", refresh_if_missing=False), now)
    text = json.dumps(sp, ensure_ascii=False, separators=(",", ":")) + "\n"
    ts = sp["chart"]["result"][0]["timestamp"]
    if not ts:
        raise ValueError("sp_daily 聚合後一個月份都沒有")
    (out / SP_MONTHLY).write_text(text, encoding="utf-8", newline="\n")
    return {
        "sp_months": len(ts),
        "sp_last": datetime.fromtimestamp(ts[-1], timezone.utc).strftime("%Y-%m-%d"),
        "sp_bytes": len(text.encode("utf-8")),
    }


def _publish_vix(out: Path, now: datetime | None) -> dict:
    text = vix_payload(fetch_data.load("vix", refresh_if_missing=False), now=now)
    rows = text.strip().split("\n")
    if len(rows) < 2:
        raise ValueError("vix 切窗後一列都不剩")
    (out / VIX_DAILY).write_text(text, encoding="utf-8", newline="\n")
    return {
        "vix_rows": len(rows) - 1,
        "vix_last": rows[-1].split(",")[0],
        "vix_bytes": len(text.encode("utf-8")),
    }


def publish(out_dir: Path | None = None, now: datetime | None = None) -> dict:
    """產生兩個檔案,回傳摘要(給 workflow 的 log 與健檢用)。

    ⚠ 兩個發布物【各自獨立】成功或失敗,不綁在一起。

    原本是一個函式一路寫到底,於是**任何一個來源掛掉都會讓兩份鏡像一起凍結**。
    在乾淨的 runner 上這很容易發生:`data/raw/*.csv` 不進版控,只有
    `check_sources_reachable()` 抓成功的那幾個才會落地 —— 所以 VIX 來源
    掛一天,S&P 的鏡像也跟著停在前一天,即使 S&P 抓得好好的。

    這是「一個故障擴散成兩個」的典型形狀。分開之後,壞掉的那一份沿用
    分支上的舊版(舊資料好過沒資料),好的那一份照常更新,而「舊版有多舊」
    由健檢的 `PWA 資料鏡像` 兩項分別報告 —— 它們本來就是分開的兩項。
    """
    out = out_dir or PWA_DIR
    out.mkdir(parents=True, exist_ok=True)

    summary: dict = {}
    errors: dict[str, str] = {}
    for name, fn in (("sp", _publish_sp), ("vix", _publish_vix)):
        try:
            summary.update(fn(out, now))
        except Exception as exc:  # noqa: BLE001
            errors[name] = f"{type(exc).__name__}: {exc}"
    summary["errors"] = errors
    return summary


def main() -> int:
    force_utf8()  # Windows 的 cp950 印不出中文訊息,不設會直接 UnicodeEncodeError
    ap = argparse.ArgumentParser(description="產生 PWA 直接讀的資料檔")
    ap.add_argument("--out", help="輸出目錄(預設 ops/data/pwa)")
    args = ap.parse_args()
    try:
        summary = publish(Path(args.out) if args.out else None)
    except Exception as exc:  # noqa: BLE001
        print(f"發布整個失敗:{exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    errors = summary.get("errors") or {}
    for name, msg in errors.items():
        print(f"[warn] {name} 鏡像沒有更新:{msg}", file=sys.stderr)
    # 只有【兩份都失敗】才回非零。一份成功一份失敗仍然回 0,因為成功的那一份
    # 真的更新了 —— 把它報成失敗會讓 workflow 的 log 看起來像什麼都沒做。
    # 真正的「舊版有多舊」由健檢的 `PWA 資料鏡像` 兩項分別報告(讀線上那一份),
    # 所以這裡的 exit code 只負責描述這一步做了什麼,不負責判斷嚴重程度。
    return 1 if len(errors) == 2 else 0


if __name__ == "__main__":
    raise SystemExit(main())
