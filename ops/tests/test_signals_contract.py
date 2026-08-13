"""signals.json 的契約 —— 產生端與消費端之間的那條縫。

這層的設計是「失敗即靜默」,所以任何路徑或欄位對不上都**沒有症狀**:
畫面就是不顯示那塊,和「今天剛好沒訊號」長得一模一樣。心跳指示器就踩過
同一個坑(路徑寫成 `data/manifest.json`,實際是 `ops/data/manifest.json`)。
所以這裡把兩端釘在一起,靠測試而不是靠記性。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import fetch_signals
import regime

OPS_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = OPS_ROOT.parent
INDEX = REPO_ROOT / "index.html"


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def payload() -> dict:
    """完全離線組出一份 payload —— 六條旗標全部缺資料的極端情況。

    刻意用最壞的輸入:如果連「什麼都抓不到」都能產出結構正確的 JSON,
    那真實資料只會更好。
    """
    dom = regime.dominance({})
    warn = regime.warning_flags(None, None)
    return {
        "schema": 1,
        "generated_at": "2026-08-12T00:00:00+00:00",
        "dominance": {"leader": dom.leader, "scores": dom.scores, "matrix": dom.matrix,
                      "margin": None, "n_markets": dom.n_markets, "window": "2y"},
        "warning": {"n_triggered": warn.n_triggered, "n_usable": warn.n_usable,
                    "band": warn.band,
                    "flags": [{"key": f.key, "label": f.label, "triggered": f.triggered,
                               "detail": f.detail, "rule": f.rule, "source": f.source,
                               "ok": f.ok, "why": f.why} for f in warn.flags]},
        "cnn": None, "ndc": None, "sources": {},
    }


# ── 路徑必須對得上 ───────────────────────────────────────────────────────


def test_pwa_url_matches_where_the_writer_actually_writes(html: str) -> None:
    m = re.search(r'OPS_SIGNALS\s*=\s*"([^"]+)"', html)
    assert m, "index.html 找不到 OPS_SIGNALS"
    url = m.group(1)

    rel = fetch_signals.SIGNALS_PATH.relative_to(REPO_ROOT).as_posix()
    assert url.endswith(rel), (
        f"PWA 抓 {url},但 fetch_signals 寫到 {rel} —— 路徑對不上會 404,"
        f"而 404 在這個設計下【完全沒有症狀】"
    )
    assert "/ops-data/" in url, "必須讀 ops-data 分支(main 上沒有這個檔)"
    assert url.startswith("https://raw.githubusercontent.com/"), (
        "只有 raw.githubusercontent.com 會送 Access-Control-Allow-Origin: *"
    )


def test_signals_is_pushed_by_the_daily_workflow() -> None:
    """產生了卻沒推上去,PWA 一樣讀不到。把 workflow 也綁進來。"""
    wf = (REPO_ROOT / ".github" / "workflows" / "daily-health.yml").read_text(encoding="utf-8")
    assert "fetch_signals.py" in wf, "daily-health.yml 沒有產生 signals.json"
    assert "ops/data/signals.json" in wf, "daily-health.yml 沒有把 signals.json 加進 git"


# ── 結構契約 ─────────────────────────────────────────────────────────────


FLAG_FIELDS = {"key", "label", "triggered", "detail", "rule", "source", "ok", "why"}


def test_payload_shape(payload: dict) -> None:
    assert payload["schema"] == 1
    for k in ("generated_at", "dominance", "warning"):
        assert k in payload
    for k in ("leader", "scores", "margin", "n_markets", "window"):
        assert k in payload["dominance"]
    for k in ("n_triggered", "n_usable", "band", "flags"):
        assert k in payload["warning"]


def test_every_flag_carries_every_field_the_pwa_renders(payload: dict) -> None:
    for f in payload["warning"]["flags"]:
        assert FLAG_FIELDS <= set(f), f"旗標缺欄位:{FLAG_FIELDS - set(f)}"


def test_pwa_reads_only_fields_the_writer_emits(html: str, payload: dict) -> None:
    """PWA 裡出現的 f.xxx 必須都是產生端真的有寫的欄位。

    少一個欄位在畫面上只會顯示 undefined —— 沒有錯誤、沒人會發現。
    """
    used = set(re.findall(r"\bf\.([a-z_]+)", html))
    assert used, "index.html 裡找不到旗標欄位的使用"
    assert used <= FLAG_FIELDS, f"PWA 讀了產生端沒寫的欄位:{used - FLAG_FIELDS}"


def test_payload_survives_json_roundtrip(payload: dict) -> None:
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload


# ── 產出的必須是【JS 讀得懂的】JSON ──────────────────────────────────────
#
# 2026-08-13 實測到的 bug:`dominance()` 在第二名總分為 0 時回 `float("inf")`
# (「領先到沒有可比性」,語意正確)。build_signals 的守門式寫的是 `x != x`,
# 那只擋 NaN —— `inf != inf` 是 False,所以 inf 一路溜到 json.dumps,
# 寫出裸的 `Infinity`。那不是合法 JSON(RFC 8259 沒這個字面值),
# 瀏覽器的 JSON.parse 直接拋,而 checkRegime 的 catch 是空的
# => **整塊體制面板無聲消失**,症狀和「檔案不存在」一模一樣。
#
# 上面那條 round-trip 測試永遠抓不到它:**Python 的 json 接受 Infinity。**
# 真正的消費端是 JS,所以判準必須用 JS 的。


def _reject_constant(name: str):
    raise AssertionError(
        f"signals.json 出現了 `{name}` —— 那不是合法 JSON,"
        f"瀏覽器的 JSON.parse 會拋,體制面板會無聲消失"
    )


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_numbers_never_reach_the_json(bad: float) -> None:
    assert fetch_signals.jsonable(bad) is None, (
        f"{bad} 沒有被擋下。注意 `x != x` 只擋 NaN,擋不住 inf"
    )


def test_jsonable_keeps_real_numbers() -> None:
    """對照組 —— 不能是靠「什麼都回 None」達成的。"""
    for good in (0.0, -1.5, 4.79, 1e9):
        assert fetch_signals.jsonable(good) == good
    assert fetch_signals.jsonable(None) is None


def test_dominance_really_can_produce_an_infinite_margin() -> None:
    """先證明這個情境存在,否則下面那條只是在測一個不會發生的事。

    兩個市場、其中一個的報酬是常數 -> 它當領先者時 R² 全是 NaN
    -> 第二名總分 0 -> `margin = top / 0` 被寫成 `float("inf")`。
    """
    a = {d: (0.01 if d % 2 else -0.01) for d in range(1, 200)}
    b = {d: 0.0 for d in range(1, 200)}
    assert regime.dominance({"甲": a, "乙": b}).margin == float("inf")


def test_build_signals_output_is_strict_json_when_margin_is_infinite(
        monkeypatch) -> None:
    """走**真正的組裝路徑**,不是只測 jsonable() 這個小工具。

    這一條是 S2 突變(把守門式改回 `x != x`)唯一抓得到的地方 ——
    只測工具函式的話,呼叫點改壞了也沒人知道。
    """
    prices_alt = {1_900_000 + i: (100.0 if i % 2 else 101.0) for i in range(200)}
    prices_flat = {1_900_000 + i: 100.0 for i in range(200)}
    live = list(fetch_signals.MARKETS)[:2]

    def fake_market(symbol: str) -> dict[int, float]:
        syms = [fetch_signals.MARKETS[n] for n in live]
        if symbol == syms[0]:
            return prices_alt
        if symbol == syms[1]:
            return prices_flat
        raise RuntimeError("這個市場今天抓不到")   # 其餘四個市場失敗

    def boom() -> dict:
        raise RuntimeError("來源掛了")

    monkeypatch.setattr(fetch_signals, "fetch_market", fake_market)
    monkeypatch.setattr(fetch_signals, "fetch_cnn", boom)
    monkeypatch.setattr(fetch_signals, "fetch_ndc", boom)

    payload = fetch_signals.build_signals()
    assert payload["dominance"]["margin"] is None, (
        f"margin 應該被擋成 None,實際是 {payload['dominance']['margin']!r}"
    )
    # 用 JS 的尺量:Python 的 json 接受 Infinity,拿它當判準等於量錯了
    json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=True),
               parse_constant=_reject_constant)


def test_write_signals_refuses_to_emit_invalid_json(tmp_path, monkeypatch) -> None:
    """第二道防線:jsonable() 若哪天漏了一條路徑,寫檔要**拋**而不是安靜寫壞。

    寫不出來時 workflow 會沿用 ops-data 上的舊版 ——
    舊資料好過一份會讓面板消失的新資料。
    """
    monkeypatch.setattr(fetch_signals, "SIGNALS_PATH", tmp_path / "signals.json")
    with pytest.raises(ValueError):
        fetch_signals.write_signals({"margin": float("inf")})


def test_the_committed_signals_file_is_strict_json() -> None:
    """實際產出的那份檔案(若在本機存在)也要通過同一把尺。"""
    path = fetch_signals.SIGNALS_PATH
    if not path.exists():
        pytest.skip("本機還沒有 signals.json(CI 上由 workflow 產生)")
    json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能用真正的 JSON.parse")
def test_javascript_can_parse_what_python_wrote(payload: dict, tmp_path) -> None:
    """最終判準:交給**真正的** JSON.parse。

    Python 的 json 對 Infinity/NaN 太寬容,拿它當判準等於用錯的尺量。
    """
    p = tmp_path / "signals.json"
    p.write_text(json.dumps({**payload, "dominance": {**payload["dominance"],
                                                      "margin": None}},
                            ensure_ascii=False), encoding="utf-8")
    r = subprocess.run(
        ["node", "-e",
         "const fs=require('fs');JSON.parse(fs.readFileSync(process.argv[1],'utf8'));"
         "console.log('ok')", str(p)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert r.returncode == 0, f"JS 解析不了 Python 寫出的 JSON:\n{r.stderr}"


def test_everything_missing_still_produces_a_valid_payload(payload: dict) -> None:
    """全部資料源掛掉時不能爆,而且必須誠實回報「不知道」。"""
    assert payload["dominance"]["leader"] is None
    assert payload["warning"]["n_usable"] == 0
    assert payload["warning"]["band"] == "無"
    assert len(payload["warning"]["flags"]) == 6


# ── 失敗即靜默 ───────────────────────────────────────────────────────────


def test_regime_fetch_failure_is_silent(html: str) -> None:
    """抓不到就整塊不顯示。會亂叫的指示燈幾天後就會被無視,那等於沒有。"""
    block = html[html.index("function checkRegime"):]
    block = block[:block.index("checkRegime();")]
    assert re.search(r"\.catch\(\s*\(\s*\)\s*=>\s*\{\s*\}\s*\)", block), (
        "checkRegime 的 catch 被改成會顯示東西了 —— 這是刻意的靜默,見 CLAUDE.md"
    )


def test_panel_is_hidden_until_data_arrives(html: str) -> None:
    assert "#regimepanel{display:none}" in html
    assert "#regimepanel.on{display:block}" in html
    assert 'classList.add("on")' in html


def test_external_strings_are_escaped_before_innerhtml(html: str) -> None:
    """signals.json 帶有外部來源的字串(國發會燈號、錯誤訊息)。

    CLAUDE.md 第四道防線:外部內容必須淨化才能進入會被渲染的地方。
    """
    block = html[html.index("function renderRegime"):html.index("function checkRegime")]
    for field in ("f.label", "f.detail", "f.rule", "f.source", "f.why"):
        assert f"esc({field})" in block, f"{field} 未經 esc() 就進了 innerHTML"
    assert "esc(warn.band" in block
    assert "esc(n)" in block, "市場名稱未逸出"


# ── 硬邊界 ───────────────────────────────────────────────────────────────


def test_pwa_regime_layer_does_not_touch_the_cash_number(html: str) -> None:
    """renderRegime 不得寫入任何核心讀數的 DOM 節點。

    這是「不要把 PWA 改爛」的機械化保證:就算以後有人想「順手」讓警訊
    調整水位,這條會擋下來。
    """
    block = html[html.index("function renderRegime"):html.index("function checkRegime")]
    core_ids = ["bigpct", "verdict", "momcash", "vixcash", "tmark", "tval",
                "fill", "cap", "allin", "svix", "sma", "ddbig"]
    touched = [i for i in core_ids if f'$("{i}")' in block]
    assert not touched, f"體制層碰到了核心讀數節點:{touched}"

    for fn in ("momCash", "vixCash", "compute(", "paintLadder", "renderTemp"):
        assert fn not in block, f"體制層呼叫了核心計算 {fn}"


def test_core_ladder_constants_are_untouched(html: str) -> None:
    """順手把兩腿階梯抄一份在這裡當金絲雀。改到它們的人會先撞到這條。"""
    assert "function momCash(mom){let c=0; if(mom<0)c=.30; if(mom<-.03)c=.60; " \
           "if(mom<-.08)c=.90; if(mom<-.13)c=1.0; return c;}" in html
    assert "function vixCash(r){let c=0; if(r>.5)c=.24; if(r>1.0)c=.42; " \
           "if(r>1.8)c=.66; if(r>2.6)c=.90; if(r>3.4)c=1.0; return c;}" in html
