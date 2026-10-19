"""資料抓取層 —— 整個專案唯一碰網路的地方。

其他模組一律吃 DataFrame,不知道資料從哪來。這樣 `engine.py` 可以被
離線測試,CI 也不會因為某個資料源今天抽風就變紅燈。

資料源狀態(2026-08 實測)
--------------------------
Shiller S&P  GitHub datasets,1871 起月頻,免金鑰,穩定
VIX 日頻     **CBOE 官方**,1990 起,免金鑰,穩定
             (2026-10-19 從 GitHub datasets 的鏡像換過來 —— 那份自
              2026-09-22 起凍結,但仍回 HTTP 200。見 SOURCES["vix"] 的註解)

2026-08-12 移除 FRED(HY-OAS / T10Y3M)
--------------------------------------
那兩個來源的抓取路徑存在了很久,但 `refresh()` 預設排除需金鑰的來源,
`annual_review.py` 也沒有對應章節 —— **設了 `FRED_API_KEY` 不會改變任何輸出**。
一段「文件宣稱有、實際沒接」的程式比沒有更糟:它讓人以為信用面已經被涵蓋了。

信用面現在由 `fetch_signals.py` 的 CNN `junk_bond_demand` 子指標涵蓋
(每日、免金鑰、已上線),那是即時訊號要的東西。FRED 唯一不可替代的是
**長期歷史**(CNN 端點只給 250 天),而目前沒有任何章節需要它。

要加回來的話:技術限制仍然成立 —— `fredgraph.csv` 端點忽略 `cosd` 只回近三年,
`/data/*.txt` 會轉址到 HTML,所以全段歷史確實需要免費 API key。
"""

from __future__ import annotations

import functools
import hashlib
import io
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = REPO_ROOT / "data" / "raw"
MANIFEST_PATH = REPO_ROOT / "data" / "manifest.json"

USER_AGENT = "cash-gauge-ops/1.0 (+https://github.com/marschannewtag-spec/mars-robot)"

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC"
# CBOE 官方發布的 VIX 全段歷史(1990 起)。會 307 轉址到 cdn-api.cboe.com,
# requests 預設跟隨轉址,不需特別處理。
CBOE_VIX_HISTORY = (
    "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
)


@dataclass(frozen=True)
class Source:
    key: str
    filename: str
    url: str
    kind: str = "csv"  # csv | yahoo | cboe_vix


SOURCES: dict[str, Source] = {
    # 訊號用的價格序列:S&P 500 日K,用來聚合出【月收盤】。
    # 這是 2026-08 定案的價格定義 —— 見 CLAUDE.md「價格定義」一節。
    "sp_daily": Source(
        key="sp_daily",
        filename="sp500_daily.csv",
        url=YAHOO_CHART,
        kind="yahoo",
    ),
    # Shiller:只用來取股息(算總報酬)與長期脈絡。
    # 注意它的 SP500 欄是【月均價】不是月收盤,不可直接當價格序列用。
    "shiller": Source(
        key="shiller",
        filename="shiller_sp500.csv",
        url="https://raw.githubusercontent.com/datasets/s-and-p-500/main/data/data.csv",
    ),
    # VIX 日K。**直接抓 CBOE 自己發布的那一份**,不經鏡像。
    #
    # 2026-10-19 從 `datasets/finance-vix`(GitHub datasets 的鏡像)換過來。
    # 那個鏡像自 2026-09-22 起停止更新 —— 連紅 26 天,而它仍然 HTTP 200、
    # 仍然回 9278 列,所以「可達性」永遠是綠的,只有新鮮度看得出來。
    # 一個還在正常回應、但內容凍結的鏡像,是最難發現的那種死法。
    #
    # 換成上游【不是】換資料定義:實測重疊的 9278 個交易日 CLOSE
    # 逐筆比對 **0 筆不同**,鏡像的日期集合是 CBOE 的真子集,
    # CBOE 多出的正好是停更那 18 個交易日(2026-09-23 .. 2026-10-16)。
    # 所以黃金基準不會漂移。離線證據在 tests/test_vix_source.py:
    #   test_values_match_the_frozen_golden_fixture 拿真的 CBOE 片段
    #   (tests/fixtures/cboe_vix_history.csv)逐筆對黃金 fixture。
    #   上面那句「0 筆不同」是 2026-10-19 的網路實測,不在測試裡 ——
    #   L2 一律離線,網路上的事由 L1 的 health_check 負責。
    #
    # 備援來源(已實測可用,但【刻意不寫成程式】):
    #   https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?range=10y&interval=1d
    #   2026-10-19 實測 HTTP 200 / 266 KB / 0.61s,格式與 sp_daily 的 yahoo 分支相同。
    # 不寫成自動退回鏈的理由:CBOE 是 VIX 的發行方,沒有比它更上游的來源;
    # 而一條平常走不到的退回路徑會腐爛(allorigins 與 FRED 都是這樣死的),
    # 還會讓「主來源死了」變成無聲降級。真的要換,照上面那行改 url + kind 即可。
    "vix": Source(
        key="vix",
        filename="vix_daily.csv",
        url=CBOE_VIX_HISTORY,
        kind="cboe_vix",
    ),
}


class FetchError(RuntimeError):
    """抓取失敗(已用盡重試)。"""


def _get(url: str, params: dict | None = None, retries: int = 3,
         timeout: int = 30) -> str:
    """帶指數退避的 GET。最後一次仍失敗才拋 FetchError。"""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.get(
                url, params=params, timeout=timeout,
                headers={"User-Agent": USER_AGENT},
            )
            r.raise_for_status()
            return r.text
        except Exception as exc:  # noqa: BLE001 — 任何失敗都值得重試
            last = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise FetchError(f"{url} 抓取失敗(重試 {retries} 次):{last}") from last


def _fetch_yahoo_daily() -> str:
    """Yahoo ^GSPC 全段日K -> CSV(date, close)。

    `period1=0` 才拿得到全段(1970 起 14k+ 筆)。用 `range=max` 只會回 168 根
    月K —— Yahoo 對超長區間會自動降頻,是個容易踩的坑。
    """
    payload = _get(
        YAHOO_CHART,
        params={
            "interval": "1d",
            "period1": 0,
            "period2": int(time.time()),
        },
    )
    result = json.loads(payload)["chart"]["result"][0]
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(result["timestamp"], unit="s", utc=True),
            "close": result["indicators"]["quote"][0]["close"],
        }
    ).dropna()
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")
    return df.to_csv(index=False)


def _normalise_cboe_vix(text: str) -> str:
    """CBOE 的 VIX_History.csv -> 與原鏡像逐字元相容的 CSV。

    只動兩件事,都是格式而非數值:
      1. 日期 `MM/DD/YYYY` -> `YYYY-MM-DD`。
         **必須在這裡轉**,不能留給下游:`pd.to_datetime` 對
         `01/02/1990` 要猜月日順序,而 `engine.build_vix_monthly`
         與 PWA 的 `ym()` 都假設 ISO。猜錯的話前 12 天會被分到錯的月份,
         而那是靜默的 —— 數字照樣算得出來,只是錯的。
      2. 數值去掉 CBOE 的 6 位小數尾零(`17.240000` -> `17.24`)。
         VIX 本身只有 2 位有效小數,去尾零讓檔案小一半,且與既有的
         黃金基準 fixture(`tests/fixtures/vix_daily.csv`)寫法一致。

    欄位名稱與順序刻意保持 `DATE,OPEN,HIGH,LOW,CLOSE` —— 下游
    (engine、health_check、PWA、黃金基準 fixture)一行都不用改。
    """
    df = pd.read_csv(io.StringIO(text))
    expect = ["DATE", "OPEN", "HIGH", "LOW", "CLOSE"]
    if list(df.columns[: len(expect)]) != expect:
        raise FetchError(f"CBOE VIX 欄位變了:{list(df.columns)}")
    df = df[expect].copy()
    # format 寫死,不讓 pandas 去猜。
    #
    # ⚠ 誠實註記:在 pandas 2.2 上,`format=` 目前是【等價】的 ——
    #   突變測試(M6:拿掉 format)264 條測試全部照樣通過。實測原因:
    #       to_datetime(["01/02/1990"])               -> 1990-01-02(月在前)
    #       to_datetime(["01/02/1990","13/01/1990"])  -> 直接 raise ValueError
    #   pandas 2.x 遇到前後不一致的欄位會拋,不會像 1.x 那樣靜靜改用日在前。
    #
    #   留著它不是防禦性程式碼,是因為它釘的是【跨 pandas 版本】的語意:
    #   pandas <2.0 會對整欄推斷順序,只要第一個 day>12 的值出現就整欄翻掉,
    #   而 requirements.txt 的 `pandas>=2.2,<3` 是範圍不是鎖定(見
    #   reports/2026-10-19-dependency-locking-for-chatgpt.md)。
    #   猜對是運氣,猜錯是靜默的錯月份 —— 寫死的成本是零。
    df["DATE"] = pd.to_datetime(df["DATE"], format="%m/%d/%Y").dt.strftime("%Y-%m-%d")
    for col in ("OPEN", "HIGH", "LOW", "CLOSE"):
        df[col] = pd.to_numeric(df[col], errors="coerce").round(2)
    df = df.dropna(subset=["DATE", "CLOSE"]).sort_values("DATE")
    return df.to_csv(index=False)


def fetch_source(name: str) -> str:
    """抓單一資料源,回傳 CSV 文字。"""
    src = SOURCES[name]
    if src.kind == "yahoo":
        return _fetch_yahoo_daily()
    if src.kind == "cboe_vix":
        return _normalise_cboe_vix(_get(src.url))
    return _get(src.url)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def save(name: str, text: str) -> dict:
    """落地並回傳該檔的 manifest 條目。"""
    src = SOURCES[name]
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / src.filename
    path.write_text(text, encoding="utf-8", newline="\n")

    df = pd.read_csv(io.StringIO(text))
    date_col = df.columns[0]
    return {
        "file": src.filename,
        "url": src.url,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": int(len(df)),
        "first_date": str(df[date_col].iloc[0]) if len(df) else None,
        "last_date": str(df[date_col].iloc[-1]) if len(df) else None,
        "sha256": _sha256(text),
    }


def write_manifest(manifest: dict) -> None:
    """寫回 manifest。獨立成函式,讓 health_check 也能維護稽核軌跡。"""
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8", newline="\n",
    )


def refresh(names: list[str] | None = None) -> dict:
    """抓取指定資料源、落地、更新 manifest。回傳新的 manifest。"""
    names = names or list(SOURCES)
    manifest = read_manifest()
    for name in names:
        manifest[name] = save(name, fetch_source(name))
    write_manifest(manifest)
    return manifest


def read_manifest() -> dict:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {}


def has_local(name: str) -> bool:
    """本機有沒有這份資料的既有檔案。

    健檢用它來分辨兩件在報告上必須長得不一樣的事:
      「抓取失敗 **且** 本機什麼都沒有」→ 沒得判斷,SKIP
      「抓取失敗 **但** 本機還有上一份好資料」→ 仍然判斷得出它有多舊,照常檢查
    第二種才是使用者最需要知道的:你手上最新的那份資料已經幾天了。
    """
    return (RAW_DIR / SOURCES[name].filename).exists()


def load(name: str, refresh_if_missing: bool = True) -> pd.DataFrame:
    """從 data/raw/ 讀取。檔案不存在時視情況抓取。"""
    path = RAW_DIR / SOURCES[name].filename
    if not path.exists():
        if not refresh_if_missing:
            # 刻意只給 repo 相對路徑,不給絕對路徑:這個訊息會經由健檢
            # 變成【公開 repo 的 Issue 內文】,而絕對路徑裡沒有任何診斷價值
            # (它永遠是 ops/data/raw/),只會把 runner 或開發機的目錄結構貼出去。
            raise FileNotFoundError(
                f"data/raw/{SOURCES[name].filename} 不存在,且未允許重新抓取"
            )
        refresh([name])
    # ⚠ 一定要回 copy。年度健檢的敏感度網格會呼叫 144 次,而 `total_return_perf()`
    #   對取回來的 DataFrame 做 `sh["ym"] = ...`(**原地改**)—— 直接回快取物件的話
    #   那個原地改會污染快取,之後每一格拿到的都是被改過的東西,
    #   結果會變成「依呼叫順序而改變」。那比慢 26 秒嚴重得多。
    return _read_csv_cached(str(path), path.stat().st_mtime).copy()


@functools.lru_cache(maxsize=8)
def _read_csv_cached(path_str: str, mtime: float) -> pd.DataFrame:
    """以 (路徑, mtime) 當鍵快取 CSV 解析。

    年度健檢的 144 格網格原本每格都重讀三個 CSV —— 實測 432 次讀檔、26 秒。
    把 mtime 放進鍵裡,`refresh()` 寫入新檔之後會自然失效,不需要手動清。

    **永遠不要把這個函式的回傳值直接交出去** —— 見 `load()` 的說明。
    """
    return pd.read_csv(path_str)


if __name__ == "__main__":
    import sys

    from console import force_utf8

    force_utf8()
    which = sys.argv[1:] or None
    m = refresh(which)
    for k, v in m.items():
        print(f"{k:10s} {v['rows']:>6} 列  {v['first_date']} .. {v['last_date']}")
