"""PWA 資料鏡像 —— publish_pwa.py。

這一層的存在理由:瀏覽器抓不到 Yahoo / CBOE(沒有 CORS 標頭),而免費代理
在 2026-10 已全數失效。改由每日 workflow 在 server 端抓好、推到 ops-data,
PWA 直接讀 raw.githubusercontent.com(送 `ACAO: *`)。

所以這裡要釘住的不是「程式有跑」,而是三件會讓使用者看到錯數字的事:
  1. 只發月底那一天,與發全段日K 的計算結果必須【完全相同】(省 95% 流量的依據)
  2. 月份分組必須用 UTC —— 用本地時區的話,月底那根會被算進下個月
  3. 發出去的欄位必須是 PWA 現有解析器讀得懂的那幾個
"""

from __future__ import annotations

import csv
import io
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

import publish_pwa

TESTS_DIR = Path(__file__).resolve().parent
FIXTURES = TESTS_DIR / "fixtures"
MIRROR_BRIDGE = TESTS_DIR / "mirror_bridge.mjs"


def _yahoo_shape(dates: list[str], closes: list[float]) -> dict:
    """把 ISO 日期 + 收盤包成 Yahoo chart JSON(全段日K 的對照組)。"""
    ts = [int(pd.Timestamp(d, tz="UTC").timestamp()) for d in dates]
    return {"chart": {"error": None, "result": [{
        "timestamp": ts,
        "indicators": {"quote": [{"close": closes}]},
    }]}}


# --------------------------------------------------------------------------
# 1. 恆等式:只發月底 == 發全段
# --------------------------------------------------------------------------

def test_month_end_only_is_identical_to_full_daily(sp_daily: pd.DataFrame):
    """Python 側:取每月最後一筆,等於對全段分組後取每月最後一筆。

    這是「只發 19 KB 而不是 421 KB」的依據。若哪天 publish 改成取
    月均或月初,這裡會抓到 —— 而那會讓畫面上的趨勢腿與回測基準脫鉤。
    """
    payload = publish_pwa.sp_monthly_payload(sp_daily)
    ts = payload["chart"]["result"][0]["timestamp"]
    closes = payload["chart"]["result"][0]["indicators"]["quote"][0]["close"]

    got = {datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m"): c
           for t, c in zip(ts, closes)}

    df = sp_daily.copy()
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df = df.sort_values("date")
    df["ym"] = df["date"].dt.strftime("%Y-%m")
    want = df.groupby("ym")["close"].last().to_dict()

    assert got == pytest.approx(want)
    assert len(got) == len(want) > 100


def test_one_row_per_month_and_it_is_the_last_trading_day(sp_daily: pd.DataFrame):
    """每個月只能有一筆,而且必須是該月最後一個交易日。"""
    payload = publish_pwa.sp_monthly_payload(sp_daily)
    ts = payload["chart"]["result"][0]["timestamp"]
    days = [datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d") for t in ts]
    months = [d[:7] for d in days]
    assert len(months) == len(set(months)), "同一個月出現多筆"
    assert months == sorted(months), "月份沒有遞增"

    df = sp_daily.copy()
    df["ym"] = df["date"].astype(str).str.slice(0, 7)
    last = df.groupby("ym")["date"].max().astype(str).to_dict()
    assert days == [last[m] for m in months]


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")
def test_real_pwa_spFromYahoo_sees_them_as_identical(
    index_html: Path, sp_daily: pd.DataFrame, tmp_path: Path
):
    """用【PWA 自己那支 spFromYahoo】驗同一條恆等式。

    Python 側那條測試用的是我寫的分組;真正跑在使用者瀏覽器裡的是這一支。
    兩邊都問過才算驗過 —— 這正是 js_bridge 存在的理由。
    """
    if not (TESTS_DIR.parent / "node_modules" / "jsdom").exists():
        pytest.skip("尚未 npm install jsdom")

    df = sp_daily.copy()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"])
    full = _yahoo_shape(df["date"].astype(str).tolist(), df["close"].tolist())
    month_end = publish_pwa.sp_monthly_payload(sp_daily)

    (tmp_path / "full.json").write_text(json.dumps(full), encoding="utf-8")
    (tmp_path / "me.json").write_text(json.dumps(month_end), encoding="utf-8")

    proc = subprocess.run(
        ["node", str(MIRROR_BRIDGE), str(index_html),
         str(tmp_path / "full.json"), str(tmp_path / "me.json")],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    if proc.returncode != 0:
        pytest.fail("橋接器失敗(exit %d):\n%s" % (proc.returncode, proc.stderr))
    out = json.loads(proc.stdout)
    assert len(out["full"]) > 100
    assert out["identical"], (
        "PWA 的 spFromYahoo 對兩種輸入給出不同結果 —— "
        "全段 %d 個月,月底版 %d 個月" % (len(out["full"]), len(out["monthEnd"]))
    )


# --------------------------------------------------------------------------
# 2. UTC 分月
# --------------------------------------------------------------------------

def test_month_boundary_uses_utc_not_local_time():
    """分月必須用 UTC。

    `spFromYahoo()` 用 `toISOString().slice(0,7)` 分月,也就是 UTC。
    Python 側若改用本地時區,月底那根會被算進下個月 —— 而這是靜默的:
    數字照樣算得出來,只是歸到錯的月份。

    ⚠ 輸入刻意帶時區與時間,不是只有日期。
    ------------------------------------------------------------------
    我第一版用 `["2026-01-29", "2026-01-30", "2026-02-02"]` ——
    **那條測試是空轉的**。純日期字串在 `utc=True` 與不給 `utc` 之下
    解析出完全相同的日曆日與 epoch(pandas 對 naive Timestamp 的
    `.timestamp()` 本來就當 UTC),所以拿掉 `utc=True` 的突變測不出來。
    實測:把 `utc=True` 拿掉,264 條測試全部照樣通過。

    會分岔的輸入必須同時有「時間」與「偏移」:
        2026-02-01T07:00:00+08:00  ->  utc=True 給 2026-01
                                       無 utc   給 2026-02
    `_fetch_yahoo_daily()` 目前落地的是純日期,所以這種輸入現在不會出現 ——
    但 Yahoo 的原始 timestamp 打在 13:30/14:30 UTC,哪天上游格式改成帶
    時間的 ISO,`utc=True` 就是唯一擋住錯月份的東西。這條測試釘的是那個契約。
    """
    sp = pd.DataFrame({
        "date": ["2026-01-02T05:00:00+08:00", "2026-02-01T07:00:00+08:00"],
        "close": [100.0, 101.0],
    })
    payload = publish_pwa.sp_monthly_payload(sp)
    ts = payload["chart"]["result"][0]["timestamp"]
    months = [datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m") for t in ts]
    # 兩筆在 UTC 下都落在一月 -> 分組後只剩一筆(該月最後一筆,101.0)。
    # 用本地時區(+08)的話會變成一月與二月各一筆 —— 月份數就是判別點。
    assert months == ["2026-01"], (
        "2026-02-01T07:00+08:00 的 UTC 時刻是 2026-01-31 23:00 —— "
        "必須歸到一月。出現 2026-02 表示分月用了本地時區"
    )
    assert payload["chart"]["result"][0]["indicators"]["quote"][0]["close"] == [101.0]

    # 控制組:純日期輸入下兩種寫法等價,所以上面那組輸入是必要的
    plain = pd.DataFrame({
        "date": ["2026-01-29", "2026-01-30", "2026-02-02"],
        "close": [100.0, 101.0, 102.0],
    })
    got = publish_pwa.sp_monthly_payload(plain)["chart"]["result"][0]
    assert [datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m")
            for t in got["timestamp"]] == ["2026-01", "2026-02"]
    assert got["indicators"]["quote"][0]["close"] == [101.0, 102.0]


# --------------------------------------------------------------------------
# 3. 欄位契約 —— PWA 現有解析器讀得懂
# --------------------------------------------------------------------------

def test_vix_payload_keeps_exactly_the_columns_the_pwa_reads(vix_daily: pd.DataFrame):
    """`buildVixMonthly()` 只讀 DATE / CLOSE / HIGH。

    多發 OPEN / LOW 會讓每天的 commit 大一倍而沒人用到;少發 HIGH 會讓
    「月內飆升」項默默退回用收盤 —— 歷史上有 9 個月份因此翻轉現金水位。
    """
    text = publish_pwa.vix_payload(vix_daily, years=50,
                                   now=datetime(2026, 10, 19, tzinfo=timezone.utc))
    header = next(csv.reader(io.StringIO(text)))
    assert header == ["DATE", "HIGH", "CLOSE"]


def test_vix_payload_window_covers_the_chart_plus_the_rolling_window(
    vix_daily: pd.DataFrame
):
    """發出去的月份數必須夠畫 96 個月的圖,外加 36 個月的滾動 z 分數。

    不夠的話圖會自己變短,而畫面上看不出是資料被裁掉還是歷史真的只有這麼長。
    """
    text = publish_pwa.vix_payload(vix_daily, now=datetime(2026, 10, 19,
                                                           tzinfo=timezone.utc))
    months = {r["DATE"][:7] for r in csv.DictReader(io.StringIO(text))}
    assert len(months) >= 96 + 36, "只有 %d 個月,不足 132" % len(months)


def test_vix_payload_values_are_unchanged_by_the_slice(vix_daily: pd.DataFrame):
    """切窗只能影響【發幾筆】,不能影響【發出去的數字】。"""
    now = datetime(2026, 10, 19, tzinfo=timezone.utc)
    text = publish_pwa.vix_payload(vix_daily, now=now)
    rows = list(csv.DictReader(io.StringIO(text)))
    src = vix_daily.copy()
    src["DATE"] = src["DATE"].astype(str)
    by_date = src.set_index("DATE")
    for r in rows[:: max(1, len(rows) // 40)]:
        assert float(r["CLOSE"]) == pytest.approx(
            float(by_date.loc[r["DATE"], "CLOSE"]), abs=5e-3)
        assert float(r["HIGH"]) == pytest.approx(
            float(by_date.loc[r["DATE"], "HIGH"]), abs=5e-3)


def test_missing_high_falls_back_to_close():
    """HIGH 缺漏時退回用 CLOSE —— 與 engine / PWA 同一條規則。

    發出 NaN 的話 `parseFloat` 會拿到 NaN,`buildVixMonthly` 的
    `isFinite(hi)?hi:cl` 會接住 —— 但那是靠下游補,鏡像自己就該是乾淨的。
    """
    vix = pd.DataFrame({
        "DATE": ["2026-10-14", "2026-10-15"],
        "HIGH": [None, 20.0],
        "CLOSE": [15.0, 19.0],
    })
    text = publish_pwa.vix_payload(vix, years=50,
                                   now=datetime(2026, 10, 19, tzinfo=timezone.utc))
    rows = list(csv.DictReader(io.StringIO(text)))
    assert [r["HIGH"] for r in rows] == ["15.0", "20.0"]


def test_publish_writes_both_files_and_reports_what_it_wrote(
    tmp_path: Path, monkeypatch, sp_daily: pd.DataFrame, vix_daily: pd.DataFrame
):
    """`publish()` 的回傳摘要必須描述它真的寫出去的東西。

    這份摘要會進 workflow 的 log,是「鏡像今天有沒有更新」唯一的人可讀紀錄。
    """
    monkeypatch.setattr(publish_pwa.fetch_data, "load",
                        lambda name, **kw: {"sp_daily": sp_daily,
                                            "vix": vix_daily}[name])
    summary = publish_pwa.publish(out_dir=tmp_path)

    sp_file = tmp_path / publish_pwa.SP_MONTHLY
    vix_file = tmp_path / publish_pwa.VIX_DAILY
    assert sp_file.exists() and vix_file.exists()

    payload = json.loads(sp_file.read_text(encoding="utf-8"))
    assert summary["sp_months"] == len(payload["chart"]["result"][0]["timestamp"])
    assert summary["sp_bytes"] == len(
        sp_file.read_text(encoding="utf-8").encode("utf-8"))
    assert summary["vix_rows"] == len(
        vix_file.read_text(encoding="utf-8").strip().split("\n")) - 1
    assert summary["sp_last"] and summary["vix_last"]


def test_published_json_is_a_strict_superset_of_yahoos_shape(sp_daily: pd.DataFrame):
    """發出去的 JSON 必須保留 `spFromYahoo()` 要的那三層鍵。

    少了任何一層它會丟 `無 Yahoo 資料`,而 PWA 會靜靜退到下一層來源 ——
    使用者只會看到「月頻 · Shiller(退回)」,不會知道鏡像壞了。
    """
    payload = publish_pwa.sp_monthly_payload(sp_daily)
    r = payload["chart"]["result"][0]
    assert isinstance(r["timestamp"], list) and r["timestamp"]
    assert isinstance(r["indicators"]["quote"][0]["close"], list)
    assert len(r["timestamp"]) == len(r["indicators"]["quote"][0]["close"])


# --------------------------------------------------------------------------
# 4. config.yml <-> index.html 不能漂移
# --------------------------------------------------------------------------

def test_config_mirror_urls_match_the_pwa(index_html: Path):
    """健檢探測的網址,必須就是 PWA 真的在讀的那兩個。

    這和代理清單漂移是同一個形狀的故障:健檢盯著一個 PWA 沒在讀的檔案,
    而 PWA 在讀的那個沒人看著 —— 於是鏡像壞掉時健檢是綠的。
    """
    import re

    import yaml

    cfg = yaml.safe_load(
        (TESTS_DIR.parent / "config.yml").read_text(encoding="utf-8"))
    html = index_html.read_text(encoding="utf-8")

    sp = re.search(r'const SP_MIRROR\s*=\s*"([^"]+)"', html)
    vix = re.search(r'const VIX_URL\s*=\s*"([^"]+)"', html)
    assert sp, "index.html 找不到 SP_MIRROR"
    assert vix, "index.html 找不到 VIX_URL"

    assert cfg["pwa"]["mirror"]["sp_url"] == sp.group(1)
    assert cfg["pwa"]["mirror"]["vix_url"] == vix.group(1)

    for url in (sp.group(1), vix.group(1)):
        assert url.startswith("https://raw.githubusercontent.com/"), (
            "只有 raw.githubusercontent.com 會送 Access-Control-Allow-Origin: * —— "
            "換成別的網域,瀏覽器會直接被 CORS 擋掉")
        assert "/ops-data/" in url, "鏡像住在 ops-data 分支,不是 main"


def test_published_filenames_match_what_the_pwa_requests(index_html: Path):
    """publish_pwa 寫出去的檔名,必須就是 PWA 請求的那個檔名。

    改了 publish 端的常數卻忘了改 PWA(或反過來)的話,鏡像會 404,
    PWA 靜靜退到代理鏈 —— 而代理鏈目前是死的。
    """
    import re

    html = index_html.read_text(encoding="utf-8")
    sp = re.search(r'const SP_MIRROR\s*=\s*"([^"]+)"', html).group(1)
    vix = re.search(r'const VIX_URL\s*=\s*"([^"]+)"', html).group(1)
    assert sp.endswith("/" + publish_pwa.SP_MONTHLY)
    assert vix.endswith("/" + publish_pwa.VIX_DAILY)


# --------------------------------------------------------------------------
# 5. 一個來源掛掉不可以凍結另一份鏡像
# --------------------------------------------------------------------------

def test_a_dead_vix_source_does_not_freeze_the_sp_mirror(
    tmp_path: Path, monkeypatch, sp_daily: pd.DataFrame
):
    """VIX 抓不到時,S&P 的鏡像仍然要更新。

    在乾淨的 runner 上這很容易發生:`data/raw/*.csv` 不進版控,只有
    `check_sources_reachable()` 抓成功的那幾個才會落地。原本兩份鏡像
    綁在同一個函式裡一路寫到底,於是 **VIX 掛一天,S&P 的鏡像也跟著凍結** ——
    一個故障擴散成兩個。
    """
    def fake_load(name, **kw):
        if name == "vix":
            raise FileNotFoundError("data/raw/vix_daily.csv 不存在")
        return sp_daily

    monkeypatch.setattr(publish_pwa.fetch_data, "load", fake_load)
    summary = publish_pwa.publish(out_dir=tmp_path)

    assert (tmp_path / publish_pwa.SP_MONTHLY).exists(), "S&P 鏡像不該被 VIX 拖累"
    assert summary["sp_months"] > 100
    assert "vix" in summary["errors"] and "sp" not in summary["errors"]
    assert "FileNotFoundError" in summary["errors"]["vix"]


def test_a_failed_artifact_leaves_the_previous_version_untouched(
    tmp_path: Path, monkeypatch, sp_daily: pd.DataFrame, vix_daily: pd.DataFrame
):
    """失敗的那一份必須沿用舊版,不可以被清空或截斷。

    舊資料好過沒資料 —— PWA 會照常算,而「舊版有多舊」由健檢的
    `PWA 資料鏡像 · VIX` 讀線上那一份去報告。寫出一個空檔或半截檔的話,
    PWA 會靜靜退到下一層,而健檢的格式檢查才會發現,晚了一天。
    """
    monkeypatch.setattr(publish_pwa.fetch_data, "load",
                        lambda name, **kw: {"sp_daily": sp_daily,
                                            "vix": vix_daily}[name])
    publish_pwa.publish(out_dir=tmp_path)
    before = (tmp_path / publish_pwa.VIX_DAILY).read_text(encoding="utf-8")
    assert len(before) > 1000

    def fake_load(name, **kw):
        if name == "vix":
            raise RuntimeError("來源今天掛了")
        return sp_daily

    monkeypatch.setattr(publish_pwa.fetch_data, "load", fake_load)
    summary = publish_pwa.publish(out_dir=tmp_path)
    after = (tmp_path / publish_pwa.VIX_DAILY).read_text(encoding="utf-8")
    assert after == before, "失敗的那一份被動到了"
    assert summary["errors"].keys() == {"vix"}


def test_an_empty_aggregation_raises_instead_of_publishing_nothing(
    tmp_path: Path, monkeypatch
):
    """聚合後一筆都沒有 → 當成失敗,不要寫出一個空的鏡像。

    空的 sp_monthly.json 會讓 `spFromYahoo` 丟例外,PWA 靜靜退到下一層;
    空的 vix_daily.csv 會讓 VIX 腿整個消失。兩者都比「沿用舊版」糟。
    """
    empty_sp = pd.DataFrame({"date": [], "close": []})
    empty_vix = pd.DataFrame({"DATE": [], "HIGH": [], "CLOSE": []})
    monkeypatch.setattr(publish_pwa.fetch_data, "load",
                        lambda name, **kw: {"sp_daily": empty_sp,
                                            "vix": empty_vix}[name])
    summary = publish_pwa.publish(out_dir=tmp_path)
    assert summary["errors"].keys() == {"sp", "vix"}
    assert not (tmp_path / publish_pwa.SP_MONTHLY).exists()
    assert not (tmp_path / publish_pwa.VIX_DAILY).exists()


# --------------------------------------------------------------------------
# 6. workflow 契約:產生得出來,不代表它每天真的被產生
#
# `test_signals_is_pushed_by_the_daily_workflow` 已經為 signals.json 做過
# 這件事(理由是心跳踩過「路徑錯了但沒有症狀」)。鏡像現在是 PWA 的
# **第一順位來源**,而它的整條鏈一直沒有對應的那一條。
# --------------------------------------------------------------------------

WORKFLOW = TESTS_DIR.parents[1] / ".github" / "workflows" / "daily-health.yml"


def _health_steps() -> list[dict]:
    """每日健檢那個 job 的步驟清單(解析 YAML,不是字串比對)。

    刻意只取 `jobs.health.steps` —— autofix 那個 job 在**另一台 runner** 上,
    檔案系統不共用。鏡像若跑在那裡,`data/raw/*.csv` 根本不存在。
    """
    import yaml

    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return wf["jobs"]["health"]["steps"]


def _step_index(steps: list[dict], needle: str) -> int:
    """跑了 `needle` 的那一步的序號。找不到或找到多個就**大聲失敗**。

    ⚠ 這道 assert 是在防「掃描式測試恆真」:步驟哪天被刪掉時,
    順序比較若只是拿到 -1 就會悄悄通過,而那正是最該紅的情況。
    """
    hits = [i for i, s in enumerate(steps) if needle in (s.get("run") or "")]
    assert len(hits) == 1, (
        f"workflow 裡跑 `{needle}` 的步驟有 {len(hits)} 個,預期恰好 1 個"
    )
    return hits[0]


def test_the_daily_workflow_actually_publishes_the_mirror() -> None:
    """鏡像必須真的由每日 workflow 產生。

    這一層的所有測試都證明「`publish_pwa.py` 跑起來會寫出對的東西」,
    但沒有任何一條證明**它每天真的被跑**。沒被跑的症狀不是壞掉,是
    **鏡像凍結在最後一次手動執行的那一天** —— HTTP 200、欄位正確、
    `spFromYahoo()` 解析成功,使用者看到一個長得完全正常的上個月數字。
    就是「還在回 HTTP 200 的死鏡像」那一節的形狀,只是這次的死因是我們自己。
    """
    steps = _health_steps()
    i = _step_index(steps, "src/publish_pwa.py")
    assert steps[i].get("working-directory") == "ops", (
        "publish_pwa.py 用 `src/...` 相對路徑呼叫,working-directory 不是 ops 會直接 404"
    )


def test_publish_runs_after_the_health_check() -> None:
    """鏡像那一步必須排在健檢**之後**。順序是承重的。

    `publish_pwa.py` 讀 `data/raw/*.csv`,而在乾淨的 runner 上那些檔案
    **只有** `check_sources_reachable()` 抓成功才會落地
    (原始 CSV 不進版控,workflow 也只從 ops-data 取回 manifest 與 health_log)。

    排到前面去的話:兩個發布物都 `FileNotFoundError` → `publish()` 兩邊都記
    errors → 鏡像沿用分支上的舊版 → **每天都沿用,永遠不更新**,
    而那一步掛著 `continue-on-error` 所以 run 還是綠的。

    ⚠ 這條依賴**在本機完全看不出來**(本機 raw 檔一直在)——
    和 `test_check_order_is_load_bearing` 釘住 `run_all()` 內部順序
    是同一個理由,只是這次兩步之間的順序寫在 YAML 裡。
    """
    steps = _health_steps()
    check = _step_index(steps, "src/health_check.py --markdown")
    publish = _step_index(steps, "src/publish_pwa.py")
    assert publish > check, (
        f"鏡像在第 {publish} 步、健檢在第 {check} 步 —— "
        f"鏡像讀的是健檢剛落地的 data/raw/*.csv,排在前面會讀到不存在的檔案"
    )


def test_the_heartbeat_stages_the_mirror_directory() -> None:
    """產生了卻沒推上去,PWA 一樣讀不到(signals.json 就踩過這個)。

    鏡像住在 `ops-data`,而心跳步驟是唯一會推那條分支的地方。
    少了 `git add`,`git diff --cached --quiet` 會判定「資料無變化」而整個
    跳過 commit —— **連 log 都不會有異狀**。
    """
    steps = _health_steps()
    i = _step_index(steps, "git push origin ops-data")
    run = steps[i]["run"]
    assert "git add -f ops/data/pwa" in run, (
        "心跳沒有把 ops/data/pwa 加進 index —— 鏡像永遠到不了 ops-data 分支"
    )
    assert "ops/data/pwa/" in run, (
        "心跳沒有把產生出來的鏡像複製進 ops-data 的工作目錄"
    )


def test_a_mirror_failure_cannot_turn_the_daily_check_red() -> None:
    """鏡像那一步失敗時,每日健檢**不得**因此變紅。

    這不是為了讓它「容易綠」。鏡像的真實健康由健檢的 `PWA 資料鏡像`
    兩項負責(讀線上那一份、有年齡門檻),它們才是承重點。
    讓產生步驟也能把 run 變紅的話,一次暫時性失敗就會蓋掉**資料源本身**的
    訊號 —— 而「紅燈看久了就被無視」是這個專案已經實測過的失效模式
    (連紅 54 天、52 則留言)。
    """
    steps = _health_steps()
    i = _step_index(steps, "src/publish_pwa.py")
    assert steps[i].get("continue-on-error") is True, (
        "鏡像產生步驟少了 continue-on-error —— 一次暫時性失敗會把 run 變紅,"
        "而真正的鏡像健康是由健檢的『PWA 資料鏡像』兩項在看"
    )
