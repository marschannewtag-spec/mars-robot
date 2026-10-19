"""L1 健檢的判斷邏輯 —— 這一層在 2026-08-13 之前的覆蓋率是 **0%**。

227 行、整套「沒消息就是好消息」的偵測器,沒有任何測試載入過它。
那代表沒人驗證過它**報得出警**。而它一旦壞掉,症狀就是「一切安靜」——
和系統健康時完全一樣。這是這個架構裡最不能無聲失效的一個檔案。

這裡不測網路(那是 L1 自己在線上做的事),測的是**判斷**:
給它一份壞掉的資料,它認不認得出來、以及認出來之後做了什麼。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from conftest import OPS_ROOT

sys.path.insert(0, str(OPS_ROOT / "src"))

import fetch_data  # noqa: E402
import health_check as hc  # noqa: E402


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch) -> Path:
    """把 fetch_data 的落地路徑導到暫存目錄 —— 絕不碰使用者的真實資料。"""
    raw = tmp_path / "raw"
    raw.mkdir(parents=True)
    monkeypatch.setattr(fetch_data, "RAW_DIR", raw)
    monkeypatch.setattr(fetch_data, "MANIFEST_PATH", tmp_path / "manifest.json")
    monkeypatch.setattr(hc, "HEALTH_LOG", tmp_path / "health_log.csv")
    return raw


@pytest.fixture(scope="module")
def cfg() -> dict:
    return hc.load_config()


def _trading_days(start: date, n: int):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def write_sp(raw: Path, days, price=lambda i: 100.0 + i) -> str:
    text = "date,close\n" + "".join(
        f"{d.isoformat()},{price(i)}\n" for i, d in enumerate(days))
    (raw / fetch_data.SOURCES["sp_daily"].filename).write_text(
        text, encoding="utf-8", newline="\n")
    return text


def write_vix(raw: Path, days, close=20.0, high=22.0) -> None:
    (raw / fetch_data.SOURCES["vix"].filename).write_text(
        "DATE,OPEN,HIGH,LOW,CLOSE\n" + "".join(
            f"{d.isoformat()},{close},{high},{close},{close}\n" for d in days),
        encoding="utf-8", newline="\n")


def statuses(results) -> dict[str, str]:
    return {r.name: r.status for r in results}


def failed_names(results) -> list[str]:
    return [r.name for r in results if r.failed]


# ══════════════════════════════════════════════════════════════════════════
# 這個檔案存在的理由:偵測器要真的偵測得到
#
# 每一條都先確認「壞資料 -> FAIL」,因為一個永遠回 OK 的健檢
# 和沒有健檢是同一件事,而且更糟 —— 它讓人以為有人在看。
# ══════════════════════════════════════════════════════════════════════════


def test_stale_data_is_detected(sandbox: Path, cfg: dict) -> None:
    days = _trading_days(hc.today_utc() - timedelta(days=400), 60)
    write_sp(sandbox, days)
    write_vix(sandbox, days)
    (sandbox / fetch_data.SOURCES["shiller"].filename).write_text(
        "Date,SP500,Dividend,Long Interest Rate\n2024-01-01,3000,50,2.5\n",
        encoding="utf-8", newline="\n")

    assert failed_names(hc.check_freshness(cfg)) == [
        "資料新鮮度 · sp_daily", "資料新鮮度 · vix", "資料新鮮度 · shiller",
    ]


def test_fresh_data_is_not_flagged(sandbox: Path, cfg: dict) -> None:
    """對照組 —— 少了這條,上面那條可能只是「什麼都報 FAIL」。"""
    days = _trading_days(hc.today_utc() - timedelta(days=90), 60)
    write_sp(sandbox, days)
    write_vix(sandbox, days)
    (sandbox / fetch_data.SOURCES["shiller"].filename).write_text(
        f"Date,SP500,Dividend,Long Interest Rate\n"
        f"{hc.today_utc().isoformat()},3000,50,2.5\n",
        encoding="utf-8", newline="\n")

    st = statuses(hc.check_freshness(cfg))
    assert st["資料新鮮度 · sp_daily"] == hc.OK
    assert st["資料新鮮度 · shiller"] == hc.OK


def test_vix_out_of_range_and_duplicate_dates_are_detected(
        sandbox: Path, cfg: dict) -> None:
    days = _trading_days(date(2020, 1, 1), 300)
    write_sp(sandbox, days)
    lines = ["DATE,OPEN,HIGH,LOW,CLOSE"]
    for i, d in enumerate(days):
        c = 250.0 if i == 50 else 20.0          # 遠高於 vix_max
        day = days[i - 1] if i == 80 else d     # 重複日期
        lines.append(f"{day.isoformat()},{c},{c + 2},{c},{c}")
    (sandbox / fetch_data.SOURCES["vix"].filename).write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    st = statuses(hc.check_sanity(cfg))
    assert st["數值合理性 · VIX 範圍"] == hc.FAIL
    assert st["資料完整性 · VIX 無重複日期"] == hc.FAIL


def test_monthly_gap_names_the_right_month(sandbox: Path, cfg: dict) -> None:
    """跳空報告必須指出**發生跳空的那個月**,不是它的前一個月。

    這裡的索引很容易寫錯:`gap` 是 pct_change().dropna() 之後的序列,
    它的「位置」比 `monthly` 少一格,但「標籤」是對齊的。用 argmax()(位置)
    去查 monthly 會整整差一個月,而這條路只在真的跳空時才會跑到 ——
    也就是**只在最需要它正確的時候才有機會出錯**。
    """
    days = _trading_days(date(2020, 1, 1), 600)
    write_sp(sandbox, days, price=lambda i: 100.0 if i < 200 else 50.0)
    write_vix(sandbox, days)

    gap = [r for r in hc.check_sanity(cfg) if "月跳空" in r.name][0]
    assert gap.failed, "注入了 -50% 的月跳空卻沒有報警"
    assert days[200].strftime("%Y-%m") in gap.detail, (
        f"跳空發生在 {days[200]:%Y-%m}(價格由 100 變 50 的那個月),"
        f"報告卻說:{gap.detail}"
    )


def test_shiller_column_degradation_beyond_the_accepted_date(
        sandbox: Path, cfg: dict) -> None:
    """已接受的劣化起點之前也變成全 0 = 劣化範圍擴大,必須報警。"""
    rows = ["Date,SP500,Dividend,Long Interest Rate"]
    for i in range(120):
        y, m = 2015 + i // 12, i % 12 + 1
        dead = f"{y}-{m:02d}" >= "2021-01"          # 比已接受的 2023-07 早兩年半
        rows.append(f"{y}-{m:02d}-01,{'0' if dead else '3000'},"
                    f"{'0' if dead else '50'},{'0' if dead else '2.5'}")
    (sandbox / fetch_data.SOURCES["shiller"].filename).write_text(
        "\n".join(rows) + "\n", encoding="utf-8", newline="\n")

    r = hc.check_column_degradation(cfg)[0]
    assert r.failed, f"劣化範圍擴大卻沒報警:{r.detail}"
    assert "SP500" in r.detail


# ══════════════════════════════════════════════════════════════════════════
# 回歸:被截斷的來源不得蓋掉本機最後一份好資料(2026-08-13)
#
# 原本 `manifest[name] = save(name, text)` 寫在 if/else 外面,於是判定
# 「只回了 5 列,低於下限 10000」之後,那 5 列照樣落地。
# 實測:601 列 -> 6 列,manifest 記上 rows=5 與全新的 fetched_at。
# 也就是稽核軌跡與心跳同時宣稱「剛剛更新成功」,而檢查正在說它壞了。
# ══════════════════════════════════════════════════════════════════════════


def _serve(monkeypatch, **payloads) -> None:
    monkeypatch.setattr(fetch_data, "fetch_source",
                        lambda name: payloads[name])


def _good_payloads(days) -> dict[str, str]:
    return {
        "sp_daily": "date,close\n" + "".join(
            f"{d.isoformat()},{100.0 + i}\n" for i, d in enumerate(days)),
        "vix": "DATE,OPEN,HIGH,LOW,CLOSE\n" + "".join(
            f"{d.isoformat()},20,22,20,20\n" for d in days),
        "shiller": "Date,SP500,Dividend,Long Interest Rate\n" + "".join(
            f"{2000 + i // 12}-{i % 12 + 1:02d}-01,3000,50,2.5\n"
            for i in range(1900)),
    }


def test_truncated_source_does_not_clobber_local_data(
        sandbox: Path, cfg: dict, monkeypatch) -> None:
    days = _trading_days(date(2020, 1, 1), 600)
    good = write_sp(sandbox, days)
    path = sandbox / fetch_data.SOURCES["sp_daily"].filename

    payloads = _good_payloads(_trading_days(date(1990, 1, 1), 11000))
    payloads["sp_daily"] = "date,close\n2026-08-01,100\n"      # 1 列 << 10000
    _serve(monkeypatch, **payloads)

    results = hc.check_sources_reachable(cfg)

    assert "來源可達性 · sp_daily" in failed_names(results)
    assert path.read_text(encoding="utf-8") == good, (
        "被截斷的回應蓋掉了本機最後一份好資料 —— "
        "健檢的職責是【只檢查、不改資料】"
    )
    assert "sp_daily" not in fetch_data.read_manifest(), (
        "稽核軌跡記下了一份剛被判定為壞掉的資料"
    )


def test_a_good_source_is_still_saved(sandbox: Path, cfg: dict, monkeypatch) -> None:
    """對照組:上面那條不能是靠「什麼都不存」達成的。"""
    _serve(monkeypatch, **_good_payloads(_trading_days(date(1990, 1, 1), 11000)))
    results = hc.check_sources_reachable(cfg)

    assert not failed_names(results), [r.detail for r in results if r.failed]
    assert (sandbox / fetch_data.SOURCES["sp_daily"].filename).exists()
    assert fetch_data.read_manifest()["sp_daily"]["rows"] == 11000


# ══════════════════════════════════════════════════════════════════════════
# 耦合:每個來源都要真的被檢查到
# ══════════════════════════════════════════════════════════════════════════


def test_every_source_has_a_row_floor_and_a_freshness_limit(cfg: dict) -> None:
    """新增來源卻忘了訂閾值 = 那個來源永遠不會被檢查,而且沒有任何症狀。

    這正是 FRED 死管線的形狀:定義在、看起來有在跑、實際上沒人碰。
    """
    declared = set(fetch_data.SOURCES)
    assert declared <= set(cfg["sanity"]["min_rows"]), (
        f"這些來源沒有 min_rows 下限:{declared - set(cfg['sanity']['min_rows'])}"
    )
    assert declared <= set(cfg["freshness"]), (
        f"這些來源沒有新鮮度契約:{declared - set(cfg['freshness'])}"
    )
    # 每個來源都要明說自己的發布頻率。少了這個欄位,判準就會退回「猜一個天數」,
    # 而那正是 shiller 每月假警報的成因。
    for name, spec in cfg["freshness"].items():
        assert spec.get("cadence") in ("daily", "monthly"), (
            f"來源 {name} 沒有宣告 cadence:{spec}"
        )
        need = "max_age_days" if spec["cadence"] == "daily" else "publish_grace_day"
        assert need in spec, f"來源 {name}({spec['cadence']})缺少 {need}:{spec}"


def test_source_list_is_not_hardcoded(sandbox: Path, cfg: dict, monkeypatch) -> None:
    """檢查的來源清單必須來自 `fetch_data.SOURCES`,不能是手打的字串。

    原本寫死成 `["sp_daily", "vix", "shiller"]` —— 加第四個來源時
    健檢會安靜地略過它。這裡塞一個假來源進去,它必須出現在結果裡。
    """
    fake = fetch_data.Source(**{
        **{f.name: getattr(fetch_data.SOURCES["vix"], f.name)
           for f in fetch_data.Source.__dataclass_fields__.values()},
        "filename": "ghost.csv",
    })
    monkeypatch.setitem(fetch_data.SOURCES, "ghost", fake)
    _serve(monkeypatch, **{**_good_payloads(_trading_days(date(1990, 1, 1), 11000)),
                           "ghost": "a,b\n1,2\n"})

    got = {r.name: r for r in hc.check_sources_reachable(cfg)}
    assert "來源可達性 · ghost" in got, (
        f"新來源沒有被檢查到 —— 清單可能又被寫死了。實際檢查了:{list(got)}"
    )
    # 光看名字不夠:沒訂下限時若「靜靜略過」,它一樣會因為別的例外產生同名的
    # FAIL,兩者從名稱上分不出來。判準必須是它有沒有**說出真正的原因**。
    assert "min_rows" in got["來源可達性 · ghost"].detail, (
        f"新來源沒有下限卻沒有明講,只回了:{got['來源可達性 · ghost'].detail}"
    )


# ══════════════════════════════════════════════════════════════════════════
# 報告輸出 —— Issue 內文與軌跡
# ══════════════════════════════════════════════════════════════════════════


def test_markdown_lists_every_failure(sandbox: Path) -> None:
    results = [
        hc.Result("來源可達性 · vix", hc.FAIL, "抓取失敗:timeout"),
        hc.Result("PWA 部署", hc.OK, "HTTP 200"),
        hc.Result("自架 Worker", hc.SKIP, "未設定"),
    ]
    md = hc.render_markdown(results)
    assert "偵測到 **1** 項異常" in md
    assert "來源可達性 · vix" in md and "timeout" in md
    assert "PWA 部署" in md            # 完整清單也要在 details 裡
    assert "health_check.py" in md     # 診斷指引不得被拿掉


def test_log_row_is_parseable_as_csv(sandbox: Path) -> None:
    """檢查項名稱裡若出現逗號,這個手寫的 CSV 會直接錯位。"""
    import csv

    hc.append_log([hc.Result("來源可達性 · vix", hc.FAIL, "x"),
                   hc.Result("PWA 部署", hc.OK, "y")])
    rows = list(csv.DictReader(hc.HEALTH_LOG.open(encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["ok"] == "0"
    assert rows[0]["n_checks"] == "2"
    assert rows[0]["failed"] == "來源可達性 · vix"


def test_no_check_name_contains_a_comma() -> None:
    """append_log 沒有做 CSV 逸出 —— 名稱裡有逗號就會讓整份軌跡錯位。"""
    import re

    src = (OPS_ROOT / "src" / "health_check.py").read_text(encoding="utf-8")
    names = re.findall(r'Result\(\s*f?"([^"]+)"', src)
    assert names, "抓不到任何檢查項名稱,這條測試沒有在看東西"
    assert not [n for n in names if "," in n or "，" in n]


# ══════════════════════════════════════════════════════════════════════════
# 一個故障只該報一次 —— 噪音會訓練使用者無視通知(2026-10-14)
#
# 2026-08-13 修掉「壞資料會蓋掉好資料」之後,壞掉的來源就【不落地】了。
# 修法是對的,但二階效應沒被預料到:在乾淨 runner 上那個檔案於是根本不存在,
# 後面兩項檢查就改報「檔案不存在」。實測一個被截斷的 sp_daily 會產生
# 4 條訊息,其中 2 條在說錯的事,而真正的原因只在第一行 ——
# 還順便把絕對路徑貼進公開 repo 的 Issue。
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def truncated_sp(sandbox: Path, monkeypatch):
    """sp_daily 回一份被截斷的回應,vix / shiller 正常。

    ⚠ vix 必須真的超過 `min_rows.vix`(8000),否則它也會被判成不可達,
    「沒壞的來源不該連坐」那條對照組就失去判別力。第一版就是這樣寫壞的。
    """
    # 8200 個交易日 ≈ 32 年,夠過 min_rows 的 8000 下限
    vix_days = _trading_days(hc.today_utc() - timedelta(days=365 * 33), 8200)
    real_vix = "DATE,OPEN,HIGH,LOW,CLOSE\n" + "".join(
        f"{d.isoformat()},20,22,20,20\n" for d in vix_days)
    shiller = "Date,SP500,Dividend,Long Interest Rate\n" + "".join(
        f"{1871 + i // 12}-{i % 12 + 1:02d}-01,100,2,3\n" for i in range(1850))

    def fake_fetch(name: str) -> str:
        if name == "sp_daily":
            return "date,close\n2026-10-01,5000\n2026-10-02,5010\n"
        if name == "vix":
            return real_vix
        return shiller

    monkeypatch.setattr(fetch_data, "fetch_source", fake_fetch)
    return sandbox


def test_a_broken_source_is_reported_once_not_three_times(
        truncated_sp: Path, cfg: dict) -> None:
    reachable = hc.check_sources_reachable(cfg)
    down = hc.unreachable_sources(reachable)
    assert down == {"sp_daily"}, f"沒有正確辨識出壞掉的來源:{down}"

    results = reachable + hc.check_freshness(cfg, down) + hc.check_sanity(cfg, down)
    sp_failures = [r for r in results if r.failed and "sp" in r.name.lower()]
    assert len(sp_failures) == 1, (
        "同一個 sp_daily 故障被報成多條 FAIL:\n"
        + "\n".join(f"  {r.name} — {r.detail}" for r in sp_failures)
        + "\n一個故障報多次就是噪音,而噪音會讓使用者學會無視整個 Issue"
    )
    assert "只回了 2 列" in sp_failures[0].detail, (
        f"唯一那條 FAIL 沒有說出真正的原因:{sp_failures[0].detail!r}"
    )


def test_downstream_checks_say_skipped_not_broken(truncated_sp: Path, cfg: dict) -> None:
    """「沒得判斷」和「也壞了」在報告上必須長得不一樣。

    這是這個專案的核心原則之一(體制層的 ok=False vs triggered=False 同理):
    混在一起的話,一個上游故障會看起來像三個系統同時崩潰。
    """
    down = hc.unreachable_sources(hc.check_sources_reachable(cfg))
    by_name = {r.name: r for r in hc.check_freshness(cfg, down) + hc.check_sanity(cfg, down)}
    assert by_name["資料新鮮度 · sp_daily"].status == hc.SKIP
    assert by_name["數值合理性 · S&P"].status == hc.SKIP
    # 對照組:沒壞的來源必須照常被檢查,不能連坐
    assert by_name["資料新鮮度 · vix"].status in (hc.OK, hc.FAIL)
    assert by_name["數值合理性 · VIX 範圍"].status in (hc.OK, hc.FAIL)


def test_missing_file_error_does_not_leak_an_absolute_path(sandbox: Path) -> None:
    """`load()` 的錯誤訊息會經由健檢變成【公開 repo】的 Issue 內文。

    ⚠ 測在這裡而不是測報告:第一版去掃 render_markdown() 的輸出,但在那個情境裡
    壞掉的來源已經被 SKIP 了 —— `load()` 從來沒被呼叫,所以絕對路徑當然不會出現,
    那條測試是**恆真**的(把修法整個改回去也照樣綠)。
    判準要測在事實產生的地方,不是測在它剛好不會出現的地方。

    絕對路徑在這裡也沒有診斷價值:它永遠是 ops/data/raw/。
    """
    with pytest.raises(FileNotFoundError) as excinfo:
        fetch_data.load("vix", refresh_if_missing=False)
    msg = str(excinfo.value)
    assert ":\\" not in msg and not msg.startswith("/"), (
        f"錯誤訊息帶了絕對路徑,會被貼進公開 Issue:{msg!r}"
    )
    assert "data/raw/vix_daily.csv" in msg, (
        f"訊息沒說是哪個檔 —— 去掉絕對路徑不代表要去掉診斷資訊:{msg!r}"
    )


def test_no_absolute_paths_reach_the_public_issue(truncated_sp: Path, cfg: dict) -> None:
    """同上,但測到報告層。這一條是防護網,不是主判準(見上面那條的說明)。"""
    down = hc.unreachable_sources(hc.check_sources_reachable(cfg))
    results = hc.check_freshness(cfg, down) + hc.check_sanity(cfg, down)
    body = hc.render_markdown(results)
    assert "資料新鮮度" in body, "前置條件:報告裡要真的有內容,否則這條是恆真的"
    offenders = [tok for tok in body.split() if ":\\" in tok or tok.startswith("/home/")]
    assert not offenders, f"報告裡出現絕對路徑:{offenders[:3]}"


def test_check_order_is_load_bearing(sandbox: Path, cfg: dict, monkeypatch) -> None:
    """`run_all()` 必須先跑可達性,否則乾淨 runner 上後面全部變成假紅燈。

    `data/raw/*.csv` 不進版控、也不從 ops-data 取回,所以在 runner 上那個目錄
    一開始是空的。這條依賴在本機完全看不出來(本機 raw 檔一直在)——
    有人重排 run_all() 的話,本機測試照樣全綠,線上會爆一整排「檔案不存在」。
    """
    days = _trading_days(hc.today_utc() - timedelta(days=400), 260)

    def fake_fetch(name: str) -> str:
        if name == "sp_daily":
            return "date,close\n" + "".join(
                f"{d.isoformat()},{100.0 + i}\n" for i, d in enumerate(days)) * 40
        if name == "vix":
            return "DATE,OPEN,HIGH,LOW,CLOSE\n" + "".join(
                f"{d.isoformat()},20,22,20,20\n" for d in days) * 32
        return "Date,SP500,Dividend,Long Interest Rate\n" + "".join(
            f"{1900 + i // 12}-{i % 12 + 1:02d}-01,100,2,3\n" for i in range(1900))

    monkeypatch.setattr(fetch_data, "fetch_source", fake_fetch)
    # 代理 / PWA / Worker 那幾項會打真的網路,這裡只關心前三組
    # `**kw`:check_proxies 會收到 mirror_ok —— 這個 stub 不在意它是什麼,
    # 但簽章變了就該在這裡看得出來,所以不用 *args 吞掉位置參數
    monkeypatch.setattr(hc, "check_proxies", lambda cfg, **kw: [])
    monkeypatch.setattr(hc, "check_pwa_deploy", lambda cfg: [])
    monkeypatch.setattr(hc, "check_worker", lambda cfg: [])
    monkeypatch.setattr(hc, "check_published_mirror", lambda cfg: [])

    assert not list(sandbox.iterdir()), "前置條件:raw 目錄必須是空的(模擬乾淨 runner)"
    results = hc.run_all()

    ghosts = [r for r in results if r.failed and "不存在" in r.detail]
    assert not ghosts, (
        "乾淨環境跑 run_all() 冒出了「檔案不存在」的失敗:\n"
        + "\n".join(f"  {r.name} — {r.detail}" for r in ghosts)
        + "\n→ 可達性檢查必須排在新鮮度/合理性【之前】,它負責把檔案落地"
    )


# ══════════════════════════════════════════════════════════════════════════
# Issue 同步 —— 標題與內文必須反映【目前】的狀態(2026-10-14)
#
# 原本的去重 key 是「有沒有開放的 health-fail Issue」,不是「哪幾項在失敗」。
# 實測:Issue #2 連紅 54 天、52 則留言,期間失敗項目換過 6 次,包含兩個
# 真正影響使用者看到的數字的新故障 —— 全部以「第 N 則留言」的形式埋在
# 一個標題還寫著早就恢復了的原因的討論串裡。
#
# 這組測試釘的是:變動要說、沒變動要安靜、而且舊格式的 Issue 要能平順接上。
# ══════════════════════════════════════════════════════════════════════════


def _r(*names) -> list:
    return [hc.Result(n, hc.FAIL, f"detail of {n}") for n in names]


def test_title_names_what_is_currently_failing() -> None:
    title = hc.issue_title(_r("PWA 代理鏈", "資料新鮮度 · vix"))
    assert "PWA 代理鏈" in title and "vix" in title, (
        f"標題沒說出目前在失敗的是什麼:{title!r} —— "
        "原本只寫第一次失敗的日期,54 天後標題講的還是那個早就恢復的項目"
    )
    assert "2 項" in title


def test_title_does_not_grow_without_bound() -> None:
    """失敗項目很多時標題仍要能看。GitHub 的列表會把過長標題截掉。"""
    title = hc.issue_title(_r(*[f"很長的檢查項目名稱編號 {i}" for i in range(12)]))
    assert len(title) <= 80, f"標題長度 {len(title)}:{title!r}"
    assert "12 項" in title, "截斷之後至少要保留『幾項』這個數字"


def test_marker_round_trips_through_the_issue_body() -> None:
    """標記必須能從自己產生的報告裡讀回來 —— 這是整個變動判斷的基礎。"""
    results = _r("PWA 代理鏈", "資料新鮮度 · vix")
    assert hc.parse_failset(hc.render_markdown(results)) == hc.failset(results)


def test_unchanged_failure_set_produces_no_comment() -> None:
    """沒變動就不留言 —— 這一條就是 52 則留言不會再發生的原因。

    ⚠ 要餵回 **issue_plan 回傳的 body**,不是原始報告。那份 body 才帶著
    狀態標記(第一次失敗日、已通知等級、上次留言日),而 workflow 實際寫進
    Issue 的就是它。餵原始報告的話會因為「沒有上次留言紀錄」而觸發週期提醒 ——
    那是對的行為,只是測錯了東西。
    """
    report = hc.render_markdown(_r("PWA 代理鏈"))
    first = hc.issue_plan(report, None)
    assert first["changed"] is True, "第一次一定要出聲"
    # 第二天:同樣的失敗集合,內文沿用上一次寫進去的那一份
    again = hc.issue_plan(report, first["body"])
    assert again["changed"] is False, (
        f"狀態完全沒變卻又留言了 —— 52 則留言就是這樣來的:{again['comment'][:120]}"
    )


def test_a_new_failure_is_announced_with_what_changed() -> None:
    old = hc.render_markdown(_r("資料新鮮度 · shiller"))
    new = hc.render_markdown(_r("PWA 代理鏈", "資料新鮮度 · vix"))
    plan = hc.issue_plan(new, old)
    assert plan["changed"] is True
    assert "PWA 代理鏈" in plan["comment"], "沒說新增了什麼"
    assert "資料新鮮度 · shiller" in plan["comment"], "沒說哪一項已經恢復"
    assert "新增失敗" in plan["comment"] and "已恢復" in plan["comment"]


def test_recovery_of_one_item_is_still_a_change() -> None:
    """兩項紅變一項紅也是變動 —— 否則「部分恢復」永遠不會被說出來。"""
    old = hc.render_markdown(_r("PWA 代理鏈", "資料新鮮度 · vix"))
    new = hc.render_markdown(_r("PWA 代理鏈"))
    plan = hc.issue_plan(new, old)
    assert plan["changed"] is True
    assert "資料新鮮度 · vix" in plan["comment"]


def test_legacy_issue_without_a_marker_is_handled() -> None:
    """既有的 Issue #2 沒有標記。那是「不知道」,不是「上次沒有失敗」。

    兩者必須分得開:當成「上次沒有失敗」會把目前每一項都說成新增的,
    內容雖然不致命但會誤導。
    """
    assert hc.parse_failset("健檢異常 · 2026-08-21\n\n舊格式內文") is None
    plan = hc.issue_plan(hc.render_markdown(_r("PWA 代理鏈")), "舊格式內文")
    assert plan["changed"] is True, "舊格式的 Issue 要留一次言把現況說清楚"
    assert "先前沒有記錄" in plan["comment"]
    assert "新增失敗" not in plan["comment"], (
        "舊格式被當成『上次沒有失敗』了 —— 「不知道」和「空集合」必須分得開"
    )


def test_issue_plan_reads_names_from_the_marker_not_the_prose() -> None:
    """名稱要從標記讀。解析散文的話,文案改一次就無聲壞掉 ——

    而它壞掉的症狀是「永遠判定沒有變動」,也就是這個修法完全失效且沒有症狀。
    """
    report = hc.render_markdown(_r("PWA 代理鏈"))
    # 把散文全部拿掉,只留標記 —— 名稱仍然必須讀得出來
    marker_only = [ln for ln in report.splitlines() if ln.startswith("<!-- failset:")]
    assert marker_only, "報告裡沒有標記"
    assert hc.issue_plan("\n".join(marker_only), None)["failed"] == ["PWA 代理鏈"]


def test_the_workflow_actually_calls_issue_plan() -> None:
    """邏輯有測試,但 workflow 要真的呼叫它才有用 —— 心跳就踩過這種路徑錯誤。

    (`OPS_SIGNALS` 寫成 `data/manifest.json`、實際是 `ops/data/manifest.json`,
    而在「失敗即靜默」的設計下完全沒有症狀。)
    """
    wf = (OPS_ROOT.parent / ".github" / "workflows" / "daily-health.yml").read_text(
        encoding="utf-8")
    assert "--issue-plan" in wf, "workflow 沒有呼叫 --issue-plan,這組邏輯是死碼"
    assert "gh issue edit" in wf, (
        "workflow 沒有更新 Issue 內文/標題 —— 內文會永遠停在第一天那一份"
    )


# ══════════════════════════════════════════════════════════════════════════
# 抓取失敗 ≠ 沒得判斷(2026-10-19,第二意見指出)
#
# 第一版對「上游失敗」的來源一律 SKIP。那太粗了:只要本機還有上一份好資料,
# 它有多舊仍然判斷得出來 —— 而來源連續死三週時,那個訊號正是重點
# (「你手上最新的那份已經 25 天了」)。一律 SKIP 會把它藏起來。
#
# 判準因此改成【本機有沒有檔案】,不是【上游有沒有失敗】。
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def source_dead_but_local_data_exists(sandbox: Path, monkeypatch, cfg: dict):
    """本機有一份【30 天前】的 vix,而來源現在抓不到了。

    ⚠ 日期必須從「今天往回數」構造。第一版寫成
    `_trading_days(today - 33年, 8200)` 再砍掉最後 30 天 —— 但 8200 個交易日
    就是 31.4 年,序列本來就結束在 2025 年,那個砍掉完全沒有作用,
    實際測到的是「566 天前」。數字對不上就代表 fixture 在說謊。
    """
    last = hc.today_utc() - timedelta(days=30)
    # 8200 個交易日往回推約 11480 個日曆天,讓序列結束在 last
    start = last - timedelta(days=11600)
    days = [d for d in _trading_days(start, 8600) if d <= last]
    assert len(days) >= cfg["sanity"]["min_rows"]["vix"], f"fixture 列數不足:{len(days)}"
    write_vix(sandbox, days)

    def dead_fetch(name: str) -> str:
        raise RuntimeError("來源連線逾時")

    monkeypatch.setattr(fetch_data, "fetch_source", dead_fetch)
    return days[-1]


def test_stale_local_data_is_still_judged_when_the_source_is_down(
        source_dead_but_local_data_exists: date, cfg: dict) -> None:
    """來源掛了,但本機那份已經 30 天 —— 這一項必須照常報 FAIL,不是 SKIP。

    「你手上最新的資料已經 30 天了」是來源長期死掉時唯一有用的訊號。
    一律 SKIP 會把它藏起來,而那正好是最需要它的時候。
    """
    down = hc.unreachable_sources(hc.check_sources_reachable(cfg))
    assert "vix" in down, "前置條件:來源必須被判定為不可達"

    got = {r.name: r for r in hc.check_freshness(cfg, down)}
    vix = got["資料新鮮度 · vix"]
    assert vix.status == hc.FAIL, (
        f"本機資料已經 30 天卻被跳過了 —— 來源長期死掉時這就是唯一的訊號:{vix}"
    )
    assert str(source_dead_but_local_data_exists) in vix.detail, (
        f"沒報出本機那份的實際日期:{vix.detail!r}"
    )
    # 天數由 fixture 實際寫出的日期算,不寫死 —— today-30 可能落在週末,
    # 最後一個交易日因此是 31 天前。寫死數字只會讓測試在某些日子紅。
    expected_age = (hc.today_utc() - source_dead_but_local_data_exists).days
    assert f"已 {expected_age} 天" in vix.detail, f"天數算錯了:{vix.detail!r}"
    assert "最後一份好資料" in vix.detail, (
        f"沒講明評估的是舊資料,使用者會以為這是剛抓到的:{vix.detail!r}"
    )


def test_sanity_also_runs_on_the_last_good_copy(
        source_dead_but_local_data_exists: date, cfg: dict) -> None:
    """合理性同理:有檔案就檢查得出來,不該因為上游失敗就整組跳過。"""
    down = hc.unreachable_sources(hc.check_sources_reachable(cfg))
    names = {r.name: r.status for r in hc.check_sanity(cfg, down)}
    assert names.get("數值合理性 · VIX 範圍") in (hc.OK, hc.FAIL), (
        f"有本機資料卻沒有檢查:{names}"
    )
    assert "數值合理性 · VIX" not in names, "不該出現整組 SKIP 的那一列"


def test_no_local_data_really_is_skipped(truncated_sp: Path, cfg: dict) -> None:
    """對照組:本機真的什麼都沒有時,SKIP 仍然是對的。

    沒有這一條,上面兩條可能只是因為「永遠不 SKIP」而通過,
    而那會讓 2026-10-14 修掉的「一個故障報四次」整個回來。
    """
    down = hc.unreachable_sources(hc.check_sources_reachable(cfg))
    got = {r.name: r for r in hc.check_freshness(cfg, down) + hc.check_sanity(cfg, down)}
    assert got["資料新鮮度 · sp_daily"].status == hc.SKIP
    assert "沒有既有資料" in got["資料新鮮度 · sp_daily"].detail
    assert got["數值合理性 · S&P"].status == hc.SKIP


# ══════════════════════════════════════════════════════════════════════════
# 月頻來源的新鮮度契約(2026-10-19)
#
# 舊判準 `shiller: 50`(天)每個月必然破線一次再自動復原 ——
# 因為觀測月 M 那一列在 M+1 月初才發布,它在被取代前年齡必然走到約 60 天。
# 實測軌跡(ops-data 的 health_log.csv,82 天):
#     08-21 FAIL → 09-01 ok → 09-22 FAIL → 10-02 ok
#
# 新判準問的是「該發布的那一期到了沒」。這【不是】放寬閾值,但那個區分
# 只有在能證明下面兩件事時才站得住腳,所以這兩條測試是這個改動的全部正當性:
#
#   1. 正常發布節奏下,一整年逐日模擬【一次都不會】假警報
#   2. 來源真的停更時,仍然抓得到 —— 而且是在「第一次漏期」那一刻
#
# publish_grace_day 的依據是實測抵達日(2026-08-31~09-01、2026-10-01~02),
# 不是挑一個比較好過的數字。測試 1 同時驗證了「只要抵達日 ≤ 寬限日就不會假警報」
# 這個不變量,所以日後若抵達日變晚,這條測試會先紅。
# ══════════════════════════════════════════════════════════════════════════

GRACE = 5   # 與 config.yml 的 shiller.publish_grace_day 一致,由下面的測試綁住


def test_grace_day_matches_the_config(cfg: dict) -> None:
    assert cfg["freshness"]["shiller"]["publish_grace_day"] == GRACE, (
        "config 的寬限日改了,但這個檔案的模擬還在用舊值 —— 兩邊會說不同的話"
    )


def _latest_available(today: date, arrival_day: int) -> str:
    """模擬:在 `today` 這一天,來源實際已經發布到哪個觀測月。

    觀測月 M 的那一列在 M+1 月的 `arrival_day` 號抵達。
    """
    back = 1 if today.day >= arrival_day else 2
    return hc._shift_month(today.year, today.month, back)


@pytest.mark.parametrize("arrival_day", [1, 2, 3, 4, 5])
def test_monthly_contract_has_no_false_alarms(arrival_day: int) -> None:
    """正常發布節奏 → 一整年每一天都不得報警。

    涵蓋跨年(12 月 → 1 月要回推到去年 12 月),那是月份算術最容易錯的地方。
    """
    day = date(2026, 1, 1)
    bad = []
    while day < date(2027, 1, 1):
        have = _latest_available(day, arrival_day)
        want = hc.expected_month(day, GRACE)
        if have < want:
            bad.append((day.isoformat(), f"有 {have} / 要 {want}"))
        day += timedelta(days=1)
    assert not bad, (
        f"抵達日 {arrival_day} 號、發布完全正常,卻有 {len(bad)} 天會報警:"
        f"{bad[:5]} —— 這就是舊的 50 天門檻每個月紅一次的那個問題"
    )


def test_a_late_arrival_past_the_grace_day_is_caught() -> None:
    """對照組:抵達日比寬限日晚,就【應該】被抓到。

    沒有這一條,上面那條可能只是因為「永遠不報警」而全綠。
    """
    late = GRACE + 3
    fired = [d for d in range(1, 29)
             if _latest_available(date(2026, 6, d), late) < hc.expected_month(date(2026, 6, d), GRACE)]
    assert fired, f"抵達日晚到 {late} 號仍然完全不報警 —— 這個判準沒有判別力"


def test_a_stopped_monthly_source_is_caught() -> None:
    """來源真的停更 → 必須在「第一次漏期」那一刻抓到,不能更晚。

    情境:2026-09 那一期正常發布(10-02 抵達),之後就再也沒有新的了。
    2026-10 那一期本來該在 11 月初到。
    """
    frozen = "2026-09"
    first_fail = None
    day = date(2026, 10, 2)
    while day < date(2027, 3, 1):
        if frozen < hc.expected_month(day, GRACE):
            first_fail = day
            break
        day += timedelta(days=1)
    assert first_fail == date(2026, 11, GRACE), (
        f"停更後第一次報警是 {first_fail},預期 2026-11-{GRACE:02d} —— "
        "太早代表在沒有證據時就報警,太晚代表偵測被放寬了"
    )


def test_the_old_day_threshold_really_did_fire_on_healthy_data() -> None:
    """把舊判準跑一次,證明「改單位」不是藉口 —— 它真的每個月都會紅。

    這是這個改動唯一的正當性來源:舊規則在資料完全正常時也會報警。
    若這條測試哪天不再成立,就代表改判準的理由消失了。
    """
    OLD_LIMIT = 50
    false_alarms = 0
    day = date(2026, 1, 1)
    while day < date(2027, 1, 1):
        have = _latest_available(day, arrival_day := 2)
        # 該觀測月那一列的日期標記是當月 1 號
        y, m = int(have[:4]), int(have[5:])
        if (day - date(y, m, 1)).days > OLD_LIMIT:
            false_alarms += 1
        day += timedelta(days=1)
    assert false_alarms > 50, (
        f"舊的 {OLD_LIMIT} 天門檻在完全正常的發布節奏下只誤報 {false_alarms} 天 —— "
        "若真的這麼少,改判準的理由就不成立,應該把這個改動收回去"
    )


# ══════════════════════════════════════════════════════════════════════════
# 持續惡化 ≠ 持續失敗(2026-10-19,第二意見指出)
#
# 「只在失敗集合變動時留言」修掉了 52 則留言,但留下一個洞:
#   第 1 天  {vix}  —— VIX 剛開始落後
#   第 22 天 {vix}  —— 已經停更 22 天
#   第 60 天 {vix}  —— 這個來源大概不會自己回來了
# 三個狀態的【集合一模一樣】,嚴重程度完全不同,而原本一則都不會說。
#
# 補的是兩種新事件:嚴重程度跨級、以及太久沒出聲的週期提醒。
# 兩者都刻意稀疏 —— 一個永久故障在 90 天內最多 3 次跨級 + 之後每 30 天一次,
# 和原本的 52 則不是同一個量級。
# ══════════════════════════════════════════════════════════════════════════


def _plan_on(day: str, names, previous_body=None, detail=None):
    """把時鐘固定在 `day` 跑一次通知決策,回 (Notice, 這次要寫進 Issue 的完整內文)。

    ⚠ 回傳的內文必須是 **report + 狀態標記**,也就是 workflow 真的會
    `gh issue edit --body-file` 進去的那一份。只餵狀態標記的話,
    `parse_failset()` 讀不到失敗集合,每一次都會被當成「第一次」而出聲 ——
    那正是這組測試要防的事,結果測試自己先踩進去。
    """
    report = hc.render_markdown(_r(*names))
    notice = hc.plan_notice(previous_body, sorted(names), detail or {}, day)
    return notice, hc.with_state_markers(report, notice.state_markers)


def test_the_same_failure_set_still_escalates_when_it_drags_on() -> None:
    """集合沒變,但持續時間跨級 → 必須說一次。"""
    n1, b1 = _plan_on("2026-09-01", ["資料新鮮度 · vix"])
    assert n1.comment, "第一次一定要出聲"

    # 第 3 天:還在同一級,不該出聲
    n2, b2 = _plan_on("2026-09-04", ["資料新鮮度 · vix"], b1)
    assert n2.comment is None, f"才 3 天就又留言了:{n2.comment[:100]}"

    # 第 8 天:跨過 7 天那一級
    n3, b3 = _plan_on("2026-09-09", ["資料新鮮度 · vix"], b2)
    assert n3.comment, "持續一週跨級了卻沒有任何通知"
    assert "跨級" in n3.comment and "持續一週以上" in n3.comment, n3.comment[:200]


def test_escalation_fires_once_per_level_not_every_day() -> None:
    """跨級是【事件】不是狀態 —— 跨過之後的每一天都不該再說。"""
    body = None
    spoke = []
    for d in range(1, 26):
        day = f"2026-09-{d:02d}"
        n, body = _plan_on(day, ["資料新鮮度 · vix"], body)
        if n.comment:
            spoke.append(day)
    # 9/01 第一次 + 9/08 跨到「一週以上」。25 天內不該有第三次。
    assert spoke == ["2026-09-01", "2026-09-08"], (
        f"25 天內出聲了 {len(spoke)} 次:{spoke} —— 跨級應該只在跨過的那天說一次"
    )


def test_a_long_silent_failure_gets_a_periodic_reminder() -> None:
    """久到你已經忘記它還開著 → 週期提醒。但不是每天。"""
    body = None
    spoke = []
    day = date(2026, 9, 1)
    while day < date(2027, 3, 1):
        n, body = _plan_on(day.isoformat(), ["資料新鮮度 · vix"], body)
        if n.comment:
            spoke.append(day.isoformat())
        day += timedelta(days=1)

    assert len(spoke) <= 10, (
        f"181 天內出聲 {len(spoke)} 次 —— 太吵了,會變回沒人看的討論串:{spoke}"
    )
    assert len(spoke) >= 5, (
        f"181 天內只出聲 {len(spoke)} 次 —— 一個半年沒好的故障應該被提醒過幾次:{spoke}"
    )
    # 任兩次之間不得超過提醒週期太多
    gaps = [(date.fromisoformat(b) - date.fromisoformat(a)).days
            for a, b in zip(spoke, spoke[1:])]
    assert max(gaps) <= hc.REMINDER_DAYS + 1, f"中間有 {max(gaps)} 天完全沒出聲:{spoke}"


def test_recovery_still_wins_over_the_quiet_rules() -> None:
    """恢復永遠要說 —— 不能因為「最近才剛提醒過」就吞掉。"""
    n1, b1 = _plan_on("2026-09-01", ["A", "B"])
    n2, b2 = _plan_on("2026-09-02", ["A"], b1)
    assert n2.comment, "有一項恢復了卻完全沒說"
    assert "已恢復" in n2.comment and "B" in n2.comment, n2.comment[:200]


def test_a_recovered_item_that_breaks_again_restarts_its_clock() -> None:
    """修好又壞掉 = 新的故障,不該把中間修好的那段算進持續時間。"""
    n1, b1 = _plan_on("2026-09-01", ["A"])
    n2, b2 = _plan_on("2026-09-20", [], b1)   # A 恢復
    n3, b3 = _plan_on("2026-09-21", ["A"], b2)  # 又壞
    since = hc._parse_kv(b3, hc._SINCE_PREFIX)
    assert since["A"].startswith("2026-09-21"), (
        f"重新壞掉卻沿用了舊的起算日 {since['A']} —— 持續時間會被灌水,"
        "一個反覆壞掉的來源會被誤判成長期故障"
    )


def test_severity_buckets_are_monotonic() -> None:
    """天數越久等級不得下降。分級本身壞掉的話,跨級偵測就全錯。"""
    levels = [hc.severity(d) for d in range(0, 200)]
    assert levels == sorted(levels), "嚴重程度隨天數下降了"
    assert levels[0] == 0 and levels[-1] == len(hc.SEVERITY_DAYS)
    assert len(hc.SEVERITY_LABEL) == len(hc.SEVERITY_DAYS) + 1, (
        "等級數與標籤數對不上 —— 會 IndexError,而那會讓整個通知流程掛掉"
    )


def test_check_names_contain_no_marker_delimiters() -> None:
    """狀態標記用 `|` 分項、`=` 分鍵值。檢查項名稱不得含這兩個字元。

    含了的話標記會解析錯 —— 而症狀是「持續時間算錯」或「永遠判定沒變動」,
    兩個都完全無聲。
    """
    cfg = hc.load_config()
    names = [r.name for r in hc.check_freshness(cfg)] + \
            [f"來源可達性 · {n}" for n in fetch_data.SOURCES] + \
            ["PWA 代理鏈", "PWA 部署", "自架 Worker", "欄位劣化 · Shiller"]
    bad = [n for n in names if "|" in n or "=" in n]
    assert not bad, f"這些檢查項名稱會破壞狀態標記:{bad}"


def test_the_workflow_writes_the_body_with_state_markers() -> None:
    """workflow 必須把 plan 的 `body` 寫進 Issue,不是原始報告。

    少了狀態標記,下一次執行就不知道「這一項壞多久了」—— 嚴重程度跨級與
    週期提醒會整個失效,**而且完全無聲**(它會退化成「只在集合變動時留言」,
    看起來一切正常)。心跳就踩過這種「路徑錯了但沒有症狀」的坑。
    """
    wf = (OPS_ROOT.parent / ".github" / "workflows" / "daily-health.yml").read_text(
        encoding="utf-8")
    assert "jq -r .body > issue_body.md" in wf, "workflow 沒有取出帶狀態標記的 body"
    assert "--body-file issue_body.md" in wf, "寫進 Issue 的不是那份 body"

    # 2026-10-19:原本是取「第一個 gh issue create 之後的 200 個字元」。
    # 依賴安裝失敗那條路徑加進來之後,第一個 create 變成那一條,於是這條測試
    # 開始對著錯的區段在比。改成兩條更嚴的斷言,**涵蓋範圍比原本大**:
    #
    #   1. 每一個 gh issue create / edit 都必須用 --body-file,不准用 --body。
    #      行內 --body 代表內文是當場拼出來的字串,那份字串不會帶狀態標記。
    #   2. 健檢那條路徑用的必須就是 issue_body.md(帶 failsince/lastnotice)。
    #
    # 依賴安裝那條路徑用的是 dep_issue_body.md —— 它也帶 failset 標記
    # (由 dep_manifest.issue_body() 產生,有自己的測試),所以「恢復正常
    # 自動關閉」對兩條路徑都成立。
    import re as _re

    # ⚠ 先把 YAML 的註解行拿掉再掃。第一版沒有,於是它比對到的是我自己
    # 寫在 workflow 裡**解釋 `--issue-plan` 的那段註解**,不是真的那一行指令。
    # 這個專案已經踩過同一件事(掃 index.html 時比對到自己的 `//` 註解),
    # 症狀同樣是「測試在看一個不是事實產生處的地方」。
    code = "\n".join(ln for ln in wf.splitlines()
                     if not ln.strip().startswith("#"))

    for verb in ("create", "edit"):
        hits = _re.findall(rf"gh issue {verb}\b((?:.|\n)*?)(?=\n\s*(?:[a-z]|$))", code)
        assert hits, f"workflow 裡找不到任何 gh issue {verb} —— 這條斷言會變成恆真"
        for seg in hits:
            assert "--body-file" in seg, (
                f"有一處 gh issue {verb} 沒有用 --body-file:{seg[:120]!r} —— "
                f"行內 --body 的內文不會帶狀態標記"
            )
    assert "--body-file issue_body.md" in code.split("--issue-plan")[1], (
        "健檢那條路徑建立/更新 Issue 時用的不是帶標記的 issue_body.md"
    )


def test_issue_plan_body_round_trips_every_marker() -> None:
    """plan 產生的 body,下一次必須讀得回全部三種狀態。

    這是整條鏈最容易無聲壞掉的一環:任何一個標記寫出去卻讀不回來,
    症狀都是「某個提醒永遠不會發生」。
    """
    report = hc.render_markdown(_r("PWA 代理鏈"))
    plan = hc.issue_plan(report, None)
    body = plan["body"]
    assert hc.parse_failset(body) == ["PWA 代理鏈"], "failset 讀不回來"
    since = hc._parse_kv(body, hc._SINCE_PREFIX)
    assert "PWA 代理鏈" in since and "," in since["PWA 代理鏈"], f"failsince 讀不回來:{since}"
    assert hc._parse_kv(body, hc._NOTICE_PREFIX).get("at"), "lastnotice 讀不回來"


def test_an_empty_log_file_still_gets_a_header(sandbox: Path) -> None:
    """既存但【空】的 health_log.csv,仍然要寫表頭。

    ## 這不是假想的情況

    workflow 第 5 步用這一行取回既有軌跡:

        git show origin/ops-data:ops/data/health_log.csv > ops/data/health_log.csv

    shell 的重導向**在 git show 執行之前就把目的檔截斷成 0 bytes**,
    所以那一步只要失敗(分支還不存在、fetch 沒建出 remote-tracking ref、
    網路抽風),留下的就是一個存在但空的檔案。

    判準若是 `not exists()`,這裡會認定「表頭已經有了」→ 直接追加一列資料。
    於是檔案變成沒有表頭的 CSV,而下一次 `pd.read_csv` 會把那一列當欄位名:

        欄位 = ['2026-10-19', '1', '1.1', 'Unnamed: 3']   列數 = 0

    稽核軌跡靜靜變成一張空表,年度健檢的「資料源穩定度」會說沒有紀錄,
    而沒有任何東西會紅。
    """
    import pandas as pd

    hc.HEALTH_LOG.parent.mkdir(parents=True, exist_ok=True)
    hc.HEALTH_LOG.write_text("", encoding="utf-8")       # 重導向留下的 0 bytes
    hc.append_log([hc.Result("來源可達性 · vix", hc.OK, "ok")])

    text = hc.HEALTH_LOG.read_text(encoding="utf-8")
    assert text.startswith("date,ok,n_checks,failed"), (
        "空檔案被當成「已經有表頭」:%r" % text)
    df = pd.read_csv(hc.HEALTH_LOG)
    assert list(df.columns) == ["date", "ok", "n_checks", "failed"]
    assert len(df) == 1


def test_an_existing_log_does_not_get_a_second_header(sandbox: Path) -> None:
    """控制組:已經有內容的軌跡檔不可以再被插一行表頭。

    少了這一條,「空檔案要補表頭」可以用「每次都寫表頭」來作弊,
    而那會讓軌跡裡散落一堆表頭列。
    """
    import pandas as pd

    hc.HEALTH_LOG.parent.mkdir(parents=True, exist_ok=True)
    hc.append_log([hc.Result("來源可達性 · vix", hc.OK, "ok")])
    hc.append_log([hc.Result("來源可達性 · vix", hc.FAIL, "bad")])

    text = hc.HEALTH_LOG.read_text(encoding="utf-8")
    assert text.count("date,ok,n_checks,failed") == 1
    df = pd.read_csv(hc.HEALTH_LOG)
    assert len(df) == 2 and list(df["ok"]) == [1, 0]


# ══════════════════════════════════════════════════════════════════════════
# 「PWA 部署」這一項原本一條測試都沒有(2026-10-19)
#
# 它是少數幾個能產生 FAIL 的檢查之一,而它的 FAIL 文案是「無法連線」——
# 也就是**最容易被一次網路抽風誤觸**的那種。既然它在替整個線上版把關,
# 它自己就需要被驗:抽一次風不報警、真的連不上要報警、
# 而「回了 200 但內容不對」不得被重試吞掉。
# ══════════════════════════════════════════════════════════════════════════


class _Resp:
    def __init__(self, status: int, body: str):
        self.status_code = status
        self.text = body


def _deploy_run(monkeypatch, cfg: dict, seq: list):
    """跑 check_pwa_deploy,依序回應第 n 次請求,回傳 (Result, 請求次數)。"""
    import requests  # noqa: PLC0415 — 只有這幾條測試需要造連線錯誤

    monkeypatch.setattr(hc, "_LIVE_BACKOFF", 0)
    calls = {"n": 0}

    def fake_get(url, **kw):
        item = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(hc.requests, "get", fake_get)
    assert requests  # 讓上面的 import 不是裝飾用
    return hc.check_pwa_deploy(cfg)[0], calls["n"]


def _good_page(cfg: dict) -> _Resp:
    return _Resp(200, "".join(cfg["pwa"]["expect_markers"]))


def test_a_healthy_pwa_deploy_is_ok(cfg: dict, monkeypatch) -> None:
    """控制組。少了它,下面每一條都可能是「永遠紅」的空轉。"""
    r, n = _deploy_run(monkeypatch, cfg, [_good_page(cfg)])
    assert r.status == hc.OK, r.detail
    assert n == 1, "成功時不該多問"


def test_a_transient_blip_is_not_a_dead_pwa(cfg: dict, monkeypatch) -> None:
    import requests

    r, n = _deploy_run(monkeypatch, cfg,
                       [requests.ConnectionError("boom"), _good_page(cfg)])
    assert r.status == hc.OK, r.detail
    assert n == 2, f"沒有重試,只請求了 {n} 次"


def test_a_pwa_that_really_is_unreachable_is_still_a_failure(
    cfg: dict, monkeypatch
) -> None:
    """安全網。重試用完仍然失敗必須 FAIL —— 否則「重試」就是「不報警」。"""
    import requests

    r, n = _deploy_run(monkeypatch, cfg, [requests.ConnectionError("boom")])
    assert r.status == hc.FAIL and r.failed, r.detail
    assert n == 3


def test_a_404_from_pages_is_not_retried(cfg: dict, monkeypatch) -> None:
    """4xx 是定論:Pages 說沒有這一頁。不重試,直接報。"""
    r, n = _deploy_run(monkeypatch, cfg, [_Resp(404, "Not Found")])
    assert r.status == hc.FAIL and "404" in r.detail
    assert n == 1, f"404 被重試了 {n} 次"


def test_a_200_with_missing_markers_is_still_a_failure(
    cfg: dict, monkeypatch
) -> None:
    """回了 200 但內容不是預期的那一版 —— 重試不得把這件事吞掉。

    這是「部署回退到舊版」唯一看得出來的訊號:HTTP 狀態完全正常,
    和「還在回 HTTP 200 的死鏡像」同一個形狀。
    """
    r, n = _deploy_run(monkeypatch, cfg, [_Resp(200, "<html>舊版</html>")])
    assert r.status == hc.FAIL and r.failed, r.detail
    assert n == 1, "內容不對不是『問不到』,不該重試"
