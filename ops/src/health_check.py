"""L1 每日健康檢查 —— 全部正常就靜默,任一異常才吵人。

設計原則
--------
1. **寧可漏報也不能狼來了。** 這層一年只該叫使用者幾次。一旦開始出現誤報,
   使用者就會學會無視通知,整層等於不存在。閾值因此訂得寬。
2. **只檢查、不修復、不改資料。** 發現問題就報事實,由人決定怎麼辦。
3. **和 L2 嚴格分工。** 這裡管資料源死活,不碰邏輯正確性;
   L2 管邏輯,完全離線跑。混在一起的話,Yahoo 抽風會讓你以為程式壞了。

用法
----
    python src/health_check.py            # 正常靜默 exit 0,異常 exit 1
    python src/health_check.py --verbose  # 一律列出每一項
    python src/health_check.py --markdown # 輸出 markdown(給 GitHub Issue 用)
"""

from __future__ import annotations

import argparse
import io
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_data  # noqa: E402

OPS_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = OPS_ROOT / "config.yml"
UA = {"User-Agent": "cash-gauge-ops/1.0 health-check"}

OK, FAIL, SKIP = "OK", "FAIL", "SKIP"


@dataclass
class Result:
    name: str
    status: str
    detail: str

    @property
    def failed(self) -> bool:
        return self.status == FAIL


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 各項檢查
# --------------------------------------------------------------------------


def check_sources_reachable(cfg: dict) -> list[Result]:
    """能不能抓到,以及抓到的資料夠不夠長。"""
    out: list[Result] = []
    for name in ["sp_daily", "vix", "shiller"]:
        try:
            text = fetch_data.fetch_source(name)
            df = pd.read_csv(io.StringIO(text))
            floor = cfg["sanity"]["min_rows"][name]
            if len(df) < floor:
                out.append(Result(
                    f"來源可達性 · {name}", FAIL,
                    f"只回了 {len(df)} 列,低於下限 {floor} —— 來源可能改了格式或被截斷",
                ))
            else:
                out.append(Result(f"來源可達性 · {name}", OK, f"{len(df)} 列"))
            fetch_data.save(name, text)
        except Exception as exc:  # noqa: BLE001
            out.append(Result(f"來源可達性 · {name}", FAIL, f"抓取失敗:{exc}"))
    return out


def check_freshness(cfg: dict) -> list[Result]:
    """最新一筆距今多久。停更是最常見的無聲故障。"""
    out: list[Result] = []
    today = datetime.now(timezone.utc).date()
    for name, limit in cfg["freshness_days"].items():
        try:
            df = fetch_data.load(name, refresh_if_missing=False)
            last = pd.to_datetime(df[df.columns[0]]).max().date()
            age = (today - last).days
            if age > limit:
                out.append(Result(
                    f"資料新鮮度 · {name}", FAIL,
                    f"最新一筆是 {last},已 {age} 天沒更新(上限 {limit} 天)",
                ))
            else:
                out.append(Result(f"資料新鮮度 · {name}", OK, f"{last}({age} 天前)"))
        except Exception as exc:  # noqa: BLE001
            out.append(Result(f"資料新鮮度 · {name}", FAIL, f"無法判讀:{exc}"))
    return out


def check_sanity(cfg: dict) -> list[Result]:
    """數值合理性 —— 抓得到不代表資料是對的。"""
    out: list[Result] = []
    s = cfg["sanity"]

    try:
        vix = fetch_data.load("vix", refresh_if_missing=False)
        close = pd.to_numeric(vix["CLOSE"], errors="coerce").dropna()
        bad = close[(close < s["vix_min"]) | (close > s["vix_max"])]
        if len(bad):
            out.append(Result(
                "數值合理性 · VIX 範圍", FAIL,
                f"{len(bad)} 筆落在 [{s['vix_min']}, {s['vix_max']}] 之外,"
                f"例如 {bad.head(3).tolist()}",
            ))
        else:
            out.append(Result("數值合理性 · VIX 範圍", OK,
                              f"{close.min():.2f} .. {close.max():.2f}"))

        dup = vix["DATE"].duplicated().sum()
        out.append(Result("資料完整性 · VIX 無重複日期",
                          FAIL if dup else OK,
                          f"{dup} 筆重複日期" if dup else "無重複"))
    except Exception as exc:  # noqa: BLE001
        out.append(Result("數值合理性 · VIX", FAIL, f"檢查失敗:{exc}"))

    try:
        spd = fetch_data.load("sp_daily", refresh_if_missing=False)
        px = pd.to_numeric(spd["close"], errors="coerce")
        if (px <= 0).any():
            out.append(Result("數值合理性 · S&P 為正", FAIL,
                              f"{int((px <= 0).sum())} 筆非正數"))
        else:
            out.append(Result("數值合理性 · S&P 為正", OK,
                              f"{px.min():.2f} .. {px.max():.2f}"))

        dup = spd["date"].duplicated().sum()
        out.append(Result("資料完整性 · S&P 無重複日期",
                          FAIL if dup else OK,
                          f"{dup} 筆重複日期" if dup else "無重複"))

        # 月收盤的月對月跳空。用月頻而非日頻 —— 訊號本來就是月頻的,
        # 日頻跳空在分割/資料修訂時很常見,會製造噪音。
        import engine

        monthly = engine.sp_monthly(spd)
        gap = (monthly["price"].pct_change().abs() * 100).dropna()
        worst = gap.max()
        if worst > s["max_monthly_gap_pct"]:
            idx = gap.idxmax()
            out.append(Result(
                "數值合理性 · S&P 月跳空", FAIL,
                f"{monthly['ym'].iloc[idx]} 單月變動 {worst:.1f}%,"
                f"超過 {s['max_monthly_gap_pct']}% —— 可能是資料錯誤或真的崩盤,請人工確認",
            ))
        else:
            out.append(Result("數值合理性 · S&P 月跳空", OK, f"最大 {worst:.1f}%"))
    except Exception as exc:  # noqa: BLE001
        out.append(Result("數值合理性 · S&P", FAIL, f"檢查失敗:{exc}"))

    return out


def check_column_degradation(cfg: dict) -> list[Result]:
    """欄位無聲劣化 —— 整欄變成 0 或 NaN,但檔案照樣抓得到。

    Shiller 的 Dividend 就是這樣從 2023-07 起悄悄變成全 0 的。
    已接受的劣化列在 config 的 accepted 區,不再報警;**新出現的**才報。
    """
    out: list[Result] = []
    accepted_since = str(cfg["accepted"]["shiller_zero_columns_since"])
    try:
        sh = fetch_data.load("shiller", refresh_if_missing=False)
        sh["ym"] = pd.to_datetime(sh["Date"]).dt.strftime("%Y-%m")
        recent = sh[sh["ym"] < accepted_since].tail(24)
        problems = []
        for col in ["SP500", "Dividend", "Long Interest Rate"]:
            vals = pd.to_numeric(recent[col], errors="coerce").fillna(0)
            if (vals == 0).all():
                problems.append(col)
        if problems:
            out.append(Result(
                "欄位劣化 · Shiller", FAIL,
                f"{accepted_since} 之前的欄位也變成全 0:{problems} —— "
                f"這是新的劣化,不在已接受清單內",
            ))
        else:
            first_zero = sh[pd.to_numeric(sh["Dividend"], errors="coerce").fillna(0) == 0]
            first_ym = first_zero["ym"].iloc[0] if len(first_zero) else "無"
            note = f"Dividend 自 {first_ym} 起為 0"
            if first_ym < accepted_since and first_ym != "無":
                out.append(Result(
                    "欄位劣化 · Shiller", FAIL,
                    f"{note},早於已接受的 {accepted_since} —— 劣化範圍擴大了",
                ))
            else:
                out.append(Result("欄位劣化 · Shiller", OK,
                                  f"{note}(已知並接受)"))
    except Exception as exc:  # noqa: BLE001
        out.append(Result("欄位劣化 · Shiller", FAIL, f"檢查失敗:{exc}"))
    return out


def check_proxies(cfg: dict) -> list[Result]:
    """PWA 實際在用的免金鑰代理鏈。

    這是整套健檢最有價值的一項。allorigins 就是這樣無聲死掉的 —— 它照樣回應
    HTTP,只是回 408,PWA 靜靜退回月頻資料,畫面上完全看不出來,
    而且每次載入還白等 17 秒。這種故障沒有主動探測就永遠不會被發現。

    ⚠ 探測**必須偽裝成瀏覽器**。corsproxy.io 會擋伺服器端請求
    (回 403「Server-side requests are not allowed on your plan」),
    而且實測必須 Origin + Referer + 瀏覽器 UA **三者同時具備**才放行,
    少任何一個都是 403。用裸請求探測會得到「代理全死」的假警報 ——
    而假警報比漏報更致命,因為使用者會學會無視通知。

    只有在**全部**代理都死掉時才算失敗 —— 那才是 PWA 真的退回月頻的時候。
    部分失效只記錄不報警,否則一個長期死掉的代理會讓你天天收到通知。
    """
    target = cfg["proxy_probe_target"]
    origin = cfg["pwa"]["url"].rstrip("/")
    browser_headers = {
        "Origin": "/".join(origin.split("/")[:3]),
        "Referer": origin + "/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    known_dead = set(cfg["accepted"].get("known_dead_proxies") or [])

    alive: list[str] = []
    detail: list[str] = []
    for p in cfg["proxies"]:
        url = p["template"].replace("{url}", quote(target, safe=""))
        try:
            r = requests.get(url, headers=browser_headers, timeout=15)
            if r.status_code == 200 and '"chart"' in r.text[:400]:
                alive.append(p["name"])
                detail.append(f"{p['name']}: OK ({len(r.text) // 1024} KB)")
            else:
                detail.append(f"{p['name']}: HTTP {r.status_code} {r.text[:50]!r}")
        except Exception as exc:  # noqa: BLE001
            detail.append(f"{p['name']}: {type(exc).__name__}")

    joined = " | ".join(detail)
    if not alive:
        return [Result(
            "PWA 代理鏈", FAIL,
            f"**所有免金鑰代理都失效** —— PWA 的趨勢腿會退回 Shiller 月頻"
            f"(月均價定義,與回測基準不同,畫面會顯示警示列)。{joined}",
        )]

    dead_unexpected = [
        p["name"] for p in cfg["proxies"]
        if p["name"] not in alive and p["name"] not in known_dead
    ]
    if dead_unexpected:
        return [Result(
            "PWA 代理鏈", FAIL,
            f"代理 {dead_unexpected} 新失效,目前只剩 {alive}。功能還沒壞,"
            f"但備援耗損了 —— 若剩下的也掛掉,PWA 就會退回月頻。{joined}",
        )]
    return [Result("PWA 代理鏈", OK, f"可用 {alive}。{joined}")]


def check_pwa_deploy(cfg: dict) -> list[Result]:
    """線上的 PWA 是不是還活著、而且是預期的那一版。"""
    url = cfg["pwa"]["url"]
    try:
        r = requests.get(url, headers=UA, timeout=20)
        if r.status_code != 200:
            return [Result("PWA 部署", FAIL, f"{url} 回 HTTP {r.status_code}")]
        missing = [m for m in cfg["pwa"]["expect_markers"] if m not in r.text]
        if missing:
            return [Result(
                "PWA 部署", FAIL,
                f"線上版缺少預期標記 {missing} —— 部署可能回退到舊版,"
                f"或 index.html 被改壞了",
            )]
        return [Result("PWA 部署", OK, f"HTTP 200,{len(r.text) // 1024} KB,標記齊全")]
    except Exception as exc:  # noqa: BLE001
        return [Result("PWA 部署", FAIL, f"{url} 無法連線:{exc}")]


def check_worker(cfg: dict) -> list[Result]:
    url = (cfg.get("worker_url") or "").strip()
    if not url:
        return [Result("自架 Worker", SKIP, "未設定(config.yml 的 worker_url 留空)")]
    try:
        r = requests.get(url, headers=UA, timeout=15)
        if r.status_code == 200 and '"chart"' in r.text[:400]:
            return [Result("自架 Worker", OK, f"HTTP 200,{len(r.text) // 1024} KB")]
        return [Result("自架 Worker", FAIL,
                       f"HTTP {r.status_code},回傳前 60 字元 {r.text[:60]!r}")]
    except Exception as exc:  # noqa: BLE001
        return [Result("自架 Worker", FAIL, f"無法連線:{exc}")]


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def run_all() -> list[Result]:
    cfg = load_config()
    results: list[Result] = []
    results += check_sources_reachable(cfg)
    results += check_freshness(cfg)
    results += check_sanity(cfg)
    results += check_column_degradation(cfg)
    results += check_proxies(cfg)
    results += check_pwa_deploy(cfg)
    results += check_worker(cfg)
    return results


HEALTH_LOG = OPS_ROOT / "data" / "health_log.csv"


def append_log(results: list[Result]) -> None:
    """把每天的結果追加一行。

    年度健檢的「資料源穩定度」需要一整年的軌跡才算得出來,而軌跡只能靠每天累積。
    一行約 80 bytes,一年 30 KB —— 跟著心跳一起 commit,成本可以忽略。
    """
    failed = [r.name for r in results if r.failed]
    row = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "ok": 0 if failed else 1,
        "n_checks": len(results),
        "failed": "|".join(failed),
    }
    HEALTH_LOG.parent.mkdir(parents=True, exist_ok=True)
    header = not HEALTH_LOG.exists()
    with HEALTH_LOG.open("a", encoding="utf-8", newline="\n") as fh:
        if header:
            fh.write("date,ok,n_checks,failed\n")
        fh.write(f"{row['date']},{row['ok']},{row['n_checks']},{row['failed']}\n")


def render_markdown(results: list[Result]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    failed = [r for r in results if r.failed]
    lines = [
        f"每日健檢於 **{stamp}** 偵測到 **{len(failed)}** 項異常。",
        "",
        "## 異常項目",
        "",
    ]
    for r in failed:
        lines.append(f"- **{r.name}** — {r.detail}")
    lines += ["", "<details><summary>完整檢查結果</summary>", ""]
    for r in results:
        icon = {"OK": "✅", "FAIL": "❌", "SKIP": "⏭"}[r.status]
        lines.append(f"- {icon} `{r.name}` — {r.detail}")
    lines += [
        "",
        "</details>",
        "",
        "---",
        "本 Issue 由 `ops/src/health_check.py` 自動建立。修好之後下一次健檢會自動關閉它。",
        "診斷方式:把這個 Issue 丟給 Claude Code,或本機執行 "
        "`python ops/src/health_check.py --verbose`。",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="現金水位儀每日健檢")
    ap.add_argument("--verbose", action="store_true", help="一律列出每一項")
    ap.add_argument("--markdown", action="store_true", help="輸出 markdown 給 Issue 用")
    ap.add_argument("--no-log", action="store_true", help="不要追加 health_log.csv")
    args = ap.parse_args()

    results = run_all()
    failed = [r for r in results if r.failed]
    if not args.no_log:
        append_log(results)

    if args.markdown:
        if failed:
            print(render_markdown(results))
        return 1 if failed else 0

    if args.verbose or failed:
        for r in results:
            icon = {"OK": "✅", "FAIL": "❌", "SKIP": "⏭"}[r.status]
            print(f"{icon} {r.name:<28} {r.detail}")

    if failed:
        print(f"\n{len(failed)} 項異常。", file=sys.stderr)
        return 1
    return 0  # 全部正常 -> 靜默


if __name__ == "__main__":
    raise SystemExit(main())
