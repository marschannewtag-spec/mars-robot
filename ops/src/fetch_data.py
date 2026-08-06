"""資料抓取層 —— 整個專案唯一碰網路的地方。

其他模組一律吃 DataFrame,不知道資料從哪來。這樣 `engine.py` 可以被
離線測試,CI 也不會因為某個資料源今天抽風就變紅燈。

資料源狀態(2026-08 實測)
--------------------------
Shiller S&P  GitHub datasets,1871 起月頻,免金鑰,穩定
VIX 日頻     GitHub datasets,1990 起,免金鑰,穩定
HY-OAS       FRED,**需要免費 API key**。fredgraph.csv 端點只回近三年
             且忽略 cosd 參數,/data/*.txt 會轉址到 HTML —— 兩者都拿不到全段
T10Y3M       同上,需要 API key 才有全段(1982 起)
"""

from __future__ import annotations

import hashlib
import io
import json
import os
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
FRED_API = "https://api.stlouisfed.org/fred/series/observations"


YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC"


@dataclass(frozen=True)
class Source:
    key: str
    filename: str
    url: str
    needs_fred_key: bool = False
    fred_series: str = ""
    kind: str = "csv"  # csv | fred | yahoo


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
    "vix": Source(
        key="vix",
        filename="vix_daily.csv",
        url="https://raw.githubusercontent.com/datasets/finance-vix/main/data/vix-daily.csv",
    ),
    "hy_oas": Source(
        key="hy_oas",
        filename="hy_oas.csv",
        url="",
        needs_fred_key=True,
        fred_series="BAMLH0A0HYM2",
    ),
    "t10y3m": Source(
        key="t10y3m",
        filename="t10y3m.csv",
        url="",
        needs_fred_key=True,
        fred_series="T10Y3M",
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


def _fred_key() -> str:
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        raise FetchError(
            "缺少 FRED_API_KEY。FRED 的免金鑰 CSV 端點只回近三年資料,"
            "全段歷史必須用 API key。請到 "
            "https://fred.stlouisfed.org/docs/api/api_key.html 免費申請,"
            "再設成環境變數或 GitHub repo secret。"
        )
    return key


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


def fetch_source(name: str) -> str:
    """抓單一資料源,回傳 CSV 文字。"""
    src = SOURCES[name]
    if src.kind == "yahoo":
        return _fetch_yahoo_daily()
    if not src.needs_fred_key:
        return _get(src.url)

    payload = _get(
        FRED_API,
        params={
            "series_id": src.fred_series,
            "api_key": _fred_key(),
            "file_type": "json",
            "observation_start": "1900-01-01",
        },
    )
    obs = json.loads(payload).get("observations", [])
    rows = [(o["date"], o["value"]) for o in obs if o.get("value") not in (".", None)]
    df = pd.DataFrame(rows, columns=["observation_date", src.fred_series])
    return df.to_csv(index=False)


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
        "url": src.url or f"{FRED_API}?series_id={src.fred_series}",
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
    names = names or [n for n, s in SOURCES.items() if not s.needs_fred_key]
    manifest = read_manifest()
    for name in names:
        manifest[name] = save(name, fetch_source(name))
    write_manifest(manifest)
    return manifest


def read_manifest() -> dict:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {}


def load(name: str, refresh_if_missing: bool = True) -> pd.DataFrame:
    """從 data/raw/ 讀取。檔案不存在時視情況抓取。"""
    path = RAW_DIR / SOURCES[name].filename
    if not path.exists():
        if not refresh_if_missing:
            raise FileNotFoundError(f"{path} 不存在,且未允許重新抓取")
        refresh([name])
    return pd.read_csv(path)


if __name__ == "__main__":
    import sys

    which = sys.argv[1:] or None
    m = refresh(which)
    for k, v in m.items():
        print(f"{k:10s} {v['rows']:>6} 列  {v['first_date']} .. {v['last_date']}")
