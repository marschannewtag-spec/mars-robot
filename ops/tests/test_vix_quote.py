"""盤中 VIX 報價 —— 產生端的判斷,與消費端真的會不會接。

這一層存在的理由寫在 `src/fetch_vix_quote.py` 的模組 docstring:
VIX 腿是領先腿,而它唯一的來源只收錄已收盤的交易日,所以整個交易時段裡
那條該最早動的腿看的是昨天。

這組測試釘的是**加上這一層之後不會變壞**,分成兩半:

* Python 側:哪些報價不可信。每一條都對應一種**不會有症狀**的壞法 ——
  值變成 0、欄位改名、時區假設不再成立。它們的共同點是:發布出去之後,
  畫面上只會看起來「今天比較平靜」,不會有任何錯誤訊息。
* JS 側(`vix_quote_bridge.mjs`):接上去之後**歷史有沒有被動到**。
  那不是靠讀原始碼確認「它看起來只會 append」,是真的跑 `buildVixMonthly`
  前後逐月比對 —— 判準要對準那個宣稱。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import OPS_ROOT, require_js_bridge

sys.path.insert(0, str(OPS_ROOT / "src"))
import fetch_vix_quote as fq  # noqa: E402

TESTS_DIR = Path(__file__).resolve().parent
FIXTURES = TESTS_DIR / "fixtures"
BRIDGE = TESTS_DIR / "vix_quote_bridge.mjs"
INDEX = OPS_ROOT.parent / "index.html"

# 2026-11-02 18:33:33Z 真的從 CBOE 抓下來的那一份,一個位元都沒改。
REAL = json.loads((FIXTURES / "cboe_vix_quote.json").read_text(encoding="utf-8"))


def _payload(**over) -> dict:
    """真實報價的副本,只動指定的欄位。對照組永遠是「原樣必須通過」。"""
    p = json.loads(json.dumps(REAL))
    for k, v in over.items():
        if k == "timestamp":
            p["timestamp"] = v
        else:
            p["data"][k] = v
    return p


# --------------------------------------------------------------------------
# 產生端:什麼可信、什麼不可信
# --------------------------------------------------------------------------


def test_the_real_recorded_quote_is_accepted() -> None:
    """對照組。沒有這一條的話,一個「什麼都拒絕」的解析器也會全綠。"""
    row = fq.parse_quote(REAL)
    assert row["date"] == "2026-11-02"
    assert row["high"] == 20.13
    assert row["close"] == 19.57
    assert row["low"] == 18.03


def test_the_trading_day_comes_from_eastern_time_not_from_utc() -> None:
    """交易日取自美東的最後成交時間,不是伺服器(UTC)那個時鐘。

    這是 ponytail 稽核拿來當範例的那個形狀:用 UTC 當「今天」,
    於是跨過午夜之後的那一筆被記到隔天。VIX 現貨目前收在 16:15 ET
    (= 21:15 UTC),所以實務上兩個日期還不會分岔 —— **但釘的是規則,
    不是今天的開盤時間**。哪天延長交易時段,這條會先紅。

    ⚠ 不要把它改成「比對 UTC 日期」讓它變簡單:那正是這條在擋的事。
    """
    row = fq.parse_quote(_payload(
        timestamp="2026-11-03 00:30:00", last_trade_time="2026-11-02T19:20:00"))
    assert row["date"] == "2026-11-02", "UTC 已經是 3 號了,但那一筆成交屬於 2 號"


@pytest.mark.parametrize("over,why", [
    ({"symbol": "^VIX9D"}, "符號換了 —— 那是另一個指數,不是這個引擎吃的東西"),
    ({"close": 0.0}, "0 會讓 lvl=(0-16)/9 被 clip 成 0,風險分數整個塌掉"),
    ({"high": 0.0}, "同上,而飆升項吃的正是 high"),
    ({"close": None}, "null"),
    ({"close": "19.57"}, "字串 —— 欄位型別變了就不要猜"),
    ({"high": 19.0}, "high < close,三個數不自洽"),
    ({"low": 25.0}, "low > close,三個數不自洽"),
    ({"close": 900.0}, "超出合理值域,歷史盤中最高是 89.53"),
    # ⚠ 三個數一起是 0 才是真正會發生的那個 —— 非交易時段的報價就長這樣。
    #   單獨把 close 設 0 會被 low<=close<=high 那條先擋下,所以那個輸入
    #   **分不出** VIX_MIN 有沒有在做事(`實測`:把 VIX_MIN 改成 0.0,
    #   整組測試照樣全綠)。這一列才是讓值域下界變紅的那個輸入。
    ({"low": 0.0, "close": 0.0, "high": 0.0}, "收盤後整組回 0 —— 0<=0<=0 自洽,只有值域擋得住"),
    ({"last_trade_time": "2026-10-30T16:15:00"}, "最後成交是上週五 —— 現在不在交易時段"),
    ({"last_trade_time": "2026-11-02T18:33:33"}, "與 UTC 同時刻 = 時區假設不再成立"),
    ({"last_trade_time": "not a time"}, "解析不了"),
    ({"timestamp": "not a time"}, "解析不了"),
])
def test_an_untrustworthy_quote_is_rejected(over: dict, why: str) -> None:
    """每一條都是一種【不會有症狀】的壞法,所以寧可不發布也不要發錯。

    發布出去之後畫面不會報錯,只會顯示一個比較小的數字 ——
    而這個工具存在的理由就是在那個數字該變大的時候讓它變大。
    """
    with pytest.raises(fq.QuoteRejected):
        fq.parse_quote(_payload(**over))


def test_a_rejected_quote_writes_no_file(tmp_path: Path, monkeypatch) -> None:
    """拒絕時不得留下檔案,而且**不得紅燈**。

    收盤後與週末「沒有可發布的報價」是正常狀態,不是故障。
    讓它紅等於每天響好幾次,而那種警報兩週內就會被無視 ——
    這個 repo 已經記錄過一次那個事故(連紅 47 天沒人看)。
    """
    monkeypatch.setattr(fq, "_get", lambda *a, **k: json.dumps(
        _payload(close=0.0)).encode())
    assert fq.main(["--out", str(tmp_path)]) == 0
    assert not (tmp_path / fq.QUOTE_FILE).exists()


def test_a_network_failure_writes_no_file_and_does_not_fail(
        tmp_path: Path, monkeypatch) -> None:
    """抓不到也一樣 —— 它是補充讀數,拿不到就回到日檔。"""
    def boom(*a, **k):
        raise TimeoutError("假裝逾時")

    monkeypatch.setattr(fq, "_get", boom)
    assert fq.main(["--out", str(tmp_path)]) == 0
    assert not (tmp_path / fq.QUOTE_FILE).exists()


def test_the_published_file_is_strict_json_with_no_nan(
        tmp_path: Path, monkeypatch) -> None:
    """Python 的 json 比瀏覽器寬鬆。NaN/Infinity 寫得出去,`JSON.parse` 讀不進來。

    用消費端的尺量 —— 這是這個 repo 反覆記錄的那條。
    """
    monkeypatch.setattr(fq, "_get", lambda *a, **k: json.dumps(REAL).encode())
    assert fq.main(["--out", str(tmp_path)]) == 0
    text = (tmp_path / fq.QUOTE_FILE).read_text(encoding="utf-8")
    for bad in ("NaN", "Infinity"):
        assert bad not in text
    json.loads(text)  # 嚴格模式:多餘的逗號、單引號、裸鍵都會在這裡炸


# --------------------------------------------------------------------------
# 消費端:PWA 真的會不會接,以及接了之後歷史有沒有動
# --------------------------------------------------------------------------


def _run_bridge(csv: str, quotes: list[dict], tmp_path: Path) -> list[dict]:
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps({"csv": csv, "quotes": quotes}, ensure_ascii=False),
                     encoding="utf-8")
    r = subprocess.run(
        ["node", str(BRIDGE), str(INDEX), str(cases)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=OPS_ROOT, timeout=180)
    assert r.returncode == 0, f"橋接器失敗:\n{r.stderr}"
    return json.loads(r.stdout)


CSV = ("DATE,HIGH,CLOSE\n"
       "2026-09-28,17.10,16.40\n"
       "2026-09-29,17.50,17.00\n"
       "2026-09-30,18.00,17.30\n"
       "2026-10-28,17.77,16.91\n"
       "2026-10-29,17.36,17.20\n"
       "2026-10-30,23.93,19.42\n")


def test_the_pwa_appends_a_real_quote_and_leaves_history_untouched(
        tmp_path: Path) -> None:
    """產生端真的吐出來的那個物件,餵進消費端真的在跑的那支函式。

    兩端的欄位名(`date` / `high` / `close`)在這裡對帳。只測其中一端的話,
    改了鍵名兩邊都還是綠的,而畫面會安靜地退回只有日檔 —— 沒有任何症狀。
    這正是這個 repo 最常見的那個 bug 家族:**同一份事實有 N 個定義,
    而沒有人對帳。**
    """
    require_js_bridge(
        "PWA 自己的 withLiveVixRow() 會接受 fetch_vix_quote.py 真的發出來的"
        "那個物件,而且接上去之後【除了最後一個月】沒有任何一個月的讀數"
        "或風險分數改變。")

    row = fq.parse_quote(REAL)
    [r] = _run_bridge(CSV, [{"name": "real", "quote": row}], tmp_path)

    assert r["appended"] is True
    assert r["live"] == "2026-11-02"
    assert r["monthsAfter"] == r["monthsBefore"] + 1
    assert r["lastYmAfter"] == "2026-11"
    assert r["lastNAfter"] == 1
    assert r["lastMaxAfter"] == 20.13
    assert r["historyIntact"] is True


def test_history_is_never_rewritten_by_the_intraday_row(tmp_path: Path) -> None:
    """同一個月裡接上去也一樣 —— 只有那個月前進,前面每一個月逐位元不動。

    ⚠ 這一條與上面那條不同:上面是新的月份(append 出一個新月),
      這一條是**既有的月份被加上一天**。後者才是月中每天都會發生的情況,
      而它更危險 —— 它會改到一個已經有讀數的月份的 close 與 max。
      改到的必須**只有那一個月**。
    """
    require_js_bridge(
        "盤中那一列寫進一個【已經存在】的月份時,只有那個月的 close 與 max "
        "前進,前面每一個月的讀數與風險分數逐一不變 —— 也就是一筆 15 分鐘 "
        "延遲的報價改不到黃金基準所錨定的任何歷史。")

    mid = dict(fq.parse_quote(REAL), date="2026-10-31", high=25.0, close=24.0)
    [r] = _run_bridge(CSV, [{"name": "same-month", "quote": mid}], tmp_path)

    assert r["appended"] is True
    assert r["monthsAfter"] == r["monthsBefore"], "10 月已經存在,不該多出一個月"
    assert r["lastYmAfter"] == "2026-10"
    assert r["lastMaxAfter"] == 25.0, "飆升項吃月內最高,新的盤中高點必須進得去"
    assert r["lastCloseAfter"] == 24.0
    assert r["historyIntact"] is True


@pytest.mark.parametrize("over,why", [
    # 「日檔太舊」與「日期在未來」各有一條獨立的測試 —— 它們要相對今天算,
    # 寫死在這張表裡會互相遮蔽。見下面那兩條的 docstring。
    ({"date": "2026-10-30"}, "與日檔最後一天同一天 —— 日檔已經有結算值了"),
    ({"date": "2026-10-29"}, "比日檔還舊,接上去等於改寫歷史"),
    ({"date": "11/02/2026"}, "日期格式不是 ISO"),
    ({"close": 0}, "0 會讓風險分數塌掉"),
    ({"high": 0}, "同上"),
    ({"close": 30.0}, "close > high,不自洽"),
    ({"high": "abc"}, "不是數字"),
])
def test_a_suspicious_quote_is_ignored_and_the_page_falls_back_to_the_daily_file(
        over: dict, why: str, tmp_path: Path) -> None:
    """可疑就不接,而不接的後果只是回到加這一層之前的行為。

    這是刻意挑的失效模式:**新東西壞掉 = 舊行為**,不是壞掉 = 畫面出錯。
    """
    require_js_bridge(
        "可疑的盤中報價(舊日期、0、不自洽、格式錯)一律不接,日檔 CSV 一個 "
        "位元都不變 —— 失去它就等於沒有人驗過「新東西壞掉時會回到舊行為」。")

    q = dict(fq.parse_quote(REAL), **over)
    [r] = _run_bridge(CSV, [{"name": why, "quote": q}], tmp_path)

    assert r["appended"] is False, f"這一筆不該被接受:{why}"
    assert r["changed"] is False, "沒接受就不得動到 CSV"
    assert r["monthsAfter"] == r["monthsBefore"]
    assert r["lastYmAfter"] == "2026-10"


def test_a_frozen_daily_file_is_not_papered_over_by_one_live_row(
        tmp_path: Path) -> None:
    """日檔自己凍結的時候,不接那一列 —— 不能讓新東西遮住舊東西的故障。

    `實測` 2026-09:上游鏡像停更 26 天,而它仍然 HTTP 200、仍然回 9278 列。
    那種時候接一列孤零零的今天上去,月序列會多出一個前面缺好幾個月的單日月份,
    而滾動窗是**照位置**取的 —— 整排 z 分數與飆升項都會錯位,畫面上沒有症狀。

    ⚠ 這條測試用的日期是**相對今天**算的,不是寫死的。寫死的話
      (第一版就是:日檔 2026-10-30、報價 2026-11-09)那個報價同時也落在
      「未來」那一條裡,於是這條與未來那條**互相遮蔽**,兩個突變都逃掉。
      判準要對準那個宣稱:這裡要量的是「日檔太舊」,就不能讓輸入順便犯別的規。
    """
    require_js_bridge(
        "日檔自己凍結超過一週時,盤中那一列不接 —— 失去它就沒有人驗過"
        "「新加的來源不會遮住舊來源的故障」,而那正是 2026-09 鏡像停更 26 天 "
        "卻一路 HTTP 200 的那個形狀。")

    import datetime as _dt
    today = _dt.date.today()
    stale_last = today - _dt.timedelta(days=30)   # 日檔停在一個月前
    csv = ("DATE,HIGH,CLOSE\n"
           f"{stale_last - _dt.timedelta(days=1)},17.50,17.00\n"
           f"{stale_last},18.00,17.30\n")
    q = dict(fq.parse_quote(REAL), date=today.isoformat())  # 今天,不是未來

    [r] = _run_bridge(csv, [{"name": "stale-daily", "quote": q}], tmp_path)
    assert r["appended"] is False, "日檔落後 30 天,這一列不該接上去"
    assert r["changed"] is False


def test_a_quote_dated_in_the_future_is_ignored(tmp_path: Path) -> None:
    """日期在未來的報價不接,即使它離日檔最後一天很近。

    ⚠ 同樣是相對今天算的,理由同上:寫一個 2099 年的日期進去,它會先被
      「日檔太舊」那條擋掉,於是這條守衛**永遠不會是擋下它的那一個**。
      這裡刻意讓它只犯一條規 —— 與日檔只差 3 天(過得了 MAX_GAP_DAYS),
      但落在明天之後。
    """
    require_js_bridge(
        "日期落在明天之後的盤中報價不接 —— 失去它就沒有人驗過這道守衛,"
        "而一個未來日期會在月序列尾端造出一個不存在的月份,畫面上沒有症狀。")

    import datetime as _dt
    today = _dt.date.today()
    csv = ("DATE,HIGH,CLOSE\n"
           f"{today - _dt.timedelta(days=2)},17.50,17.00\n"
           f"{today - _dt.timedelta(days=1)},18.00,17.30\n")
    q = dict(fq.parse_quote(REAL),
             date=(today + _dt.timedelta(days=2)).isoformat())

    [r] = _run_bridge(csv, [{"name": "future", "quote": q}], tmp_path)
    assert r["appended"] is False, "後天的報價不存在"
    assert r["changed"] is False


def test_a_missing_quote_file_changes_nothing(tmp_path: Path) -> None:
    """檔案不存在、404、壞掉的 JSON —— 全部走同一條路:當它不存在。"""
    require_js_bridge(
        "報價檔缺席、404、或 JSON 壞掉時一律當它不存在,日檔 CSV 一個位元 "
        "都不變 —— 失去它就沒有人驗過這一層的失效模式真的是「回到舊行為」。")

    cases = [{"name": "null", "quote": None},
             {"name": "empty", "quote": {}},
             {"name": "string", "quote": "19.5"},
             {"name": "array", "quote": []}]
    for r in _run_bridge(CSV, cases, tmp_path):
        assert r["appended"] is False, r["name"]
        assert r["changed"] is False, r["name"]
