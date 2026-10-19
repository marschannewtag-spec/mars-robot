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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
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
    return date.today() - timedelta(days=1)


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
    old = date.today() - timedelta(days=cfg["pwa"]["mirror"]["max_age_days"] + 3)
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
    on_limit = date.today() - timedelta(days=limit)
    res = _run(monkeypatch, cfg,
               sp=_Resp(200, json.dumps(_sp_payload(on_limit))),
               vix=_Resp(200, _vix_csv(on_limit)))
    assert all(not r.failed for r in res.values()), \
        {k: v.detail for k, v in res.items()}

    over = date.today() - timedelta(days=limit + 1)
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
