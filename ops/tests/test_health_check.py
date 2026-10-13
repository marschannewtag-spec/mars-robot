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
    days = _trading_days(date.today() - timedelta(days=400), 60)
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
    days = _trading_days(date.today() - timedelta(days=90), 60)
    write_sp(sandbox, days)
    write_vix(sandbox, days)
    (sandbox / fetch_data.SOURCES["shiller"].filename).write_text(
        f"Date,SP500,Dividend,Long Interest Rate\n"
        f"{date.today().isoformat()},3000,50,2.5\n",
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
    assert declared <= set(cfg["freshness_days"]), (
        f"這些來源沒有新鮮度上限:{declared - set(cfg['freshness_days'])}"
    )


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
    vix_days = _trading_days(date.today() - timedelta(days=365 * 33), 8200)
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
    days = _trading_days(date.today() - timedelta(days=400), 260)

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
    monkeypatch.setattr(hc, "check_proxies", lambda cfg: [])
    monkeypatch.setattr(hc, "check_pwa_deploy", lambda cfg: [])
    monkeypatch.setattr(hc, "check_worker", lambda cfg: [])

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
    """沒變動就不留言 —— 這一條就是 52 則留言不會再發生的原因。"""
    results = _r("PWA 代理鏈")
    body = hc.render_markdown(results)
    assert hc.issue_plan(body, body)["changed"] is False
    assert hc.change_summary(hc.failset(results), results) is None


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
