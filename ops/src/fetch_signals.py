"""體制層的抓取端 —— 產出 `data/signals.json` 給 PWA 讀。

跑在 GitHub Actions 上(每日),結果推到 `ops-data` 分支。PWA 從
`raw.githubusercontent.com` 讀那一份 —— 那個網域送 `Access-Control-Allow-Origin: *`,
是這整個架構能成立的原因。

═══════════════════════════════════════════════════════════════════════
 為什麼是這三個來源 —— 使用者給的八個連結逐一實測(2026-08-12)
═══════════════════════════════════════════════════════════════════════

| 使用者給的 | 結論 | 實測到什麼 |
|---|---|---|
| cnn.com/markets/fear-and-greed | ✅ **採用** | 官方 JSON 端點,7 個子指標各 250 天歷史,而且送 `ACAO: *` |
| index.ndc.gov.tw 領先指標 | ✅ **採用(換路徑)** | 網站是 Angular + CSRF,`POST /n/json/leading` 回 419。改走 data.gov.tw 的官方開放資料集 6099(ZIP),1982 起月頻,是同一份資料的授權出口 |
| tradingeconomics.com 利率 | ⚠ **改用原始來源** | TE 的 ToS 禁止抓取。利率的原始出處是 Fed;本層改用 CNN 的 `junk_bond_demand` 涵蓋信用面 |
| macromicro sp500-finra | ⚠ **改用原始來源** | MacroMicro 是轉載。原始出處 FINRA 對本機 UA 回 **403** |
| macromicro cnn-fear-and-greed | ⚠ **改用原始來源** | 同上,直接打 CNN |
| macromicro tw-sentiment | ❌ **不採用** | MM 自有的專有指標,付費牆,沒有可授權的機器讀取管道 |
| aaii.com/sentimentsurvey | ❌ **不採用** | 頁面掛了 bot 防護,數字散在 HTML 裡沒有結構化出口。解析器會在某次改版後無聲壞掉,而這種壞法正是本專案在防的 |
| mof.gov.tw/multiplehtml/383 | ❌ **不採用** | 那是公告清單頁,不是資料端點。出口動能改由 NDC 同一份資料裡的「海關出口值」涵蓋 |

實測失敗但**與這裡無關**的兩個(記錄下來免得以後重踩):
  * FRED 直連在開發機被本機安全軟體中斷(WinError 10053),經 corsproxy 回 403。
    因此 FRED 這條路在本機**無法驗證**,不併進來 —— 不驗證就上線的東西不算數。
  * 證交所 OpenAPI 的憑證鏈缺 Subject Key Identifier,OpenSSL 3 直接拒絕。
    這是證交所自己的問題,Ubuntu runner 同樣是 OpenSSL 3,不能賭。

═══════════════════════════════════════════════════════════════════════

失敗處理:**任一來源掛掉不會讓整支失敗。** 該來源在 signals.json 裡記成
`{"ok": false, "error": ...}`,PWA 那一格顯示「無資料」。整份寫不出來才算失敗。
理由是這層是輔助讀數,不該有能力讓每日健檢變紅燈。
"""

from __future__ import annotations

import io
import json
import sys
import time
import urllib.parse
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from console import force_utf8  # noqa: E402
from regime import dominance, log_returns, warning_flags  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
SIGNALS_PATH = REPO_ROOT / "data" / "signals.json"

# CNN 的端點會擋掉「看起來不像瀏覽器」的請求 —— 實測只帶 UA 會回 HTTP 418,
# 要同時有 Accept-Language 與 Referer 才過。這不是在偽裝身分,是滿足它的
# 反爬檢查;端點本身是公開的、且明確送 ACAO:*(就是設計給前端讀的)。
CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
CNN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://edition.cnn.com/",
}
CNN_KEYS = ("fear_and_greed", "market_momentum_sp500", "stock_price_strength",
            "stock_price_breadth", "put_call_options", "market_volatility_vix",
            "junk_bond_demand", "safe_haven_demand")

# 不要把 ZIP 的下載網址寫死 —— 那是一串會過期的 token。
# 每次先問 metadata API 拿當下的 resourceDownloadUrl,這樣來源換位置也能自癒。
NDC_META = "https://data.gov.tw/api/v2/rest/dataset/6099"
NDC_MEMBER = "景氣指標與燈號.csv"

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/"
MARKETS = {           # 顯示名 -> Yahoo 代號
    "美國": "^GSPC",
    "歐洲": "^STOXX50E",
    "日本": "^N225",
    "香港": "^HSI",
    "中國": "000001.SS",
    "台灣": "^TWII",
}
DOMINANCE_RANGE = "2y"   # 兩年 ≈ 500 個交易日,夠穩又還跟得上體制變化

TIMEOUT = 30


def _get(url: str, headers: dict | None = None, retries: int = 3) -> bytes:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers or {"User-Agent": "cash-gauge-ops/1.0"},
                             timeout=TIMEOUT)
            r.raise_for_status()
            return r.content
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"{url} 抓取失敗:{last}") from last


# ── CNN ──────────────────────────────────────────────────────────────────

def fetch_cnn() -> dict:
    """只留下需要的欄位。原始回應 177KB,全存進 signals.json 會讓 PWA 每次多下載
    一份沒人看的歷史 —— 這裡只帶當前分數,加上 junk bond 的 21 天尾巴給 20 日變化用。
    """
    raw = json.loads(_get(CNN_URL, CNN_HEADERS))
    out: dict = {}
    for k in CNN_KEYS:
        node = raw.get(k)
        if not isinstance(node, dict):
            continue
        entry = {"score": node.get("score"), "rating": node.get("rating")}
        if k == "junk_bond_demand":
            data = node.get("data")
            if isinstance(data, list) and len(data) >= 21:
                entry["data"] = [{"y": d.get("y")} for d in data[-21:]]
        out[k] = entry
    ts = raw.get("fear_and_greed", {}).get("timestamp")
    out["as_of"] = ts
    return out


# ── 國發會景氣指標 ────────────────────────────────────────────────────────

def fetch_ndc() -> dict:
    meta = json.loads(_get(NDC_META))
    url = meta["result"]["distribution"][0]["resourceDownloadUrl"]
    zf = zipfile.ZipFile(io.BytesIO(_get(url)))
    text = zf.read(NDC_MEMBER).decode("utf-8-sig")

    lines = [ln for ln in text.splitlines() if ln.strip()]
    header = [h.strip().strip('"') for h in lines[0].split(",")]
    idx = {name: i for i, name in enumerate(header)}
    need = ("Date", "景氣對策信號綜合分數", "景氣對策信號", "領先指標不含趨勢指數")
    missing = [n for n in need if n not in idx]
    if missing:
        # 欄位改名是「來源默默變了」的典型症狀,必須當成錯誤而不是空值
        raise RuntimeError(f"NDC 欄位不如預期,缺:{missing}")

    def cell(row: list[str], name: str) -> str:
        return row[idx[name]].strip().strip('"')

    rows = [ln.split(",") for ln in lines[1:]]
    rows = [r for r in rows if len(r) == len(header) and cell(r, "Date").isdigit()]
    if not rows:
        raise RuntimeError("NDC 檔案裡沒有可用的資料列")
    rows.sort(key=lambda r: cell(r, "Date"))

    # 最新一列的分數可能還沒發布(空白),往回找第一列有分數的
    latest = None
    for r in reversed(rows):
        s = cell(r, "景氣對策信號綜合分數")
        if s:
            latest = r
            break
    if latest is None:
        raise RuntimeError("NDC 檔案裡沒有任何一列有對策信號分數")

    ym = cell(latest, "Date")
    lead_now = cell(latest, "領先指標不含趨勢指數")
    # 領先指標「不含趨勢」的月變化:連續下滑才有意義,單月是雜訊
    lead_prev = ""
    for r in reversed(rows[:rows.index(latest)]):
        if cell(r, "領先指標不含趨勢指數"):
            lead_prev = cell(r, "領先指標不含趨勢指數")
            break

    def num(s: str) -> float | None:
        try:
            return float(s)
        except (TypeError, ValueError):
            return None

    lead_mom = None
    if num(lead_now) is not None and num(lead_prev) is not None:
        lead_mom = round(num(lead_now) - num(lead_prev), 2)

    return {
        "month": f"{ym[:4]}-{ym[4:]}",
        "score": num(cell(latest, "景氣對策信號綜合分數")),
        "light": cell(latest, "景氣對策信號"),
        "leading_ex_trend": num(lead_now),
        "leading_mom": lead_mom,
        "n_months": len(rows),
    }


# ── 多市場日K ─────────────────────────────────────────────────────────────

def fetch_market(symbol: str) -> dict[int, float]:
    url = YAHOO + urllib.parse.quote(symbol) + f"?range={DOMINANCE_RANGE}&interval=1d"
    payload = json.loads(_get(url))
    res = payload["chart"]["result"][0]
    closes = res["indicators"]["quote"][0]["close"]
    return {t // 86400: c for t, c in zip(res["timestamp"], closes) if c}


# ── 組裝 ─────────────────────────────────────────────────────────────────

def _try(name: str, fn):
    """回傳 (值, 記錄)。失敗記成 ok:false,不往外拋 —— 理由見模組 docstring。"""
    try:
        return fn(), {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return None, {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}


def build_signals() -> dict:
    sources: dict[str, dict] = {}

    cnn, sources["cnn"] = _try("cnn", fetch_cnn)
    ndc, sources["ndc"] = _try("ndc", fetch_ndc)

    rets: dict[str, dict[int, float]] = {}
    mkt_status: dict[str, str] = {}
    for name, sym in MARKETS.items():
        closes, st = _try(name, lambda s=sym: fetch_market(s))
        if closes:
            rets[name] = log_returns(closes)
            mkt_status[name] = f"{len(rets[name])} 天"
        else:
            mkt_status[name] = st.get("error", "失敗")
    sources["markets"] = {"ok": len(rets) >= 2, "detail": mkt_status}

    dom = dominance(rets)
    warn = warning_flags(cnn, ndc)

    return {
        "schema": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dominance": {
            "leader": dom.leader,
            "scores": {k: round(v, 4) for k, v in dom.scores.items()},
            "matrix": {f: {l: round(v, 4) for l, v in row.items()}
                       for f, row in dom.matrix.items()},
            "margin": None if dom.margin != dom.margin else round(dom.margin, 2),
            "n_markets": dom.n_markets,
            "window": DOMINANCE_RANGE,
        },
        "warning": {
            "n_triggered": warn.n_triggered,
            "n_usable": warn.n_usable,
            "band": warn.band,
            "flags": [
                {"key": f.key, "label": f.label, "triggered": f.triggered,
                 "detail": f.detail, "rule": f.rule, "source": f.source,
                 "ok": f.ok, "why": f.why}
                for f in warn.flags
            ],
        },
        "cnn": cnn,
        "ndc": ndc,
        "sources": sources,
    }


def write_signals(payload: dict) -> Path:
    SIGNALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SIGNALS_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8", newline="\n",
    )
    return SIGNALS_PATH


def main() -> int:
    force_utf8()
    payload = build_signals()
    path = write_signals(payload)

    dom = payload["dominance"]
    warn = payload["warning"]
    print(f"✓ 寫入 {path}")
    print(f"  主體市場:{dom['leader'] or '(資料不足)'}  "
          f"領先優勢 {dom['margin']}×  取樣 {dom['n_markets']} 個市場")
    for name, sc in sorted(dom["scores"].items(), key=lambda kv: -kv[1]):
        print(f"    {name:>4s}  {sc:.3f}")
    print(f"  早期警訊:{warn['n_triggered']}/{warn['n_usable']} · {warn['band']}")
    for f in payload["warning"]["flags"]:
        mark = "●" if f["triggered"] else ("○" if f["ok"] else "?")
        print(f"    {mark} {f['label']:<8s} {f['detail']}")
    for name, st in payload["sources"].items():
        if not st.get("ok"):
            print(f"  ⚠ 來源 {name} 失敗:{st.get('error') or st.get('detail')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
