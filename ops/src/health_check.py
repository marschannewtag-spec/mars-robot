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
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
import re
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_data  # noqa: E402
from console import force_utf8  # noqa: E402

OPS_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = OPS_ROOT / "config.yml"
UA = {"User-Agent": "cash-gauge-ops/1.0 health-check"}

OK, FAIL, SKIP = "OK", "FAIL", "SKIP"


def today_utc() -> date:
    """整份健檢裡「今天」的唯一來源。

    ⚠ 不要用 `date.today()`。那是**執行這段程式的機器的本地日期**,而這個
    repo 其他每一個時間戳都是 UTC:心跳 commit、Issue 內文的 `at=` 標記、
    報告檔名、workflow 的排程。在 UTC+8 的開發機上,本地午夜到早上八點
    之間兩者差一整天。

    這個差會這樣現形:**CI(UTC)永遠綠,開發機每天紅八小時**,
    而那看起來像「測試不穩」——最容易被當成雜訊關掉的那種紅燈。
    2026-10-20 `check_published_mirror`(用 `date.today()`)就是這樣跟
    `check_freshness`(用 UTC)分岔的,由 test_health_check.py 那條
    算天數的測試在本地凌晨跑出來才看見。

    與 `markets.ok` 門檻分岔同一個形狀:**同一個判斷不能有兩個來源**。
    """
    return datetime.now(timezone.utc).date()

# 從外部回應擷取片段時允許的字元。刻意極保守。
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9 ,.:;/_@=<>()\[\]{}\"'?!+*%-]")


def safe_snippet(text: str, limit: int = 60) -> str:
    """把外部回應內容裁成可安全放進報告的片段。

    為什麼需要這個:健檢報告會變成 GitHub Issue 的內文,而 Issue 內文可能被
    自動化流程餵給 LLM。若原樣嵌入第三方伺服器回傳的 body,一個被入侵(或單純
    惡意)的資料源就能把指令注入到那個提示裡 —— 這正是 2026 年 claude-code-action
    被攻破的那類路徑。

    做法:去掉換行與反引號(避免破壞 markdown 結構)、只留白名單字元、限長。
    診斷需要的是「回了什麼型態的東西」,不需要原文逐字重現。
    """
    if not text:
        return "(空回應)"
    flat = " ".join(str(text).split())
    cleaned = _SAFE_CHARS.sub("·", flat)[:limit]
    return cleaned + ("…" if len(flat) > limit else "")


@dataclass
class Result:
    name: str
    status: str
    detail: str
    # 這一項是在講哪個資料源。用欄位而不是從 name 反解字串 —— 下游要靠它判斷
    # 「這個來源這次沒抓成功」,而從顯示名稱反解會在改文案時無聲壞掉。
    source: str | None = None

    @property
    def failed(self) -> bool:
        return self.status == FAIL


def unreachable_sources(results: list[Result]) -> set[str]:
    """這次執行裡沒有通過可達性檢查的來源。

    為什麼需要這個:`data/raw/*.csv` 不進版控,workflow 也只從 ops-data 取回
    manifest 與 health_log。所以在乾淨的 runner 上,新鮮度與合理性能不能跑
    **完全取決於** `check_sources_reachable()` 在同一次執行裡有沒有把檔案存下來。

    2026-08-13 修掉「壞資料會蓋掉好資料」之後,壞掉的來源就【不落地】了 ——
    正確,但二階效應是後面兩項檢查改報「檔案不存在」。實測一個被截斷的 sp_daily
    會產生 4 條訊息,其中 2 條在說錯的事(真正的原因只在第一行)。

    對一個以「不要狼來了」為核心的系統,那是在製造噪音。所以上游已經紅的來源,
    下游一律標 SKIP —— 語意是「這一項沒得判斷」,不是「這一項也壞了」。
    """
    return {r.source for r in results if r.failed and r.source}


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 各項檢查
# --------------------------------------------------------------------------


def check_sources_reachable(cfg: dict) -> list[Result]:
    """能不能抓到,以及抓到的資料夠不夠長。

    順帶更新 data/manifest.json 的稽核軌跡(來源、抓取時間、筆數、sha256)。
    這一步曾經漏掉 —— `save()` 只寫原始檔並回傳條目,不會寫 manifest,
    結果 manifest 一直停在最後一次手動 refresh() 的舊值,稽核軌跡形同虛設,
    而且心跳 commit 也少了一個會變動的檔案。
    """
    out: list[Result] = []
    manifest = fetch_data.read_manifest()
    floors = cfg["sanity"]["min_rows"]
    for name in sorted(fetch_data.SOURCES):
        floor = floors.get(name)
        if floor is None:
            # 新來源沒訂下限就靜靜略過的話,它等於永遠沒被檢查過 ——
            # FRED 死管線就是這樣活了很久。寧可紅燈也不要無聲的空窗。
            out.append(Result(
                f"來源可達性 · {name}", FAIL,
                f"config.yml 的 sanity.min_rows 沒有 {name} 的下限,無法判斷回應是否完整",
                source=name,
            ))
            continue
        try:
            text = fetch_data.fetch_source(name)
            df = pd.read_csv(io.StringIO(text))
            if len(df) < floor:
                # ⚠ 不落地。這一列曾經寫在 if/else 外面,於是一個被截斷的回應會
                # **蓋掉本機最後一份好資料**,並在 manifest 記上 rows=5 + 全新的
                # fetched_at —— 稽核軌跡與心跳於是宣稱「剛剛更新成功」,而檢查
                # 本身正在說它壞了。實測:601 列的好檔被 5 列的壞檔蓋掉。
                # 模組開頭寫的是「只檢查、不修復、**不改資料**」,這裡是它的例外。
                out.append(Result(
                    f"來源可達性 · {name}", FAIL,
                    f"只回了 {len(df)} 列,低於下限 {floor} —— 來源可能改了格式或被截斷。"
                    f"本機既有資料保持不動",
                    source=name,
                ))
                continue
            out.append(Result(f"來源可達性 · {name}", OK, f"{len(df)} 列", source=name))
            manifest[name] = fetch_data.save(name, text)
        except Exception as exc:  # noqa: BLE001
            out.append(Result(f"來源可達性 · {name}", FAIL,
                              f"抓取失敗:{exc}", source=name))
    fetch_data.write_manifest(manifest)
    return out


def _shift_month(year: int, month: int, back: int) -> str:
    """回 'YYYY-MM',往前推 `back` 個月。純算術,不碰 datetime 的月份加減陷阱。"""
    idx = year * 12 + (month - 1) - back
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def expected_month(today: date, grace_day: int) -> str:
    """到了 `today` 這一天,一個月頻來源【至少】該有哪個觀測月的資料。

    月初的寬限窗內只要求「上上個月」—— 因為這個月的那一列本來就還沒發布。
    過了寬限日就要求「上個月」必須已經到了。

    把它抽成純函式是刻意的:這是整條判準的核心,而它必須能在不碰網路、
    不碰檔案、不碰 datetime.now() 的情況下逐日模擬一整年(見測試)。
    """
    return _shift_month(today.year, today.month, 1 if today.day >= grace_day else 2)


def check_freshness(cfg: dict, unavailable: set[str] | None = None) -> list[Result]:
    """最新一筆距今多久。停更是最常見的無聲故障。

    判準依來源自己的發布頻率:
      daily   —— 最後一筆距今幾天,超過 max_age_days 就紅
      monthly —— 該發布的那一期到了沒(見 `expected_month()`)

    ⚠ 月頻來源【不能】用天數判。Shiller 的觀測月 M 那一列在 M+1 月初才發布,
      所以它在被取代之前年齡必然走到約 60 天 —— 任何小於 60 的天數門檻都會
      每個月必然破線一次。那不是偵測,那是週期性假警報。

    `unavailable`:本次可達性已失敗的來源(見 `unreachable_sources()`)。

    ⚠ **抓取失敗不等於沒得判斷。** 第一版對 unavailable 的來源一律 SKIP,
    那是錯的:只要本機還有上一份好資料,它有多舊仍然判斷得出來 ——
    而且那正是使用者最需要知道的事(「你手上最新的那份已經 25 天了」)。
    一律 SKIP 會把它藏起來,而來源連續死三週時那個訊號才是重點。

    所以判準改成:**本機有沒有檔案**。沒有才 SKIP(真的沒得判斷),
    有就照常檢查,並在 detail 裡講明評估的是最後一份好資料。
    """
    out: list[Result] = []
    unavailable = unavailable or set()
    today = today_utc()
    for name, spec in cfg["freshness"].items():
        if not fetch_data.has_local(name):
            # 刻意不帶 source:那個欄位的語意是「這是可達性檢查的結果」,
            # 只有 check_sources_reachable() 會設。別處也設的話,
            # unreachable_sources() 哪天被餵錯清單就會把新鮮度失敗誤判成抓不到。
            out.append(Result(
                f"資料新鮮度 · {name}", SKIP,
                "跳過 —— 本次抓取未通過檢查(見上方),且本機沒有既有資料可判斷"
                if name in unavailable else "跳過 —— 本機沒有這份資料",
            ))
            continue
        stale_note = "(評估的是本機最後一份好資料,本次抓取未通過檢查)" \
            if name in unavailable else ""
        try:
            df = fetch_data.load(name, refresh_if_missing=False)
            last = pd.to_datetime(df[df.columns[0]]).max().date()

            if spec["cadence"] == "monthly":
                want = expected_month(today, spec["publish_grace_day"])
                got = f"{last.year:04d}-{last.month:02d}"
                if got < want:
                    out.append(Result(
                        f"資料新鮮度 · {name}", FAIL,
                        f"最新觀測月是 {got},但到今天({today})應該已經有 {want} 那一期了"
                        f" —— 月頻來源漏了一期{stale_note}",
                    ))
                else:
                    out.append(Result(f"資料新鮮度 · {name}", OK,
                                      f"觀測月 {got}(要求 ≥ {want}){stale_note}"))
                continue

            age = (today - last).days
            limit = spec["max_age_days"]
            if age > limit:
                out.append(Result(
                    f"資料新鮮度 · {name}", FAIL,
                    f"最新一筆是 {last},已 {age} 天沒更新(上限 {limit} 天){stale_note}",
                ))
            else:
                out.append(Result(f"資料新鮮度 · {name}", OK,
                                  f"{last}({age} 天前){stale_note}"))
        except Exception as exc:  # noqa: BLE001
            out.append(Result(f"資料新鮮度 · {name}", FAIL, f"無法判讀:{exc}"))
    return out


def _sanity_vix(cfg: dict) -> list[Result]:
    """VIX 的數值合理性與日期完整性。"""
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
    return out


def _sanity_sp(cfg: dict) -> list[Result]:
    """S&P 的數值合理性、日期完整性、以及月對月跳空。"""
    out: list[Result] = []
    s = cfg["sanity"]
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
            # `.loc` 而不是 `.iloc`:idxmax() 回的是【標籤】。sp_monthly() 目前
            # reset_index 過,標籤恰好等於位置,所以兩者行為相同 —— 但那是上游的
            # 巧合,不是這裡的保證。用 .loc 才和 idxmax() 自洽。
            # (2026-08-13 我曾懷疑這裡有 label/position bug,建了對照資料去驗:
            #  它是對的,錯的是我的假設。已由 test_monthly_gap_names_the_right_month 釘住。)
            idx = gap.idxmax()
            out.append(Result(
                "數值合理性 · S&P 月跳空", FAIL,
                f"{monthly['ym'].loc[idx]} 單月變動 {worst:.1f}%,"
                f"超過 {s['max_monthly_gap_pct']}% —— 可能是資料錯誤或真的崩盤,請人工確認",
            ))
        else:
            out.append(Result("數值合理性 · S&P 月跳空", OK, f"最大 {worst:.1f}%"))
    except Exception as exc:  # noqa: BLE001
        out.append(Result("數值合理性 · S&P", FAIL, f"檢查失敗:{exc}"))
    return out


def check_sanity(cfg: dict, unavailable: set[str] | None = None) -> list[Result]:
    """數值合理性 —— 抓得到不代表資料是對的。

    判準同 `check_freshness()`:**本機有沒有檔案**,不是「上游有沒有失敗」。
    上游失敗但本機還有最後一份好資料時,那份資料的合理性仍然檢查得出來 ——
    真的沒檔案才是沒得判斷。
    """
    out: list[Result] = []
    unavailable = unavailable or set()
    for name, label, fn in (("vix", "VIX", _sanity_vix),
                            ("sp_daily", "S&P", _sanity_sp)):
        if not fetch_data.has_local(name):
            out.append(Result(
                f"數值合理性 · {label}", SKIP,
                "跳過 —— 本次抓取未通過檢查(見上方),且本機沒有既有資料"
                if name in unavailable else "跳過 —— 本機沒有這份資料",
            ))
            continue
        out += fn(cfg)
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


def check_proxies(cfg: dict, mirror_ok: bool = True) -> list[Result]:
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

    ⚠ 2026-10-19:嚴重程度改成【取決於鏡像】(`mirror_ok`)。
    ---------------------------------------------------------------
    這一項從 2026-08-28 起連紅,因為 corsproxy.io 轉付費了,而它是清單上
    唯一的代理。同一天起,PWA 的趨勢腿改成先讀本專案自己發布的資料鏡像
    (ops-data 分支,見 publish_pwa.py)—— 代理鏈退到第三順位。

    **這不是放寬閾值。** 放寬閾值是「把紅線往外挪,讓同一件壞事不再觸發」;
    這裡改的是**嚴重程度跟著後果走**:代理全死且鏡像正常 = 使用者看到的數字
    完全正確,備援少了一層;代理全死且鏡像也壞 = 使用者真的會看到月均價定義
    的數字,那是 FAIL。

    判準寫得可驗證,而且**仍然會紅**:
      鏡像壞 + 代理全死 -> FAIL   (test_proxy_failure_is_fail_when_the_mirror_is_broken)
      鏡像好 + 代理全死 -> SKIP   (test_proxy_failure_is_not_an_alarm_while_the_mirror_serves)
      代理活著           -> OK
    SKIP 不會進報告的「異常項目」,但仍然完整出現在 `<details>` 的全部結果裡 ——
    所以「看得到」和「被叫起來」這兩件事被分開了,而那正是這次要修的東西。
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
            # ⚠ 這一個【刻意不重試】,和鏡像/部署那兩項不同。
            #
            # 這裡量的是「使用者的瀏覽器此刻能不能透過這個代理拿到資料」,
            # 而 index.html 的 `for(const p of PROXIES)` 對每個代理**只試一次**
            # (單一 fetchT,catch 就換下一個,2026-10-19 核對過原始碼)。
            # 探測若重試,健檢就會比真正的消費端更樂觀 —— 那是「用產生端的尺
            # 量消費端」,本檔踩過好幾次的同一個形狀。
            # 由 test_proxy_chain.py::test_the_proxy_probe_is_not_retried 釘住。
            r = requests.get(url, headers=browser_headers, timeout=15)
            if r.status_code == 200 and '"chart"' in r.text[:400]:
                alive.append(p["name"])
                detail.append(f"{p['name']}: OK ({len(r.text) // 1024} KB)")
            else:
                detail.append(f"{p['name']}: HTTP {r.status_code} {safe_snippet(r.text, 50)}")
        except Exception as exc:  # noqa: BLE001
            detail.append(f"{p['name']}: {type(exc).__name__}")

    joined = " | ".join(detail)
    if not alive:
        if mirror_ok:
            return [Result(
                "PWA 代理鏈", SKIP,
                f"所有免金鑰代理都失效,但**不影響使用者看到的數字** —— "
                f"趨勢腿由本專案的資料鏡像供應(第一順位)。代理鏈是第三順位備援。"
                f"鏡像若也壞掉,這一項會立刻變回 FAIL。{joined}",
            )]
        return [Result(
            "PWA 代理鏈", FAIL,
            f"**所有免金鑰代理都失效,而資料鏡像也不可用** —— PWA 的趨勢腿會"
            f"退回 Shiller 月頻(月均價定義,與回測基準不同,畫面會顯示警示列)。"
            f"{joined}",
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


def _mirror_last_day(sp_json: dict) -> str | None:
    """從 sp_monthly.json 取最後一筆的 UTC 日期。"""
    try:
        ts = sp_json["chart"]["result"][0]["timestamp"]
    except (KeyError, IndexError, TypeError):
        return None
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts[-1]), timezone.utc).strftime("%Y-%m-%d")


# 讀「線上此刻是什麼狀態」的重試參數。`fetch_data._get()` 從一開始就是 3 次指數退避,
# 而這個新檢查原本是**整個 repo 唯一沒有重試的網路讀取** —— 偏偏它讀的是
# PWA 的第一順位來源,而且它失敗時會連帶把「PWA 代理鏈」也推成 FAIL
# (`mirror_is_serving()` 回 False),於是一次 20 秒的網路抽風會被報成
# 兩項故障 + 改標題 + 留一則言。那正是這個專案最怕的狼來了。
_LIVE_RETRIES = 3
_LIVE_BACKOFF = 1.0  # 秒,指數退避。測試把它設成 0


def _get_live(url: str, timeout: int = 20):
    """讀線上資源,只對「問不到」重試;「伺服器明確回答了」不重試。

    | 情況 | 重試 | 理由 |
    |---|---|---|
    | 連線錯誤 / 逾時 | 是 | 我們沒能問到 —— 這不是鏡像壞了的證據 |
    | 5xx | 是 | CDN 自己的暫時性故障 |
    | 4xx(含 404) | **否** | 伺服器明確說「沒有這個檔案」,那是定論 |

    ⚠ **這不是放寬閾值。** 沒有任何判準移動:404 仍然 FAIL、過期仍然 FAIL、
    格式不符仍然 FAIL、連續三次失敗仍然 FAIL。改的只有「問一次」變成
    「問三次才算問不到」。用本檔那條分辨法檢查:原本那件壞事(鏡像真的
    不見了/凍結了),現在還會紅嗎?**會。**
    """
    last_exc: Exception | None = None
    resp = None
    for attempt in range(_LIVE_RETRIES):
        try:
            resp = requests.get(url, headers=UA, timeout=timeout)
            if resp.status_code < 500:
                return resp
            last_exc = None
        except Exception as exc:  # noqa: BLE001 — 任何「問不到」都值得重試
            resp, last_exc = None, exc
        if attempt < _LIVE_RETRIES - 1 and _LIVE_BACKOFF:
            time.sleep(_LIVE_BACKOFF * (2 ** attempt))
    if resp is not None:
        return resp  # 連續 5xx —— 交給呼叫端照常報 HTTP 狀態碼
    raise last_exc  # type: ignore[misc]


def check_published_mirror(cfg: dict) -> list[Result]:
    """PWA 真的讀得到的那兩個檔,現在是什麼狀態。

    為什麼這一項必須存在
    --------------------
    2026-10-19 起趨勢腿與 VIX 腿的**第一順位**都是本專案發布到 ops-data 的
    鏡像(見 publish_pwa.py)。這條路把「免費 CORS 代理全死」這個問題拿掉了,
    但同時把一個新的單點故障放進來:**鏡像本身**。

    而它的死法會是靜默的。publish 步驟是 `continue-on-error`(它不該有能力
    讓每日健檢變紅燈),心跳 commit 在沒有差異時會跳過 —— 所以
    「publish 掛了」和「今天沒有新資料」在 workflow 的顏色上長得一模一樣。
    PWA 那端也不會叫:它會靜靜退到第二、第三順位,畫面照樣有數字。

    所以這裡**不讀本機檔案,讀線上那一份** —— 問的是使用者的瀏覽器此刻
    拿得到什麼,不是「我們剛剛算出了什麼」。這兩件事在 publish 成功但
    push 失敗的時候會不一樣,而那正是要抓的情況。

    ⚠ 讀到的是【上一次執行】推上去的那一份(本次的 publish 在後面才跑),
    所以容忍度要容得下一個排程週期 + 週末。`mirror.max_age_days` 預設 5。
    """
    mcfg = cfg["pwa"]["mirror"]
    out: list[Result] = []
    today = today_utc()

    # ⚠ 兩條腿的區域變數**刻意不共用名字**(sp_* / vix_*)。
    #    2026-10-20 之前兩段都叫 `months` / `last` / `age`,而 `age` 只在 S&P
    #    那一段被指派 —— Python 的區域變數是函式範圍的,所以 VIX 那段比的是
    #    S&P 的年齡。實測三種後果:VIX 停 300 天報 OK(detail 寫「最後
    #    2025-12-24(1 天前)」,日期和天數自己對不起來);S&P 提早分支時
    #    VIX 報「讀取失敗:cannot access local variable 'age'」;S&P 過期時
    #    把正常的 VIX 拖成假警報。既藏真故障,也造假故障。
    #
    #    既有測試看不見它,因為每一條年齡測試都把兩份鏡像一起搬到同一天,
    #    洩漏來的值剛好等於正確值。同檔案在 404 那條早就寫了「兩項必須獨立
    #    判斷」,年齡這條沒有人套用。名字分開之後,接錯會是 NameError 而不是
    #    一個看起來合理的錯數字。

    # --- S&P 月序列 ---
    try:
        r = _get_live(mcfg["sp_url"])
        if r.status_code != 200:
            out.append(Result("PWA 資料鏡像 · S&P", FAIL,
                              f"HTTP {r.status_code} —— PWA 會退到代理鏈/月頻"))
        else:
            payload = r.json()
            sp_months = len(payload.get("chart", {}).get("result", [{}])[0]
                            .get("timestamp", []) or [])
            sp_last = _mirror_last_day(payload)
            if sp_months < mcfg["min_sp_months"]:
                out.append(Result(
                    "PWA 資料鏡像 · S&P", FAIL,
                    f"只有 {sp_months} 個月,低於下限 {mcfg['min_sp_months']} —— "
                    f"PWA 的 spFromYahoo 會丟掉它並退到下一層"))
            elif sp_last is None:
                out.append(Result("PWA 資料鏡像 · S&P", FAIL,
                                  "格式不符:取不到 chart.result[0].timestamp"))
            else:
                sp_age = _days_between(sp_last, today)
                if sp_age > mcfg["max_age_days"]:
                    out.append(Result(
                        "PWA 資料鏡像 · S&P", FAIL,
                        f"最後一筆 {sp_last},已 {sp_age} 天"
                        f"(上限 {mcfg['max_age_days']})—— 發布或心跳推送壞了,"
                        f"使用者看到的是舊資料"))
                else:
                    out.append(Result(
                        "PWA 資料鏡像 · S&P", OK,
                        f"{sp_months} 個月,最後 {sp_last}({sp_age} 天前)"))
    except Exception as exc:  # noqa: BLE001
        out.append(Result("PWA 資料鏡像 · S&P", FAIL, f"讀取失敗:{exc}"))

    # --- VIX 日序列 ---
    try:
        r = _get_live(mcfg["vix_url"])
        if r.status_code != 200:
            out.append(Result("PWA 資料鏡像 · VIX", FAIL,
                              f"HTTP {r.status_code} —— PWA 會完全沒有 VIX 腿"))
        else:
            df = pd.read_csv(io.StringIO(r.text))
            need = ["DATE", "HIGH", "CLOSE"]
            missing = [c for c in need if c not in df.columns]
            if missing:
                out.append(Result(
                    "PWA 資料鏡像 · VIX", FAIL,
                    f"缺欄位 {missing} —— buildVixMonthly 讀不到,"
                    f"VIX 腿會整個消失"))
            else:
                vix_months = df["DATE"].astype(str).str.slice(0, 7).nunique()
                vix_last = str(df["DATE"].iloc[-1]) if len(df) else ""
                # ⚠ 先確認最後一筆【真的是一個日期】。
                #    `_days_between` 對解析不出來的輸入回 0(那是為了通知路徑
                #    設計的),所以一份 DATE 欄全是垃圾、但剛好有 132 個不同
                #    前七碼的檔案,會得到 age=0 而被判成 OK —— 檢查看起來在把關,
                #    實際上放行了一份 PWA 根本算不出東西的鏡像。
                #    S&P 那一邊靠 `_mirror_last_day()` 回 None 已經擋住了,
                #    這一邊少了對應的那一道。(與 CLAUDE.md 的推論同一個形狀:
                #    「失敗時回安全值」在量測路徑上是陷阱。)
                if _as_date(vix_last) is None:
                    out.append(Result(
                        "PWA 資料鏡像 · VIX", FAIL,
                        f"最後一筆的 DATE 不是日期({vix_last!r})—— "
                        f"檔案格式壞了,buildVixMonthly 會算出空的 VIX 腿"))
                elif vix_months < mcfg["min_vix_months"]:
                    out.append(Result(
                        "PWA 資料鏡像 · VIX", FAIL,
                        f"只有 {vix_months} 個月,低於下限 {mcfg['min_vix_months']}"
                        f"(圖要 96 個月 + 滾動 z 分數要 36 個月)—— 歷史圖會自己變短"))
                else:
                    vix_age = _days_between(vix_last, today)
                    if vix_age > mcfg["max_age_days"]:
                        out.append(Result(
                            "PWA 資料鏡像 · VIX", FAIL,
                            f"最後一筆 {vix_last},已 {vix_age} 天"
                            f"(上限 {mcfg['max_age_days']})—— 發布或心跳推送壞了"))
                    else:
                        out.append(Result("PWA 資料鏡像 · VIX", OK,
                                          f"{len(df)} 列 / {vix_months} 個月,"
                                          f"最後 {vix_last}({vix_age} 天前)"))
    except Exception as exc:  # noqa: BLE001
        out.append(Result("PWA 資料鏡像 · VIX", FAIL, f"讀取失敗:{exc}"))

    return out


def mirror_is_serving(results: list[Result]) -> bool:
    """鏡像這兩項是不是都沒失敗。

    代理鏈的嚴重程度要看這個 —— 見 check_proxies 的說明。
    刻意用「沒有 FAIL」而不是「全部 OK」:鏡像項目目前不會產生 SKIP,
    但若將來多了一個無法判斷的情況,「判不出來」不該被當成「壞了」
    而把代理鏈推成紅燈。
    """
    return not any(r.failed for r in results if r.name.startswith("PWA 資料鏡像"))


def check_pwa_deploy(cfg: dict) -> list[Result]:
    """線上的 PWA 是不是還活著、而且是預期的那一版。"""
    url = cfg["pwa"]["url"]
    try:
        # 走 _get_live:一次網路抽風不該報成「PWA 掛了」。判準沒有移動 ——
        # 真的連不上、或標記真的不見了,三次之後照樣 FAIL。
        r = _get_live(url)
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
        r = _get_live(url, timeout=15)
        if r.status_code == 200 and '"chart"' in r.text[:400]:
            return [Result("自架 Worker", OK, f"HTTP 200,{len(r.text) // 1024} KB")]
        return [Result("自架 Worker", FAIL,
                       f"HTTP {r.status_code},回應片段 {safe_snippet(r.text)}")]
    except Exception as exc:  # noqa: BLE001
        return [Result("自架 Worker", FAIL, f"無法連線:{exc}")]


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def run_all() -> list[Result]:
    cfg = load_config()
    results: list[Result] = []

    # ⚠ 這個順序是【承重】的,不是風格。`data/raw/*.csv` 不進版控,workflow 也
    # 只從 ops-data 取回 manifest 與 health_log —— 所以在乾淨的 runner 上,
    # 底下三項能不能跑完全取決於 check_sources_reachable() 有沒有先把檔案存下來。
    #
    # 這條依賴在本機【看不出來】(本機 raw 檔一直在)。重排順序的話:
    # 本機測試照樣全綠,線上會冒出一整排「檔案不存在」的假紅燈。
    # 由 test_health_check.py::test_check_order_is_load_bearing 釘住。
    reachable = check_sources_reachable(cfg)
    results += reachable
    down = unreachable_sources(reachable)

    results += check_freshness(cfg, down)
    results += check_sanity(cfg, down)
    results += check_column_degradation(cfg)
    # 鏡像必須排在代理鏈【之前】—— 代理鏈的嚴重程度取決於鏡像有沒有在供應
    # (見 check_proxies 的說明)。倒過來的話代理鏈會永遠以為鏡像是好的。
    mirror = check_published_mirror(cfg)
    results += mirror
    results += check_proxies(cfg, mirror_ok=mirror_is_serving(mirror))
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
    # ⚠ 判準是「檔案裡有沒有東西」,不是「檔案存不存在」。
    #
    #   workflow 第 5 步用 `git show origin/ops-data:...  > ops/data/health_log.csv`
    #   取回既有軌跡。shell 的重導向**在 git show 執行之前就把檔案截斷成 0 bytes**,
    #   所以那一步失敗時留下的是一個【存在但空的】檔案。
    #   只看 exists() 的話,這裡會判定「已經有表頭」→ 直接追加一列資料,
    #   於是檔案變成沒有表頭的 CSV。下一次 `pd.read_csv` 會把那一列當成欄位名:
    #       欄位 = ['2026-10-19', '1', '1.1', 'Unnamed: 3']   列數 = 0
    #   —— 稽核軌跡靜靜變成一張空表,而年度健檢的「資料源穩定度」會說沒有紀錄。
    #
    #   workflow 那一邊也修了(不成功就不覆蓋目的檔),但這一道留著:
    #   表頭的正確性是這個函式自己的責任,不該依賴呼叫它的那段 shell 寫對。
    header = not HEALTH_LOG.exists() or HEALTH_LOG.stat().st_size == 0
    with HEALTH_LOG.open("a", encoding="utf-8", newline="\n") as fh:
        if header:
            fh.write("date,ok,n_checks,failed\n")
        fh.write(f"{row['date']},{row['ok']},{row['n_checks']},{row['failed']}\n")


# --------------------------------------------------------------------------
# Issue 同步 —— 讓標題與內文反映【目前】的狀態(2026-10-14)
#
# 去重規則原本的 key 是「有沒有開放的 health-fail Issue」,不是「哪幾項在失敗」。
# 實測後果:Issue #2 從 2026-08-21 連紅 54 天、52 則留言,而這段期間失敗項目
# 換過 6 次 —— 包含兩個真正影響使用者看到的數字的新故障(代理鏈全死、VIX 停更)。
# 它們以「第 N 則留言」的形式出現在一個已經 52 則的討論串裡,標題仍凍結在
# 第一個、而且早就自己好了的原因。
#
# `config.yml` 開頭自己寫的:「狼來了比漏報更致命 —— 一旦你開始無視通知,
# 整層就等於不存在。」連紅 54 天的 Issue 就是那個狼來了。它每天都正確地
# 報告了事實,而資訊傳達完全失敗。
#
# 修法刻意【不動】去重的核心(仍然只有一個 Issue —— 那條不變量是為了避免
# 一年 365 個 Issue,它是對的),改的是報告層:
#   1. 內文每次都換成當前報告(而不是留在第一天那一份)
#   2. 標題寫出【目前】在失敗的是什麼
#   3. 只在失敗集合【變動】時留言,而且把新增/恢復明確列出來
# 逐日軌跡本來就有 health_log.csv 當 append-only 稽核軌跡,不需要靠留言。
# --------------------------------------------------------------------------

_MARKER_PREFIX = "<!-- failset: "
_MARKER_SUFFIX = " -->"
_SINCE_PREFIX = "<!-- failsince: "
_NOTICE_PREFIX = "<!-- lastnotice: "

# 嚴重程度只看【持續了多久】。這是這一層唯一有的、而且不會自己騙自己的維度 ——
# 「這個來源死了 3 天」和「死了 90 天」是兩件完全不同的事,但失敗集合一模一樣。
#
# 分級刻意粗糙。精細的分級會假裝有精度,而這些門檻沒有任何統計依據,
# 它們只是「人對時間長度的直覺斷點」。同 Warning.band 的設計。
SEVERITY_DAYS = (7, 30, 90)
SEVERITY_LABEL = ("剛發生", "持續一週以上", "持續一個月以上", "持續三個月以上")

# 什麼都沒變化時,隔多久再提醒一次。
# 不是每天 —— 每天留言正是把 Issue 洗成沒人看的討論串的那個行為。
# 30 天是「久到你已經忘記它還開著」的量級。
REMINDER_DAYS = 30


def failset(results: list[Result]) -> list[str]:
    """目前失敗的檢查項名稱,排序後。這是去重與變動判斷的真正 key。"""
    return sorted(r.name for r in results if r.failed)


def failset_marker(results: list[Result]) -> str:
    """把失敗集合編碼成一行 HTML 註解,藏在 Issue 內文裡當狀態。

    為什麼不去解析內文的散文:文案改一次,解析就無聲壞掉 ——
    而它壞掉的症狀是「永遠判定沒有變動」,也就是這個修法完全失效且沒有症狀。
    """
    return _MARKER_PREFIX + "|".join(failset(results)) + _MARKER_SUFFIX


def parse_failset(body: str | None) -> list[str] | None:
    """從既有 Issue 內文讀回上次的失敗集合。沒有標記回 None(=「不知道」)。

    None 與空列表必須分得開:None 是「這個 Issue 是舊格式或剛建立」,
    空列表是「上次沒有任何失敗」。前者不該被當成「全部都是新增的」。
    """
    if not body or _MARKER_PREFIX not in body:
        return None
    tail = body.split(_MARKER_PREFIX, 1)[1]
    if _MARKER_SUFFIX not in tail:
        return None
    raw = tail.split(_MARKER_SUFFIX, 1)[0]
    return sorted(n for n in raw.split("|") if n)


def _kv_marker(prefix: str, pairs: dict[str, str]) -> str:
    return prefix + "|".join(f"{k}={v}" for k, v in sorted(pairs.items())) + _MARKER_SUFFIX


def _parse_kv(body: str | None, prefix: str) -> dict[str, str]:
    if not body or prefix not in body:
        return {}
    tail = body.split(prefix, 1)[1]
    if _MARKER_SUFFIX not in tail:
        return {}
    out: dict[str, str] = {}
    for token in tail.split(_MARKER_SUFFIX, 1)[0].split("|"):
        if "=" in token:
            k, _, v = token.partition("=")
            out[k] = v
    return out


def _as_date(x: str | date | None) -> date | None:
    """ISO 字串或 date → date。其餘 → None。"""
    if isinstance(x, date):
        return x
    if not x:
        return None
    try:
        return date.fromisoformat(x)
    except (ValueError, TypeError):
        return None


def _days_between(a: str | date | None, b: str | date | None) -> int:
    """兩個日期差幾天。任一邊缺或壞 → 0。

    回 0 而不是拋:這條路上的日期來自【上一次寫進 Issue 內文的標記】,
    而那是外部狀態 —— 被人手動編輯、被舊版寫壞、或根本還不存在都有可能。
    一個解析不出來的日期不該讓整個健檢的通知流程掛掉。

    ⚠ 2026-10-19 加上 `date` 型別。原本只吃 ISO 字串,而傳進一個
    `date.today()` 會在 `date.fromisoformat()` 拋 TypeError 之後
    被上面那層「壞的就回 0」吞掉 —— **於是檢查永遠算出 0 天,永遠不會紅**。
    `check_published_mirror` 的新鮮度判準就是這樣整條變成空轉的,
    而它看起來完全正確。由 test_mirror_check.py 的 age 兩條測試抓到。

    「回 0 比較安全」對通知路徑是對的(外部狀態不可信),對**量測**路徑
    是錯的(型別寫錯會變成無聲的永遠通過)。兩種用途共用一個函式,
    就必須讓它對兩種輸入都真的算得出來。
    """
    da, db = _as_date(a), _as_date(b)
    if da is None or db is None:
        return 0
    return (db - da).days


def severity(days: int) -> int:
    """持續天數 -> 等級 0..3。"""
    return sum(1 for t in SEVERITY_DAYS if days >= t)


@dataclass(frozen=True)
class Notice:
    """這一次要不要出聲、出聲要說什麼、以及新的狀態標記。"""

    comment: str | None          # None = 不要留言
    state_markers: list[str]     # 要嵌進 Issue 內文的狀態標記


def plan_notice(previous_body: str | None, names: list[str],
                detail: dict[str, str], today: str) -> Notice:
    """決定今天要不要在 Issue 上留言。

    三種事件才出聲,其餘一律安靜 —— 每天留一則「仍未恢復」正是把 Issue
    洗成沒人看的討論串的那個行為(實測 52 則)。

    | 事件 | 為什麼它值得一則通知 |
    |---|---|
    | 失敗集合變動 | 「壞的東西換了」和「同一個東西還在壞」是兩件事 |
    | 嚴重程度跨級 | `{vix}` 第 1 天和第 60 天集合一模一樣,嚴重程度完全不同 |
    | 太久沒出聲   | 久到你已經忘記它還開著。週期提醒,不是每天重複 |

    狀態存在內文的 HTML 註解標記裡:
      `failsince`  —— 每一項第一次失敗的日期 + **上次已通知的等級**
      `lastnotice` —— 上次真的留言是哪一天

    ⚠ 等級是**存下來**的,不是回頭推算的。推算需要同時知道「第一次失敗是哪天」
      與「上次留言是哪天」,兩個狀態互相依賴就會產生只在特定時序才重現的錯誤 ——
      而這種錯誤的症狀是「某次該提醒卻沒提醒」,完全無聲。
    """
    prev = _parse_kv(previous_body, _SINCE_PREFIX)
    last_notice = _parse_kv(previous_body, _NOTICE_PREFIX).get("at")
    known_before = parse_failset(previous_body)

    since: dict[str, str] = {}
    notified_lv: dict[str, int] = {}
    for n in names:
        raw = prev.get(n, "")
        s, _, lv = raw.partition(",")
        since[n] = s or today
        notified_lv[n] = int(lv) if lv.isdigit() else 0

    now_lv = {n: severity(_days_between(since[n], today)) for n in names}

    added = [n for n in names if n not in (known_before or [])] if known_before is not None else []
    gone = [n for n in (known_before or []) if n not in names]
    escalated = [n for n in names if now_lv[n] > notified_lv[n] and n not in added]
    overdue = last_notice is None or _days_between(last_notice, today) >= REMINDER_DAYS
    first_time = known_before is None

    speak = bool(added or gone or escalated or overdue or first_time)

    if speak:
        lines = [f"### 健檢狀態更新 — {today}", ""]
        if first_time:
            lines += ["(這個 Issue 先前沒有記錄失敗項目,以下是目前的狀態。)", ""]
        if added:
            lines += ["**新增失敗:**", ""] + [f"- ❌ {n}" for n in added] + [""]
        if gone:
            lines += ["**已恢復:**", ""] + [f"- ✅ {n}" for n in gone] + [""]
        if escalated:
            lines += ["**持續時間跨級**(集合沒變,但嚴重程度變了):", ""]
            lines += [f"- ⏱ {n} — {SEVERITY_LABEL[now_lv[n]]}"
                      f"(自 {since[n]} 起,已 {_days_between(since[n], today)} 天)"
                      for n in escalated] + [""]
        if overdue and not (added or gone or escalated or first_time):
            # last_notice 可能是 None —— 舊版寫的 Issue 只有 failset 標記、
            # 沒有 lastnotice。那是遷移情境,不是「0 天前剛留過言」。
            lines += ([f"**狀態沒有變化,但已經 {_days_between(last_notice, today)} 天"
                       f"沒有更新這個 Issue 了** —— 以下是現況提醒。", ""]
                      if last_notice else
                      ["**這個 Issue 先前沒有記錄上次通知的時間** —— 以下是現況,"
                       f"之後狀態沒變化時最多每 {REMINDER_DAYS} 天提醒一次。", ""])
        lines += ["**目前失敗的全部項目:**", ""]
        lines += [f"- **{n}** — {detail.get(n, '')}"
                  f"(自 {since[n]} 起,{_days_between(since[n], today)} 天)"
                  for n in names] or ["- (無)"]
        lines += ["", "---",
                  "逐日軌跡在 `ops-data` 分支的 `ops/data/health_log.csv`(append-only)。",
                  "本留言只在**失敗項目變動、嚴重程度跨級、或太久沒更新**時出現 ——",
                  f"「仍未恢復」不會每天洗一則(最長 {REMINDER_DAYS} 天提醒一次)。"]
        comment = "\n".join(lines)
    else:
        comment = None

    # 有留言才把等級推上去。`notified_lv` 的語意是**上次告訴使用者的等級**,
    # 沒說出口就不該前進 —— 否則下一次的跨級會被無聲吃掉。
    #
    # ⚠ 這個條件式目前是**等價**的(2026-10-19 突變測試證實):只要任一項等級
    #   上升,它必定落在 `added` 或 `escalated` 裡,於是 speak 必為真。
    #   留著不是防禦性程式碼,是因為它釘住的是語意而不是現況 ——
    #   哪天 speak 的條件多了一條抑制(例如留言頻率上限),沒有它就會
    #   出現「等級前進了但使用者沒被告知」的無聲漏報。
    new_state = {n: f"{since[n]},{now_lv[n] if speak else notified_lv[n]}" for n in names}
    return Notice(
        comment=comment,
        state_markers=[
            _kv_marker(_SINCE_PREFIX, new_state),
            _kv_marker(_NOTICE_PREFIX, {"at": today if speak else (last_notice or today)}),
        ],
    )


def issue_title(results: list[Result]) -> str:
    """標題必須說出【目前】在失敗的是什麼。

    原本是 `健檢異常 · <第一次失敗的日期>`,於是 54 天後標題講的還是
    那個早就恢復了的 shiller,而真正在燒的兩件事只能在第 52 則留言裡看到。
    """
    names = failset(results)
    if not names:
        return "健檢異常"
    short = "、".join(n.replace(" · ", " ") for n in names)
    if len(short) > 60:
        short = short[:59] + "…"
    return f"健檢異常({len(names)} 項)· {short}"


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
        "本 Issue 由 `ops/src/health_check.py` 自動建立,**內文每天換成最新狀態**。",
        "逐日軌跡在 `ops-data` 分支的 `ops/data/health_log.csv`。修好之後下一次健檢會自動關閉它。",
        "診斷方式:把這個 Issue 丟給 Claude Code,或本機執行 "
        "`python ops/src/health_check.py --verbose`。",
        "",
        failset_marker(results),   # 下一次執行靠這一行判斷失敗項目有沒有變動
    ]
    return "\n".join(lines)


def with_state_markers(report: str, markers: list[str]) -> str:
    """把通知狀態標記接到報告末尾。

    分成兩步是因為 `render_markdown()` 只看得到**這一次**的檢查結果,
    而「這一項是從哪天開始壞的」必須從上一次的 Issue 內文讀回來。
    那個歷史只有 `issue_plan()` 手上有。
    """
    return report.rstrip("\n") + "\n" + "\n".join(markers) + "\n"


def issue_plan(report: str, old_body: str | None) -> dict:
    """給 workflow 用的決策:標題要改成什麼、要不要留言、留什麼。

    吃的是**已經產生好的報告文字**,不重跑檢查 —— 重跑會多打一輪網路,
    而且兩次結果可能不一樣,那會讓標題和內文講不同的事。

    注意這裡只從報告裡取失敗項目名稱,不需要 Result 物件:報告本身就是
    那次執行的事實,workflow 已經把它存成檔案了。
    """
    # 名稱從【標記】讀,不是去解析散文 —— 文案改一次,散文解析就無聲壞掉,
    # 而它壞掉的症狀是「永遠判定沒有變動」,也就是這個修法完全失效且沒有症狀。
    names = parse_failset(report) or []

    # 細節只能從散文取,所以是 best-effort:取不到就留空字串,
    # 名稱仍然是對的,留言仍然有用。細節是附加價值,不是判斷依據。
    detail: dict[str, str] = {}
    for line in report.splitlines():
        if line.startswith("- **") and "—" in line:
            head, _, tail = line.partition("—")
            detail[head.split("**", 2)[1]] = tail.strip()

    current = [Result(n, FAIL, detail.get(n, "")) for n in names]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    notice = plan_notice(old_body, names, detail, today)
    return {
        "changed": notice.comment is not None,
        "title": issue_title(current),
        "comment": notice.comment or "",
        "body": with_state_markers(report, notice.state_markers),
        "failed": names,
    }


def main() -> int:
    force_utf8()  # Windows 的 cp950 印不出 ✅❌⏭,不設會直接 UnicodeEncodeError
    ap = argparse.ArgumentParser(description="現金水位儀每日健檢")
    ap.add_argument("--verbose", action="store_true", help="一律列出每一項")
    ap.add_argument("--markdown", action="store_true", help="輸出 markdown 給 Issue 用")
    ap.add_argument("--no-log", action="store_true", help="不要追加 health_log.csv")
    ap.add_argument(
        "--issue-plan", nargs=2, metavar=("REPORT", "OLD_BODY"),
        help="不跑檢查。讀報告與既有 Issue 內文,輸出 JSON 決策給 workflow 用 "
             "(OLD_BODY 可以是不存在的路徑,代表還沒有 Issue)",
    )
    args = ap.parse_args()

    if args.issue_plan:
        import json

        report_path, old_path = (Path(p) for p in args.issue_plan)
        old = old_path.read_text(encoding="utf-8") if old_path.exists() else None
        print(json.dumps(issue_plan(report_path.read_text(encoding="utf-8"), old),
                         ensure_ascii=False))
        return 0

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
