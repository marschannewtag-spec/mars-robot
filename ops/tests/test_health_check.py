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
