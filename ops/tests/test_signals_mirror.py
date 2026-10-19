"""健檢:`PWA 體制訊號`(2026-10-20 新增)。

為什麼這一項必須存在
--------------------
體制層(CNN 貪婪指數 / 國發會燈號 / 主體判定 / 六條早期警訊)是整套監控裡
**唯一沒有人盯著**的那一塊:

  workflow 顏色   產生 `signals.json` 的步驟是 `continue-on-error` ——
                  「輔助讀數不該有能力讓每日健檢變紅燈」。那個決定是對的,
                  但副作用是那一步**連續失敗一個月也看不出來**。
  PWA 畫面        `實測`屬實:index.html 一律顯示資料年齡,超過
                  `SIGNALS_STALE_DAYS`(7)轉琥珀色並加一行提醒。
                  但那要使用者**自己打開頁面並讀那行小字**才看得到。
  既有健檢項目    `PWA 資料鏡像` 只看 sp_monthly.json 與 vix_daily.csv。
                  `health_check.py` 裡原本一次都沒有出現過 "signals"。

也就是說「沒消息就是好消息」在這一塊是空的 —— 它壞掉時什麼消息都不會有。

⚠ 這一項**刻意不計入 `mirror_is_serving()`**。那個回傳值決定代理鏈的嚴重
   程度;體制層與代理鏈毫無關係,併進去等於讓一個輔助讀數有能力把趨勢腿的
   判定推成紅燈。本檔最後一條測試釘住這件事。
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

import health_check as hc

OPS_ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = OPS_ROOT.parent / "index.html"


@pytest.fixture(scope="module")
def cfg() -> dict:
    return yaml.safe_load((OPS_ROOT / "config.yml").read_text(encoding="utf-8"))


class _Resp:
    def __init__(self, status: int, body: str):
        self.status_code = status
        self.text = body

    def json(self):
        return json.loads(self.text)


def _payload(days_old: int = 0, *, flags: int = 6,
             drop: tuple[str, ...] = ()) -> dict:
    gen = hc.today_utc() - timedelta(days=days_old)
    d = {
        "schema": 1,
        "generated_at": f"{gen}T03:00:00+00:00",
        "dominance": {"leader": "美股", "scores": {"美股": 1.0}, "margin": 1.2},
        "warning": {"n_triggered": 0, "n_usable": flags, "band": "平靜",
                    "flags": [{"label": f"f{i}", "triggered": False, "ok": True}
                              for i in range(flags)]},
        "cnn": {"value": 50},
        "ndc": {"value": 20},
        "sources": {"cnn": {"ok": True}},
    }
    for k in drop:
        d.pop(k, None)
    return d


def _run(monkeypatch, cfg: dict, resp: _Resp) -> hc.Result:
    monkeypatch.setattr(hc, "_LIVE_BACKOFF", 0)
    monkeypatch.setattr(hc.requests, "get", lambda url, **kw: resp)
    out = hc.check_published_signals(cfg)
    assert len(out) == 1, f"這一項只該產出一個結果:{out}"
    return out[0]


# --------------------------------------------------------------------------
# 控制組 —— 少了它,底下每一條都可能是「永遠紅」的空轉
# --------------------------------------------------------------------------

def test_a_healthy_signals_file_is_ok(cfg: dict, monkeypatch) -> None:
    r = _run(monkeypatch, cfg, _Resp(200, json.dumps(_payload())))
    assert r.status == hc.OK, r.detail
    assert str(hc.today_utc()) in r.detail, f"沒報出它自己的日期:{r.detail!r}"


# --------------------------------------------------------------------------
# 停更 —— 「產生體制訊號」那一步連續失敗時唯一的痕跡
# --------------------------------------------------------------------------

def test_a_stale_signals_file_is_caught(cfg: dict, monkeypatch) -> None:
    limit = cfg["pwa"]["signals"]["max_age_days"]
    r = _run(monkeypatch, cfg, _Resp(200, json.dumps(_payload(limit + 1))))
    assert r.failed, f"已經 {limit + 1} 天卻是 {r.status}:{r.detail!r}"
    assert f"{limit + 1} 天" in r.detail, r.detail


def test_the_age_boundary_is_inclusive_of_the_limit(cfg: dict, monkeypatch) -> None:
    """正好等於上限那天仍然算正常 —— 判準是「超過」才紅。

    邊界寫錯一天的方向,等於每個連假都紅一次,而狼來了比漏報更快毀掉
    「只有壞掉才通知」。
    """
    limit = cfg["pwa"]["signals"]["max_age_days"]
    on_limit = _run(monkeypatch, cfg, _Resp(200, json.dumps(_payload(limit))))
    assert not on_limit.failed, on_limit.detail
    over = _run(monkeypatch, cfg, _Resp(200, json.dumps(_payload(limit + 1))))
    assert over.failed, over.detail


def test_the_stated_age_agrees_with_the_stated_date(cfg: dict, monkeypatch) -> None:
    """detail 裡的日期和天數必須是同一份資料算出來的。

    與 test_mirror_check.py 的 `_assert_self_consistent` 同一個釘子 ——
    VIX 鏡像那次就是靠它才看出「最後 2025-12-24(1 天前)」自相矛盾。
    """
    for days in (0, 3, 30):
        r = _run(monkeypatch, cfg, _Resp(200, json.dumps(_payload(days))))
        m = re.search(r"(\d{4}-\d{2}-\d{2})", r.detail)
        assert m, r.detail
        a = (re.search(r"已\s*(\d+)\s*天", r.detail)
             or re.search(r"[((]\s*(\d+)\s*天前", r.detail))
        assert a, r.detail
        from datetime import date
        stated = date.fromisoformat(m.group(1))
        assert int(a.group(1)) == (hc.today_utc() - stated).days, \
            f"日期與天數對不起來:{r.detail!r}"


# --------------------------------------------------------------------------
# 消費者真正需要的東西 —— renderRegime() 的前置條件
# --------------------------------------------------------------------------

@pytest.mark.parametrize("drop", ["dominance", "warning"])
def test_a_field_renderRegime_requires_is_a_failure(
    cfg: dict, monkeypatch, drop: str
) -> None:
    """少了 `dominance` 或 `warning` → 面板整塊不顯示,所以是 FAIL。

    `renderRegime()` 的第一行就是
    `if(!dom||!warn||!Array.isArray(warn.flags)||!warn.flags.length)return;`
    —— 少了任何一個,整塊面板不渲染。而「面板消失」和「這塊本來就沒有」
    在畫面上長得一模一樣,使用者不會知道自己少看了東西。
    """
    r = _run(monkeypatch, cfg,
             _Resp(200, json.dumps(_payload(drop=(drop,)))))
    assert r.failed, f"{drop} 不見了卻放行:{r.detail!r}"
    assert drop in r.detail


def test_an_empty_flag_list_is_a_failure(cfg: dict, monkeypatch) -> None:
    """`warning.flags` 空陣列也會讓面板整塊不顯示 —— 不是「0 條警訊」。

    這是 truthy 檢查最容易漏掉的一格:`warning` 在、`flags` 在,但長度 0。
    """
    r = _run(monkeypatch, cfg, _Resp(200, json.dumps(_payload(flags=0))))
    assert r.failed, f"空的 flags 被當成健康:{r.detail!r}"


def test_an_unparseable_generated_at_is_a_failure(cfg: dict, monkeypatch) -> None:
    """`generated_at` 不是日期 → FAIL,不能因為「算不出年齡」就放行。

    `_days_between` 對解析不出來的輸入回 0(那是為了通知路徑設計的),
    所以放著不管會得到 age=0 而永遠綠 —— 與 VIX 鏡像 DATE 欄全是垃圾
    那次完全同一個陷阱。PWA 那端同理:`Date.parse` 回 NaN,
    `isFinite(gen)` 為假,那行年齡提醒**永遠不會出現**。
    """
    for bad in ("", "尚未產生", "N/A", None):
        p = _payload()
        p["generated_at"] = bad
        r = _run(monkeypatch, cfg, _Resp(200, json.dumps(p)))
        assert r.failed, f"{bad!r} 被放行:{r.detail!r}"


# --------------------------------------------------------------------------
# 網路
# --------------------------------------------------------------------------

def test_a_404_is_a_failure(cfg: dict, monkeypatch) -> None:
    r = _run(monkeypatch, cfg, _Resp(404, "Not Found"))
    assert r.failed and "404" in r.detail


def test_malformed_json_is_a_failure_not_a_crash(cfg: dict, monkeypatch) -> None:
    r = _run(monkeypatch, cfg, _Resp(200, "not json at all"))
    assert r.failed and "讀取失敗" in r.detail


def test_a_network_error_is_a_failure_not_a_crash(cfg: dict, monkeypatch) -> None:
    """連不上是 FAIL,而且不能把 run_all 弄死 —— 它後面還有三項要跑。"""
    monkeypatch.setattr(hc, "_LIVE_BACKOFF", 0)

    def boom(url, **kw):
        raise ConnectionError("bridge: 網路已停用")

    monkeypatch.setattr(hc.requests, "get", boom)
    out = hc.check_published_signals(cfg)
    assert len(out) == 1 and out[0].failed
    assert "讀取失敗" in out[0].detail


# --------------------------------------------------------------------------
# 設定不得漂移
# --------------------------------------------------------------------------

def test_the_checked_url_is_the_one_the_pwa_reads() -> None:
    """健檢探測的網址,必須就是 PWA 真的在讀的那個。

    與代理清單漂移同形狀:健檢盯著一個 PWA 沒在讀的檔案,
    而 PWA 在讀的那個沒人看著。
    """
    cfg = yaml.safe_load((OPS_ROOT / "config.yml").read_text(encoding="utf-8"))
    html = INDEX_HTML.read_text(encoding="utf-8")
    m = re.search(r'const OPS_SIGNALS\s*=\s*"([^"]+)"', html)
    assert m, "index.html 找不到 OPS_SIGNALS"
    assert cfg["pwa"]["signals"]["url"] == m.group(1)
    assert m.group(1).startswith("https://raw.githubusercontent.com/"), (
        "只有 raw.githubusercontent.com 會送 Access-Control-Allow-Origin: * —— "
        "換成別的網域,瀏覽器會直接被 CORS 擋掉")
    assert "/ops-data/" in m.group(1)


def test_the_two_staleness_thresholds_agree() -> None:
    """config 的 `max_age_days` 必須等於 index.html 的 `SIGNALS_STALE_DAYS`。

    兩個數字分岔 = 同一個問題有兩個答案。`markets.ok` 那次就是這樣 ——
    一個門檻說「夠了」,另一個說「不夠」,而報告只印其中一個。

    ⚠ 這兩邊的用途確實不同(一個決定要不要開 Issue,一個決定畫面上
      那行字要不要轉琥珀色),但「這份資料算不算過期」只有一個答案。
      要讓它們不同,必須是**刻意**的,而刻意就得先讓這條測試紅一次。
    """
    cfg = yaml.safe_load((OPS_ROOT / "config.yml").read_text(encoding="utf-8"))
    html = INDEX_HTML.read_text(encoding="utf-8")
    m = re.search(r"const SIGNALS_STALE_DAYS\s*=\s*(\d+)", html)
    assert m, "index.html 找不到 SIGNALS_STALE_DAYS"
    assert cfg["pwa"]["signals"]["max_age_days"] == int(m.group(1)), (
        f"健檢用 {cfg['pwa']['signals']['max_age_days']} 天、"
        f"PWA 用 {m.group(1)} 天 —— 同一份檔案有兩個「過期」的定義")


# --------------------------------------------------------------------------
# 它不可以影響代理鏈
# --------------------------------------------------------------------------

def test_the_regime_check_does_not_feed_mirror_is_serving(
    cfg: dict, monkeypatch
) -> None:
    """體制層壞掉**不得**把代理鏈推成紅燈。

    `mirror_is_serving()` 只看名稱以「PWA 資料鏡像」開頭的項目。
    若有人日後把這一項改名或併進鏡像那個清單,代理鏈的嚴重程度就會被
    一個輔助讀數綁架 —— 一次 CNN 抽風 = 趨勢腿報警。
    """
    broken = _run(monkeypatch, cfg, _Resp(500, "boom"))
    assert broken.failed
    assert hc.mirror_is_serving([broken]), \
        "體制訊號失敗卻讓 mirror_is_serving() 回 False"
    assert not broken.name.startswith("PWA 資料鏡像"), \
        f"名稱撞進鏡像那一族了:{broken.name!r}"


def test_run_all_includes_the_regime_check() -> None:
    """安全網:新檢查必須真的被 run_all() 叫到。

    寫了一個沒有人呼叫的檢查,和沒有寫是同一件事 —— 而它的症狀同樣是
    一切安靜。用原始碼而不是跑 run_all(),因為後者會打十幾次網路。
    """
    src = (OPS_ROOT / "src" / "health_check.py").read_text(encoding="utf-8")
    body = src.split("def run_all()", 1)[1].split("\ndef ", 1)[0]
    assert "check_published_signals(cfg)" in body, \
        "check_published_signals() 沒有被 run_all() 呼叫"
