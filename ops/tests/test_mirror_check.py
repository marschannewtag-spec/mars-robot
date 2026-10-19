"""健檢:`PWA 資料鏡像` 兩項(2026-10-19 新增)。

為什麼這一項必須有自己的測試
----------------------------
2026-10-19 起趨勢腿與 VIX 腿的**第一順位**都是本專案發布到 ops-data 的鏡像。
這條路把「免費 CORS 代理全死」拿掉了,但也把一個新的單點故障放進來 —— 鏡像。

而它的死法是靜默的,三層都看不出來:

  workflow 顏色   publish 是 `continue-on-error`,心跳在無差異時跳過 commit ——
                  「publish 掛了」和「今天沒有新資料」長得一模一樣
  PWA 畫面        抓不到鏡像會靜靜退到第二、第三順位,照樣有數字
  舊的健檢項目    資料源可達性與新鮮度量的是【本機剛抓到的】,不是使用者讀到的

所以這一項讀的是**線上那一份**。而既然它是現在唯一看著這件事的東西,
它自己壞掉或變成空轉就等於整層消失 —— 這個檔案釘的就是「它不會空轉」。

⚠ 特別釘住 404 必須是 FAIL。首次上線時鏡像還不存在,很容易想把 404 放寬成
   SKIP「還沒發布」—— 那會留下一個永久的洞:哪天檔案被刪掉或路徑改錯,
   健檢會是綠的而 PWA 已經退回月均價。正解是**上線前先把第一份鏡像種進
   ops-data**(2026-10-19 已做),讓 404 從頭到尾都只代表「真的壞了」。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
import yaml

import health_check as hc

OPS_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def cfg() -> dict:
    return yaml.safe_load((OPS_ROOT / "config.yml").read_text(encoding="utf-8"))


def _months_back(anchor: date, n: int) -> date:
    """往前推 n 個【日曆月】,日固定取 1 號。

    不要改用 `timedelta(days=30*n)` —— 30 天的步長會讓相鄰兩筆落在同一個
    日曆月(或跳過一個月),於是「造 40 個月」實際只有 39 個 distinct 月份。
    我第一版就是這樣寫的,害三條測試對著錯的月數在比。
    """
    idx = anchor.year * 12 + (anchor.month - 1) - n
    return date(idx // 12, idx % 12 + 1, 1)


def _sp_payload(last: date, months: int = 200) -> dict:
    """造一份 sp_monthly.json:`months` 個月,最後一筆是 `last`。"""
    ts, closes = [], []
    for i in range(months):
        day = last if i == months - 1 else _months_back(last, months - 1 - i)
        ts.append(int(datetime(day.year, day.month, day.day,
                               tzinfo=timezone.utc).timestamp()))
        closes.append(100.0 + i)
    return {"chart": {"error": None, "result": [{
        "timestamp": ts,
        "indicators": {"quote": [{"close": closes}]},
    }]}}


def _vix_csv(last: date, months: int = 150, header: str = "DATE,HIGH,CLOSE") -> str:
    """造一份 vix_daily.csv:`months` 個不同月份,最後一筆是 `last`。"""
    rows = [header]
    for i in range(months):
        day = last if i == months - 1 else _months_back(last, months - 1 - i)
        rows.append("%s,20.0,18.0" % day.isoformat())
    return "\n".join(rows) + "\n"


class _Resp:
    def __init__(self, status: int, body: str):
        self.status_code = status
        self.text = body

    def json(self):
        return json.loads(self.text)


def _run(monkeypatch, cfg: dict, *, sp: _Resp, vix: _Resp) -> dict[str, hc.Result]:
    """跑 check_published_mirror,依網址分派假回應。回傳 {名稱: Result}。"""
    sp_url = cfg["pwa"]["mirror"]["sp_url"]

    def fake_get(url, **kw):
        return sp if url == sp_url else vix

    monkeypatch.setattr(hc.requests, "get", fake_get)
    return {r.name: r for r in hc.check_published_mirror(cfg)}


def _fresh() -> date:
    return hc.today_utc() - timedelta(days=1)


def _ok_pair(cfg: dict) -> tuple[_Resp, _Resp]:
    return (_Resp(200, json.dumps(_sp_payload(_fresh()))),
            _Resp(200, _vix_csv(_fresh())))


# --------------------------------------------------------------------------
# 控制組 —— 健康時必須全綠。少了這條,下面每一條都可能是「永遠紅」的空轉。
# --------------------------------------------------------------------------

def test_a_healthy_mirror_is_ok(cfg: dict, monkeypatch) -> None:
    sp, vix = _ok_pair(cfg)
    res = _run(monkeypatch, cfg, sp=sp, vix=vix)
    assert set(res) == {"PWA 資料鏡像 · S&P", "PWA 資料鏡像 · VIX"}
    assert all(r.status == hc.OK for r in res.values()), \
        {k: v.detail for k, v in res.items()}
    assert hc.mirror_is_serving(list(res.values()))


# --------------------------------------------------------------------------
# 404 / 非 200
# --------------------------------------------------------------------------

def test_a_missing_sp_mirror_is_a_failure_not_a_skip(cfg: dict, monkeypatch) -> None:
    """404 必須是 FAIL。

    放寬成 SKIP(「還沒發布」)會留下永久的洞:路徑改錯或檔案被刪時
    健檢是綠的,而 PWA 已經退回 Shiller 月均價定義。
    """
    _, vix = _ok_pair(cfg)
    res = _run(monkeypatch, cfg, sp=_Resp(404, "Not Found"), vix=vix)
    r = res["PWA 資料鏡像 · S&P"]
    assert r.status == hc.FAIL and r.failed
    assert "404" in r.detail
    assert not hc.mirror_is_serving(list(res.values())), \
        "鏡像壞掉時 mirror_is_serving 必須回 False —— 代理鏈靠它決定要不要紅"


def test_a_missing_vix_mirror_is_a_failure(cfg: dict, monkeypatch) -> None:
    sp, _ = _ok_pair(cfg)
    res = _run(monkeypatch, cfg, sp=sp, vix=_Resp(404, "Not Found"))
    assert res["PWA 資料鏡像 · VIX"].failed
    assert res["PWA 資料鏡像 · S&P"].status == hc.OK, "兩項必須獨立判斷"


# --------------------------------------------------------------------------
# 過期 —— publish 成功但 push 失敗的情況
# --------------------------------------------------------------------------

def test_a_stale_mirror_is_caught(cfg: dict, monkeypatch) -> None:
    """鏡像停在很久以前 → FAIL。

    這是「publish 成功但心跳沒推上去」唯一會留下的痕跡:本機剛算出的資料
    很新,而使用者讀到的那一份沒動。兩者分開量才抓得到。
    """
    old = hc.today_utc() - timedelta(days=cfg["pwa"]["mirror"]["max_age_days"] + 3)
    res = _run(monkeypatch, cfg,
               sp=_Resp(200, json.dumps(_sp_payload(old))),
               vix=_Resp(200, _vix_csv(old)))
    for r in res.values():
        assert r.failed, r.detail
        assert "天" in r.detail


def test_the_age_boundary_is_inclusive_of_the_limit(cfg: dict, monkeypatch) -> None:
    """正好等於上限那天仍然算正常 —— 判準是「超過」才紅。

    邊界寫錯一天的方向,等於每個週末都紅一次。
    """
    limit = cfg["pwa"]["mirror"]["max_age_days"]
    on_limit = hc.today_utc() - timedelta(days=limit)
    res = _run(monkeypatch, cfg,
               sp=_Resp(200, json.dumps(_sp_payload(on_limit))),
               vix=_Resp(200, _vix_csv(on_limit)))
    assert all(not r.failed for r in res.values()), \
        {k: v.detail for k, v in res.items()}

    over = hc.today_utc() - timedelta(days=limit + 1)
    res = _run(monkeypatch, cfg,
               sp=_Resp(200, json.dumps(_sp_payload(over))),
               vix=_Resp(200, _vix_csv(over)))
    assert all(r.failed for r in res.values())


# --------------------------------------------------------------------------
# 格式 —— PWA 的解析器讀不懂就等於沒有
# --------------------------------------------------------------------------

def test_a_truncated_sp_mirror_is_caught(cfg: dict, monkeypatch) -> None:
    """月份太少 → FAIL。

    `spFromYahoo` 回傳少於 13 個月時 PWA 會丟掉它退到下一層(SMA12 要 13 個月),
    而那是靜默的。
    """
    _, vix = _ok_pair(cfg)
    res = _run(monkeypatch, cfg,
               sp=_Resp(200, json.dumps(_sp_payload(_fresh(), months=5))),
               vix=vix)
    r = res["PWA 資料鏡像 · S&P"]
    assert r.failed and "5 個月" in r.detail


def test_a_malformed_sp_mirror_is_caught(cfg: dict, monkeypatch) -> None:
    """少了 chart.result[0].timestamp 這層 → FAIL,不是當機。"""
    _, vix = _ok_pair(cfg)
    for body in ('{"chart":{"result":[{}]}}', '{"chart":{}}', "not json at all"):
        res = _run(monkeypatch, cfg, sp=_Resp(200, body), vix=vix)
        assert res["PWA 資料鏡像 · S&P"].failed, body


def test_a_vix_mirror_missing_high_is_caught(cfg: dict, monkeypatch) -> None:
    """少了 HIGH 欄 → FAIL。

    `buildVixMonthly` 會退回用收盤算「月內飆升」—— 歷史上有 9 個月份
    因此翻轉現金水位,而畫面上完全看不出來。
    """
    sp, _ = _ok_pair(cfg)
    res = _run(monkeypatch, cfg, sp=sp,
               vix=_Resp(200, _vix_csv(_fresh(), header="DATE,CLOSE,OPEN")))
    r = res["PWA 資料鏡像 · VIX"]
    assert r.failed and "HIGH" in r.detail


def test_a_short_vix_mirror_is_caught(cfg: dict, monkeypatch) -> None:
    """月份少於 96 + 36 → FAIL。

    不夠的話歷史圖會自己變短,而畫面上看不出是資料被裁掉
    還是歷史真的只有這麼長。
    """
    sp, _ = _ok_pair(cfg)
    res = _run(monkeypatch, cfg, sp=sp,
               vix=_Resp(200, _vix_csv(_fresh(), months=40)))
    r = res["PWA 資料鏡像 · VIX"]
    assert r.failed and "40 個月" in r.detail


def test_a_network_error_is_a_failure_not_a_crash(cfg: dict, monkeypatch) -> None:
    """連不上也是 FAIL —— 而且不能把整個 run_all 弄死。

    這一項排在 run_all 中段,拋出去的話後面的 PWA 部署檢查就不會跑,
    而使用者會看到一份少了幾項的報告卻沒人說少了什麼。
    """
    def boom(url, **kw):
        raise ConnectionError("bridge: 網路已停用")

    monkeypatch.setattr(hc.requests, "get", boom)
    res = {r.name: r for r in hc.check_published_mirror(cfg)}
    assert len(res) == 2
    assert all(r.failed for r in res.values())
    assert all("讀取失敗" in r.detail for r in res.values())


# --------------------------------------------------------------------------
# 設定本身
# --------------------------------------------------------------------------

def test_the_vix_floor_matches_what_the_chart_actually_needs(cfg: dict) -> None:
    """`min_vix_months` 必須 >= 96(圖)+ 36(滾動 z 分數)。

    把它調低是「放寬閾值讓檢查通過」最容易被接受的那種寫法 ——
    下限一旦低於畫面真正需要的月數,圖變短就不再有人會知道。
    """
    assert cfg["pwa"]["mirror"]["min_vix_months"] >= 96 + 36


def test_the_sp_floor_matches_the_sma_window(cfg: dict) -> None:
    """`min_sp_months` 必須 >= 13 —— SMA12 要 13 個月才算得出第一個訊號。"""
    assert cfg["pwa"]["mirror"]["min_sp_months"] >= 13


def test_a_vix_mirror_with_an_unparseable_date_is_caught(cfg: dict, monkeypatch) -> None:
    """DATE 欄不是日期 → FAIL,不能因為「算不出年齡」就放行。

    `_days_between` 對解析不出來的輸入回 0 —— 那是為了通知路徑設計的
    (Issue 內文的標記是外部狀態,不該讓通知流程掛掉)。但在**量測**路徑上
    它變成陷阱:一份 DATE 全是垃圾、但剛好有 132 個不同前七碼的檔案,
    會得到 age=0 而被判成 OK,而 PWA 拿到它會算出一條空的 VIX 腿。

    S&P 那一邊靠 `_mirror_last_day()` 回 None 已經擋住了,這一邊原本沒有。
    """
    sp, _ = _ok_pair(cfg)
    rows = ["DATE,HIGH,CLOSE"]
    for i in range(200):                       # 200 個不同的前七碼,衝過月數下限
        rows.append("junk-%03d-xx,20.0,18.0" % i)
    res = _run(monkeypatch, cfg, sp=sp, vix=_Resp(200, "\n".join(rows) + "\n"))
    r = res["PWA 資料鏡像 · VIX"]
    assert r.failed, "垃圾日期被放行了:%s" % r.detail
    assert "不是日期" in r.detail


def test_an_empty_vix_mirror_is_caught(cfg: dict, monkeypatch) -> None:
    """只有表頭、一列資料都沒有 → FAIL。

    `df["DATE"].iloc[-1]` 在空表上會 IndexError;那條路徑有 `len(df)` 擋著,
    結果是空字串 —— 空字串也必須是 FAIL,不是「年齡 0 天」。
    """
    sp, _ = _ok_pair(cfg)
    res = _run(monkeypatch, cfg, sp=sp, vix=_Resp(200, "DATE,HIGH,CLOSE\n"))
    assert res["PWA 資料鏡像 · VIX"].failed


# --------------------------------------------------------------------------
# 重試:「我們問不到」與「鏡像壞了」不是同一件事(2026-10-19)
#
# `fetch_data._get()` 一開始就是 3 次指數退避,而這個新檢查原本是整個 repo
# 唯一沒有重試的網路讀取 —— 偏偏它讀的是 PWA 的第一順位來源,而且它失敗時
# `mirror_is_serving()` 會回 False,連帶把「PWA 代理鏈」也推成 FAIL。
# 一次 20 秒的網路抽風 = 兩項故障 + 改標題 + 留一則言。
#
# ⚠ 這幾條測試同時釘住**重試不得變成放寬**:4xx 不重試、連續失敗仍然 FAIL。
# --------------------------------------------------------------------------

def _seq_run(monkeypatch, cfg: dict, *, sp_seq: list, vix_seq: list):
    """依序回應每個網址的第 n 次請求,並數出各自被請求幾次。

    序列元素是 `_Resp` 或 `Exception` 實例;用完之後固定重複最後一個
    (所以「只有一個元素」就代表「持續如此」)。
    """
    monkeypatch.setattr(hc, "_LIVE_BACKOFF", 0)  # 測試不要真的睡
    sp_url = cfg["pwa"]["mirror"]["sp_url"]
    calls = {"sp": 0, "vix": 0}

    def fake_get(url, **kw):
        which = "sp" if url == sp_url else "vix"
        seq = sp_seq if which == "sp" else vix_seq
        item = seq[min(calls[which], len(seq) - 1)]
        calls[which] += 1
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(hc.requests, "get", fake_get)
    return {r.name: r for r in hc.check_published_mirror(cfg)}, calls


def test_one_transient_connection_error_is_not_a_failure(cfg: dict, monkeypatch) -> None:
    """抽一次風之後讀到了,就不該報警 —— 使用者的瀏覽器此刻讀得到。"""
    sp, vix = _ok_pair(cfg)
    res, calls = _seq_run(monkeypatch, cfg,
                          sp_seq=[requests.ConnectionError("boom"), sp],
                          vix_seq=[vix])
    assert res["PWA 資料鏡像 · S&P"].status == hc.OK, res["PWA 資料鏡像 · S&P"].detail
    assert calls["sp"] == 2, f"沒有重試,只請求了 {calls['sp']} 次"
    assert hc.mirror_is_serving(list(res.values()))


def test_a_persistently_unreachable_mirror_is_still_a_failure(
    cfg: dict, monkeypatch
) -> None:
    """安全網:重試用完仍然失敗,必須 FAIL。

    少了這一條,「重試」就只是「永遠不報警」的另一種寫法。
    """
    _, vix = _ok_pair(cfg)
    res, calls = _seq_run(monkeypatch, cfg,
                          sp_seq=[requests.ConnectionError("boom")],
                          vix_seq=[vix])
    r = res["PWA 資料鏡像 · S&P"]
    assert r.status == hc.FAIL and r.failed, r.detail
    assert calls["sp"] == 3, f"重試次數應為 3,實際 {calls['sp']}"
    assert not hc.mirror_is_serving(list(res.values())), \
        "鏡像真的讀不到時,代理鏈必須失去它的安全網豁免"


def test_a_404_is_not_retried(cfg: dict, monkeypatch) -> None:
    """4xx 是**定論**,不重試。

    伺服器明確回答了「沒有這個檔案」。把它也塞進重試只會讓每次健檢多等
    兩輪退避,而且會讓「問不到」與「明確沒有」在程式裡糊成一件事 ——
    而這兩件事該有的反應不同(這份專案在 SKIP/FAIL 上反覆踩過同一條線)。
    """
    _, vix = _ok_pair(cfg)
    res, calls = _seq_run(monkeypatch, cfg,
                          sp_seq=[_Resp(404, "Not Found")], vix_seq=[vix])
    assert res["PWA 資料鏡像 · S&P"].status == hc.FAIL
    assert calls["sp"] == 1, f"404 被重試了 {calls['sp']} 次"


def test_a_transient_5xx_is_retried(cfg: dict, monkeypatch) -> None:
    """5xx 是 CDN 自己的暫時性故障,重試。"""
    sp, vix = _ok_pair(cfg)
    res, calls = _seq_run(monkeypatch, cfg,
                          sp_seq=[_Resp(503, "Service Unavailable"), sp],
                          vix_seq=[vix])
    assert res["PWA 資料鏡像 · S&P"].status == hc.OK, res["PWA 資料鏡像 · S&P"].detail
    assert calls["sp"] == 2


def test_a_persistent_5xx_is_reported_with_its_status_code(
    cfg: dict, monkeypatch
) -> None:
    """一直 5xx 仍然 FAIL,而且要把狀態碼講出來(診斷用)。"""
    sp, _ = _ok_pair(cfg)
    res, calls = _seq_run(monkeypatch, cfg,
                          sp_seq=[sp], vix_seq=[_Resp(503, "nope")])
    r = res["PWA 資料鏡像 · VIX"]
    assert r.status == hc.FAIL and "503" in r.detail, r.detail
    assert calls["vix"] == 3


# --------------------------------------------------------------------------
# 兩條腿的年齡必須各量各的(2026-10-20)
#
# `check_published_mirror` 原本只在 **S&P** 那一段指派 `age`,而 VIX 那一段
# 直接拿它來比。Python 的區域變數是函式範圍的,所以那個值會漏過去:
#
#   實測 A  S&P 新、VIX 停在 300 天前  → VIX 報 **OK**,detail 寫
#           「最後 2025-12-24(1 天前)」—— 日期和天數自己對不起來
#   實測 B  S&P HTTP 500(提早分支,`age` 從未被指派)、VIX 正常
#           → VIX 報「讀取失敗:cannot access local variable 'age'」
#   實測 C  S&P 停在 300 天前、VIX 新 → VIX 報 FAIL「最後 2026-10-19,已 300 天」
#
# 也就是說這個洩漏**兩個方向都會錯**:既藏掉真的過期,也造出假的過期。
# 而 VIX 腿的「過期」正是這整個檔案要抓的那件事(publish 成功但 push 失敗)。
#
# 為什麼既有測試看不見它:上面每一條年齡測試都把**兩份鏡像一起**搬到同一天,
# 於是洩漏來的值剛好等於正確值。同一個檔案在 404 那條已經寫了
# 「兩項必須獨立判斷」,年齡這條卻沒有人套用。
#
# 底下的 `_assert_self_consistent` 是比「VIX 要獨立」更強的釘子:
# **只要一行 detail 同時印出日期和天數,兩者就必須對得上。**
# 任何未來的重構把某個中間值接錯,都會在這裡停下來。
# --------------------------------------------------------------------------

_LAST_RE = re.compile(r"最後(?:一筆)?\s*(\d{4}-\d{2}-\d{2})")


def _stated(detail: str) -> tuple[date, int]:
    """從 detail 讀出它自己宣稱的「最後一筆日期」與「幾天」。"""
    m = _LAST_RE.search(detail)
    assert m, f"detail 沒有講最後一筆是哪天:{detail!r}"
    rest = detail[m.end():]
    a = (re.search(r"已\s*(\d+)\s*天", rest)
         or re.search(r"[((]\s*(\d+)\s*天前", rest))
    assert a, f"detail 沒有講幾天:{detail!r}"
    return date.fromisoformat(m.group(1)), int(a.group(1))


def _assert_self_consistent(r: hc.Result) -> None:
    """一行 detail 裡的日期和天數必須是同一份資料算出來的。"""
    last, age = _stated(r.detail)
    want = (hc.today_utc() - last).days
    assert age == want, (
        f"{r.name} 的 detail 自相矛盾:說最後一筆是 {last},卻說已 {age} 天"
        f"(對 UTC 今天 {hc.today_utc()} 應該是 {want} 天)—— {r.detail!r}"
    )


def test_a_stale_vix_mirror_is_caught_even_when_sp_is_fresh(
    cfg: dict, monkeypatch
) -> None:
    """VIX 停更、S&P 正常 → VIX 必須 FAIL。

    這是 VIX 腿「publish 成功但 push 失敗」的唯一痕跡。原本它拿的是 S&P 的
    年齡,所以 S&P 一天沒停,VIX 停多久都是綠的。
    """
    stale = hc.today_utc() - timedelta(days=300)
    res = _run(monkeypatch, cfg,
               sp=_Resp(200, json.dumps(_sp_payload(_fresh()))),
               vix=_Resp(200, _vix_csv(stale)))
    assert res["PWA 資料鏡像 · S&P"].status == hc.OK, res["PWA 資料鏡像 · S&P"].detail
    vix = res["PWA 資料鏡像 · VIX"]
    assert vix.failed, f"VIX 停了 300 天卻是 {vix.status}:{vix.detail!r}"
    _assert_self_consistent(vix)


def test_a_stale_sp_mirror_does_not_make_a_fresh_vix_look_stale(
    cfg: dict, monkeypatch
) -> None:
    """反向:S&P 停更不能把正常的 VIX 拖成假警報。

    假警報和漏報一樣貴 —— 這份專案的通知規則是「只有壞掉才通知」,
    一則內容自相矛盾的通知會直接侵蝕那條規則。
    """
    stale = hc.today_utc() - timedelta(days=300)
    res = _run(monkeypatch, cfg,
               sp=_Resp(200, json.dumps(_sp_payload(stale))),
               vix=_Resp(200, _vix_csv(_fresh())))
    assert res["PWA 資料鏡像 · S&P"].failed
    vix = res["PWA 資料鏡像 · VIX"]
    assert vix.status == hc.OK, f"VIX 是新的卻被報成 {vix.status}:{vix.detail!r}"
    _assert_self_consistent(vix)


def test_a_broken_sp_leg_does_not_corrupt_the_vix_verdict(
    cfg: dict, monkeypatch
) -> None:
    """S&P 走提早分支時,VIX 仍然要報出它自己的真實狀態。

    原本 S&P 任何一條提早分支都會讓 `age` 從未被指派,於是 VIX 報
    「讀取失敗:cannot access local variable 'age'」—— 一個 Python 內部錯誤
    被當成鏡像的健康狀態印給使用者看。
    """
    _, vix_ok = _ok_pair(cfg)
    broken_sp = [
        _Resp(404, "Not Found"),
        _Resp(500, "boom"),
        _Resp(200, json.dumps(_sp_payload(_fresh(), months=3))),
        _Resp(200, '{"chart":{"result":[{}]}}'),
        _Resp(200, "not json at all"),
    ]
    for sp in broken_sp:
        res = _run(monkeypatch, cfg, sp=sp, vix=vix_ok)
        r = res["PWA 資料鏡像 · VIX"]
        assert r.status == hc.OK, \
            f"S&P 壞掉({sp.status_code})把 VIX 的判斷弄壞了:{r.detail!r}"
        assert "age" not in r.detail, f"內部變數名漏進報告:{r.detail!r}"
        _assert_self_consistent(r)


def test_every_age_line_agrees_with_its_own_date(cfg: dict, monkeypatch) -> None:
    """掃描式安全網:四種年齡組合下,兩條腿的 detail 都必須自洽。

    ⚠ 這條測試自己有一個「真的掃到東西」的守衛 —— 少了它,未來若文案改成
    不再印日期,`_stated` 的 assert 會變成唯一的失敗點,而把它放寬掉
    (例如改成「找不到就跳過」)會讓整條測試無聲地變成空轉。
    """
    fresh, old = _fresh(), hc.today_utc() - timedelta(days=300)
    checked = 0
    for sp_day, vix_day in ((fresh, fresh), (fresh, old), (old, fresh), (old, old)):
        res = _run(monkeypatch, cfg,
                   sp=_Resp(200, json.dumps(_sp_payload(sp_day))),
                   vix=_Resp(200, _vix_csv(vix_day)))
        for want_day, name in ((sp_day, "PWA 資料鏡像 · S&P"),
                               (vix_day, "PWA 資料鏡像 · VIX")):
            r = res[name]
            _assert_self_consistent(r)
            last, _ = _stated(r.detail)
            assert last == want_day, \
                f"{name} 報出來的日期不是它自己那份鏡像的:{last} != {want_day}"
            assert r.failed is (want_day == old), \
                f"{name} 在 {want_day} 的判定是 {r.status}:{r.detail!r}"
            checked += 1
    assert checked == 8, f"掃描只跑到 {checked} 項,應該是 8 項"
